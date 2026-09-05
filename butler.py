"""Plant Butler backend.

One container on the NAS, LAN only. The board talks first: one plain-HTTP
POST per report interval, `k=v` tokens in the body, one static token in the
`X-Token` header. The response is `k=v` too: the next report interval and,
when one is queued, at most one command.

Storage is stdlib sqlite3 on a bind-mounted volume, schema in `schema.sql`,
WAL so a reader never blocks the writer. Timestamps are stamped on arrival:
the board has no clock worth trusting.

A malformed report is refused WHOLE with a 400 — half a report stored looks
exactly like a working system with dead sensors, and a board bug should be
loud. Strictness covers the encoding too: invalid UTF-8 is a refusal, not a
repair, because one flipped byte in `c=` would otherwise mint a phantom
controller and quietly fork the readings. Unknown KEYS, by contrast, are
ignored on purpose: the board will grow keys like `last=` before this
service learns to read them, and a report must land whole the day either
side updates first.

The board's failure mode is the design load-bearer: firmware retries a report
once when the response is lost, so an identical `t=` (board uptime, ms) from
the same controller within a short window is the same report arriving twice
and is answered 200 without storing again. The window matters — uptime
restarts at every reboot, so old `t` values legitimately recur, and a
permanent uniqueness rule would silently drop genuine readings.

Commands are a hand-off, not a job system. One slot per controller: a
command is queued (`POST /command`), handed to the board exactly once in
the response to its next report (queued -> sent), and acknowledged by the
report after that (`ack=<id>`, -> acked). A report that carries no matching
ack while a command sits sent expires it: either the response that carried
it was lost or the board dropped it — both mean the board does not have it,
and re-handing a watering command the board might still execute is how a
plant drowns. A queued command nobody collected within BUTLER_CMD_TTL_S
expires too. Expired means gone; whoever wants water asks again.

Watering rules run in-process on each fresh report — no cron, no thread.
Every gate errs dry: rules act only when the report itself says the
reservoir floats (`float=1`) and the manifold knows where it is (`pos=ok`),
outside quiet hours, on a full median window below the pot's target, with
no open command on the hose, cooldown passed and the daily cap unspent.
The board does not send `float=` or `pos=` yet, so the rules ship dark and
the fake device exercises them. In auto the command is queued directly; in
learning it becomes a proposal for `POST /approve`, and `POST /verdict`
records how the dose worked out. The flip to auto is a human act, per pot.

Alerting is a ticker, the one periodic thing here: a quiet controller
cannot be noticed on report arrival. Every ALERT_TICK_S it evaluates the
alert rules from database state alone — controller silent, a mapped
sensor's channel gone missing, reservoir empty, manifold position lost, a
safety field that vanished after the board had been sending it, a dose
that was never acked or came up short on the meter or did not raise
moisture, a learning proposal waiting — posts the transitions to a public
ntfy.sh topic (BUTLER_NTFY_TOPIC; the topic name is the secret), and only
after a fully clean pass GETs BUTLER_DEADMAN_URL. A pass with nothing to
send must first prove ntfy reachable, so the butler dying and the butler
losing ntfy both stop the pings. Raising is debounced (two bad sightings
inside FLAP_WINDOW_S for the board's own fields, thresholds for silence)
under one re-raise per REALERT_FLOOR_S per condition, dose failures are
floored per controller, and the observation window survives short restarts
via a bookkeeping row — because a phone that gets muted is worse than an
alert that arrives three minutes late.
"""

import asyncio
import contextlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import QueryParams
from starlette.requests import ClientDisconnect

# What GET /hello answers with. Kept here rather than read from the package
# metadata because the container installs no package — it copies butler.py
# beside fastapi and runs it. A test asserts this and pyproject.toml agree,
# which is the only thing that keeps the two honest.
VERSION = "0.17.0"

BODY_CAP = 4096  # a full 15-channel report is ~200 bytes; 4 KB is generous
# Photographs are the first thing here that is not small. The phone caps the
# long edge before it uploads, which puts a JPEG around 300-500 KB; 3 MiB is
# room for a bad guess and still a refusal long before the NAS volume cares.
PHOTO_CAP = 3 * 1024 * 1024
JPEG_HEAD = b"\xff\xd8\xff"  # every JPEG starts SOI + a marker
PHOTO_LIMIT = 100  # the strip's default page
MAX_PHOTO_LIMIT = 500
MAX_PHOTO_EDGE = 8192  # w=/h= are what the phone says it downscaled to
# Ids are four random bytes, so a collision needs tens of thousands of
# photographs in one install. This is what stops one being a 500.
PHOTO_ID_TRIES = 4
# Every id that becomes part of a filesystem path goes through this first.
# Ids here are minted, so nothing legitimate is turned away; what it stops
# is a `pot=../../etc` writing outside the photo store, which no amount of
# "but the pot has to exist" reasoning further down would catch on its own.
SAFE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
# err= is the board's last safety error: one of its own short lowercase
# tokens (contra, resetmid, range, heap, ...). Bounded like every other
# field, so a stray value cannot become an unbounded TEXT on status.
ERR_TOKEN = re.compile(r"\A[a-z_]{1,16}\Z")
RETRY_WINDOW_S = 300  # how long an identical (controller, t) counts as a retry
MAX_CHANNEL = 255
# The board's own number, and an integer like every other identifier on the
# wire. It was free text until 0.17.0, which made `c=` the one field a typo
# could turn into a whole second garden: a report from "bench1 " or "Bench1"
# opened its own controller row, its own heartbeat and its own alerts, and
# nothing anywhere said the two were the same board.
MAX_CONTROLLER = 255
MAX_RAW = 2**31  # 14-bit ADC today; headroom without letting 2**63 near sqlite
# The board's own PB_DOSE_RIG_MAX_ML, and the two move together: a pot
# above it is refused by the firmware with err=range, acked with flow_ml=0,
# charged nothing, cooled down and paged high once per cooldown, forever,
# and never watered. Refusing here, at /command and at pot save, is what
# keeps that loop unreachable. (DECISIONS #7: a full dump is a mop-up; a
# quarter of the bench reservoir per dose is the number that makes it one.)
MAX_DOSE_ML = 250
MAX_CAP_S = 60  # the firmware enforces its own cap; this bounds what we ask
MIN_NEXT_S, MAX_NEXT_S = 5, 3600  # the interval knob's sane range
RULES_WINDOW = 5  # median of this many readings is the whole smoothing story
PROPOSAL_TTL_S = 7200  # a proposal nobody approved in 2 h expires
DEFAULT_COOLDOWN_H = 6  # when a pot does not set its own; 0 disables
DEFAULT_DAILY_CAP_DOSES = 3  # a NULL daily_cap_ml means this many doses
FLOW_FLOOR_ML_S = 20  # worst-case pump flow, sizes cap_s; bench-rig-tunable
VERDICT_VALUES = ("ok", "too_much", "too_little")
ALERT_TICK_S = 60  # the alert ticker's beat; a create_app parameter in tests
SILENT_AFTER_S = 600  # BUTLER_SILENT_S default; the floor is 3x the interval
PERSIST_S = 180  # a status must hold this long before it raises or clears
REALERT_FLOOR_S = 3600  # a cleared condition sounds again at most hourly
SOAK_S = 1800  # water needs this long to reach the sensor before judging
MIN_RISE_PCT = 5  # a dose that raised moisture less than this did not work
DOSE_LOOKBACK_S = 86400  # doses older than a day are history, not news
PROPOSAL_NUDGE_S = 86400  # one proposal nudge per hose per day
UP_AFTER_S = 600  # the one "butler is up" probe, once uptime clears this
NTFY_TIMEOUT_S = 10
FLAP_WINDOW_S = 600  # two bad float/pos sightings this close together raise
RESUME_GRACE_S = 600  # a restart shorter than this keeps the observation window
UP_PROBE_FLOOR_S = 86400  # the up-probe fires at most daily, across restarts


class Report(NamedTuple):
    controller: str
    channels: dict[int, int]
    t: int | None  # board uptime, ms
    ack: int | None  # command id the board says it executed
    flow_ml: int | None  # what the flow meter counted while executing it
    float_ok: int | None  # reservoir float switch: 1 floats, 0 empty
    pos: str | None  # manifold position: 'ok' or 'unknown'
    err: str | None  # the board's last safety error token, when it sent one


class Command(NamedTuple):
    controller: str
    kind: str  # 'water' | 'stop'
    outlet: int | None
    ml: int | None
    cap_s: int | None


class Alert(NamedTuple):
    """One message for the phone plus the write that remembers it went out.

    `message` None is a silent judgement (a dose that worked): the record
    step still runs, nothing is posted — "tell me when it's wrong" only.
    `record` is applied by the tick only after a successful send, in its
    own short transaction.
    """

    key: str | None  # alerts-table key; None for the unrecorded up-probe
    priority: str  # ntfy priority: 'high' | 'default' | 'min'
    tags: str  # ntfy Tags header: emoji shortcodes
    message: str | None
    record: Callable | None = None


SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


def hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def new_pot_id() -> str:
    """A pot's identity, in the plan tool's own style: `pot-3f9a21`.

    Random rather than sequential so it can be minted anywhere and never
    encodes an order that means nothing.
    """
    return "pot-" + secrets.token_hex(3)


def write_new_file(path: Path, blob: bytes) -> None:
    """Create `path` and write `blob` into it, refusing to overwrite.

    O_EXCL, so claiming a name and finding it taken is one atomic step:
    a photograph's id is also its filename, and overwriting would destroy
    an earlier picture whose row would then point at nothing.
    """
    with open(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), "wb") as f:
        f.write(blob)


def new_photo_id() -> str:
    """A photograph's identity, and its filename: `photo-3f9a21b4`.

    Four bytes rather than a pot's three: this is also a path segment, and
    the only thing standing between a guessed id and somebody else's
    picture on a shared tailnet.
    """
    return "photo-" + secrets.token_hex(4)


# The columns the old pots table carried, in its own order, so the rebuild
# reads a 0.7.0 database without guessing.
_OLD_POT_COLUMNS = (
    "name",
    "controller",
    "channel",
    "outlet",
    "plant_type",
    "plant_size",
    "pot_size",
    "soil",
    "dry_raw",
    "wet_raw",
    "target_low_pct",
    "target_high_pct",
    "dose_ml",
    "mode",
    "cooldown_h",
    "daily_cap_ml",
    "enabled",
)


def _pots_ddl() -> list[str]:
    """The CREATE for `pots` and for `pots_now`, taken from schema.sql itself.

    The rebuild drops both and has to put them back inside its own
    transaction, where executescript() cannot go (it commits first). Rather
    than keep a second copy of the DDL here — to drift from the schema file
    the day a column is added — let sqlite parse schema.sql in a scratch
    database and hand back exactly what it made.
    """
    scratch = sqlite3.connect(":memory:")
    scratch.executescript(SCHEMA_SQL)
    ddl = [
        sql
        for (sql,) in scratch.execute(
            "SELECT sql FROM sqlite_master WHERE name IN ('pots', 'pots_now') "
            "ORDER BY type"  # 'table' before 'view': the view reads the table
        )
    ]
    scratch.close()
    return ddl


# Columns schema.sql grew after its CREATE had already run somewhere, with
# the old column each one carries its value over from (None: nothing to
# carry). Append-only, like the schema itself.
# The converters are lambdas because cm_from_text is defined further down
# and this tuple is built at import.
ADDED_COLUMNS = (
    ("pots", "plant_height_cm", "REAL", "plant_size", lambda v: cm_from_text(v)),
    ("pots", "pot_diameter_cm", "REAL", "pot_size", lambda v: cm_from_text(v)),
    ("species_names", "family", "TEXT", None, None),
    # The switch became a word. The carry sets the value and cannot repair
    # the wiring: a pot carried over as `graveyard` keeps its open mapping
    # window, where a graveyarding through POST /pot would have closed it.
    # Unreachable in production — the database this ships with is new — and
    # untrue of every pot in the test fixture, which are all enabled.
    (
        "pots",
        "status",
        "TEXT NOT NULL DEFAULT 'alive'",
        "enabled",
        lambda flag: "alive" if flag else "graveyard",
    ),
    ("readings", "pot_id", "TEXT", None, None),
    ("commands", "pot_id", "TEXT", None, None),
    # Trust the tank (0.18.0): a board's own last error, its durable latch,
    # whether it ever knew its position, and whether it is retired.
    ("controllers", "retired", "INTEGER NOT NULL DEFAULT 0", None, None),
    ("status", "err", "TEXT", None, None),
    ("status", "err_ts", "INTEGER", None, None),
    ("status", "latched_ts", "INTEGER", None, None),
    ("status", "latch_reason", "TEXT", None, None),
    ("status", "pos_ok_seen", "INTEGER", None, None),
)


def add_columns(con: sqlite3.Connection) -> list[str]:
    """ALTER in whatever of ADDED_COLUMNS this database has not got yet.

    `CREATE TABLE IF NOT EXISTS` is additive about TABLES and nothing else:
    a column appended to a CREATE that has already run on a database never
    reaches it, and the table quietly keeps the shape it was born with. This
    is the additive answer to that, and it is deliberately an ALTER rather
    than a second rebuild — the one rebuild this project has (migrate) stays
    the only one.

    Runs BEFORE schema.sql, because `pots_now` is recreated from that script
    and a view over a column the table has not got yet parses fine and then
    fails on every read.

    The carry-over is best-effort by design: `pot_size` held "14cm", "10"
    and "small", and only two of those are a measurement. A word is dropped
    rather than invented into centimetres, and a converter that answers None
    leaves the new column NULL.
    """
    added = []
    with con:
        for table, column, kind, source, convert in ADDED_COLUMNS:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            if not cols or column in cols:
                continue  # no such table here yet, or nothing to do
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            added.append(f"{table}.{column}")
            if source is None or source not in cols:
                continue
            for rowid, text in con.execute(
                f"SELECT rowid, {source} FROM {table} WHERE {source} IS NOT NULL"
            ).fetchall():
                value = convert(text)
                if value is not None:
                    con.execute(
                        f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                        (value, rowid),
                    )
    return added


def migrate(con: sqlite3.Connection, db_path: str) -> bool:
    """The one-time rebuild of `pots`, run at startup. Returns True if it ran.

    `schema.sql` is additive by rule and cannot retype a primary key, which
    is what turning the integer id into `pot-xxxxxx` needs. This is that
    exception, taken once and deliberately: copy into the new shape, move
    the wiring into pot_mappings with from_ts 0 so no history is orphaned,
    then swap. Idempotent — an already-migrated database is recognised by
    the pots table having no `controller` column.
    """
    cols = [r[1] for r in con.execute("PRAGMA table_info(pots)")]
    if not cols or "controller" not in cols:
        return False  # fresh database, or already rebuilt
    backup = None
    if db_path != ":memory:":
        # The live database is WAL, so recent commits sit in the -wal file
        # and a plain copy of the main file would back up everything except
        # what is most at risk. Checkpoint first, and refuse the whole
        # rebuild if anything is holding the log open: a deferred migration
        # is recoverable, a DROP behind a blank backup is not.
        busy, *_ = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise sqlite3.OperationalError(
                "cannot checkpoint the WAL before rebuilding pots: another "
                "connection is holding it open, and the backup would be short"
            )
        backup = db_path + ".pre-identity.bak"
        # Created exclusively, and kept if it is already there. A backup is
        # only ever written while `pots` still has its `controller` column,
        # and a rebuild that dies rolls back to exactly that shape, so an
        # existing one is always a good pre-identity copy. Reaching here a
        # second time is a retry after a killed rebuild — or a container
        # start that overlapped another, which would otherwise copy an
        # ALREADY-REBUILT file over the only copy of the garden. Bailing
        # out instead of keeping would lose the other way: the retry after
        # a kill has to be able to finish.
        try:
            with open(backup, "xb") as copy, open(db_path, "rb") as live:
                shutil.copyfileobj(live, copy)
        except FileExistsError:
            print(
                f"a backup is already at {backup}: keeping it, not rewriting it",
                file=sys.stderr,
            )
        except BaseException:
            # A half-written backup would be kept by every later run.
            with contextlib.suppress(OSError):
                os.unlink(backup)
            raise
    rows = con.execute(
        f"SELECT {', '.join(_OLD_POT_COLUMNS)} FROM pots ORDER BY id"
    ).fetchall()
    # Everything the new shape needs except `pots` and its view — the
    # pot_mappings table and every index — arrives here, before the rebuild
    # opens its transaction, because executescript() commits and would
    # otherwise split it in two.
    con.executescript(SCHEMA_SQL)
    con.execute("BEGIN IMMEDIATE")
    with con:
        # The guard again, this time with the write lock held. The one at
        # the top of this function is read outside any lock, so two
        # container starts on the same /data — Container Manager can leave
        # two overlapping briefly — both pass it, and the one that waits
        # here for the lock would wake up and rebuild the winner's fresh
        # table a second time from the rows it read before, minting new
        # ids and orphaning the winner's pot_mappings rows against ids
        # that no longer exist.
        if "controller" not in [r[1] for r in con.execute("PRAGMA table_info(pots)")]:
            return False
        # One transaction, DDL included (sqlite rolls that back like any
        # other statement). A container killed mid-rebuild — a NAS reboot, an
        # OOM kill, a power cut — must come back with the old table intact
        # and retry: an empty `pots` in the NEW shape would read to the guard
        # above as "already migrated", and the garden would be gone for good.
        con.execute("DROP VIEW IF EXISTS pots_now")
        con.execute("DROP TABLE pots")
        for ddl in _pots_ddl():
            con.execute(ddl)
        for row in rows:
            old = dict(zip(_OLD_POT_COLUMNS, row))
            # The old shape's two free-text sizes, as far as they were ever
            # measurements. add_columns() does the same for a database that
            # is past this rebuild; both go through one reader so they
            # cannot disagree.
            old["plant_height_cm"] = cm_from_text(old.pop("plant_size"))
            old["pot_diameter_cm"] = cm_from_text(old.pop("pot_size"))
            # The switch became a word here too, for the same reason and by
            # the same rule: the rebuild writes the columns pots has NOW.
            old["status"] = "alive" if old.pop("enabled") else "graveyard"
            pot_id = new_pot_id()
            keys = [k for k in old if k not in ("controller", "channel", "outlet")]
            con.execute(
                f"INSERT INTO pots (id, {', '.join(keys)}) "
                f"VALUES (?, {', '.join('?' * len(keys))})",
                [pot_id, *(old[k] for k in keys)],
            )
            if any(old[k] is not None for k in ("controller", "channel", "outlet")):
                con.execute(
                    "INSERT INTO pot_mappings "
                    "(pot_id, controller, channel, outlet, from_ts, to_ts) "
                    "VALUES (?, ?, ?, ?, 0, NULL)",
                    (pot_id, old["controller"], old["channel"], old["outlet"]),
                )
    # Say so: this runs once, unattended, and it rewrites every pot id the
    # app and the operator were using. A silent irreversible step is one
    # nobody can audit afterwards, and nobody would find the backup.
    print(
        f"rebuilt {len(rows)} pots onto random ids and moved their wiring "
        "into pot_mappings"
        + (f"; the database as it was is at {backup}" if backup else ""),
        file=sys.stderr,
    )
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib follows a 301/302 by replaying the POST as a bodyless GET; on a
    # to-HTTPS-redirecting proxy that "succeeds" while the message is
    # dropped. A redirect here is a misconfiguration: fail it loudly.
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post_ntfy(base_url: str, topic: str, alert: Alert) -> bool:
    """One message to ntfy, True only on a 2xx. Never raises — alerting must
    never take the service down — and a False is retried on a later tick."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{topic}",
        data=(alert.message or "").encode("utf-8"),
        method="POST",
        headers={
            "Title": "Plant Butler",
            "Priority": alert.priority,
            "Tags": alert.tags,
            "User-Agent": "plantbutler-backend",
        },
    )
    try:
        with _OPENER.open(request, timeout=NTFY_TIMEOUT_S) as answer:
            return 200 <= answer.status < 300
    except (OSError, http.client.HTTPException, ValueError):
        return False


def ping_deadman(url: str) -> bool:
    """GET the dead-man URL; True on a 2xx. The same never-raise contract."""
    try:
        with _OPENER.open(url, timeout=NTFY_TIMEOUT_S) as answer:
            return 200 <= answer.status < 300
    except (OSError, http.client.HTTPException, ValueError):
        return False


def _int_in(value: str, key: str, low: int, high: int) -> int:
    """One integer field, bounds half-open [low, high). ASCII digits only:
    bare int() would quietly repair Unicode digits, underscores and a leading
    `+` — values the board could never emit — into plausible numbers."""
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"{key}= is not an integer: {value!r}")
    n = int(value)
    if not low <= n < high:
        raise ValueError(f"{key}= out of range: {value}")
    return n


_CM = re.compile(r"\A\d{1,4}(?:\.\d{1,2})?\Z")


def _cm_in(value: str, key: str, high: float) -> float:
    """One measurement in centimetres, 0 exclusive to `high` inclusive.

    The same ASCII-only strictness as `_int_in` and for the same reason —
    bare float() takes "1e3", "inf", "nan", a Unicode digit and a leading
    `+`, and every one of those would reach log2() in the band engine. Zero
    is refused rather than treated as unsaid, because a 0 cm pot is a
    half-finished edit and saying so beats silently ignoring it.
    """
    if not (value.isascii() and _CM.match(value)):
        raise ValueError(f"{key}= is not a measurement in cm: {value!r}")
    n = float(value)
    if not 0 < n <= high:
        raise ValueError(f"{key}= out of range: {value}")
    return n


def cm_from_text(text: str | None) -> float | None:
    """A measurement out of the free text `plant_size` and `pot_size` held.

    Those two fields were TEXT and took anything: "14cm", "10", "small". The
    numbers carry over, the words do not, and a word is dropped rather than
    guessed at — "small" meant one thing to the old keyword table and would
    have to be invented into centimetres here.
    """
    if not text:
        return None
    found = re.search(r"\d{1,4}(?:\.\d{1,2})?", text)
    if not found:
        return None
    n = float(found.group())
    return n if 0 < n <= 1000 else None


def parse_report(text: str) -> Report:
    """`k=v` tokens, whitespace-separated; line breaks are whitespace too.

    Strict about shape — a malformed token, a duplicate key, a non-integer or
    out-of-range value refuses the whole report — and silent about unknown
    keys, for the reasons the module docstring gives. ASCII digits only in a
    channel key: Unicode digits would alias onto ASCII channel numbers.
    """
    controller = None
    channels: dict[int, int] = {}
    t = ack = flow_ml = float_ok = pos = err = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, MAX_CONTROLLER + 1)
        elif key == "t":
            if t is not None:
                raise ValueError("t= given twice")
            t = _int_in(value, "t", 0, 2**63)
        elif key == "ack":
            if ack is not None:
                raise ValueError("ack= given twice")
            ack = _int_in(value, "ack", 1, 2**63)  # command ids start at 1
        elif key == "flow_ml":
            if flow_ml is not None:
                raise ValueError("flow_ml= given twice")
            flow_ml = _int_in(value, "flow_ml", 0, MAX_RAW)
        elif key == "float":
            if float_ok is not None:
                raise ValueError("float= given twice")
            float_ok = _int_in(value, "float", 0, 2)
        elif key == "pos":
            if pos is not None:
                raise ValueError("pos= given twice")
            if value not in ("ok", "unknown"):
                raise ValueError(f"pos= must be ok or unknown, got {value!r}")
            pos = value
        elif key == "err":
            if err is not None:
                raise ValueError("err= given twice")
            if not ERR_TOKEN.match(value):
                raise ValueError(f"err= must be a short lowercase token, got {value!r}")
            err = value
        elif key.startswith("ch") and key[2:].isascii() and key[2:].isdigit():
            channel = int(key[2:])
            if channel > MAX_CHANNEL:
                raise ValueError(f"channel index out of range: {key}")
            if channel in channels:
                raise ValueError(f"channel given twice: {key}")
            channels[channel] = _int_in(value, key, 0, MAX_RAW)
    # `is None`, not falsiness: board 0 is a real board, and it is the one
    # the app fills in by default.
    if controller is None:
        raise ValueError("no c= in the report")
    if not channels:
        raise ValueError("no chN= in the report")
    if flow_ml is not None and ack is None:
        raise ValueError("flow_ml= without ack=")
    return Report(controller, channels, t, ack, flow_ml, float_ok, pos, err)


def cap_for(ml: int) -> int:
    """Seconds the pump may run for a dose: worst-case flow plus slack,
    bounded by MAX_CAP_S. The one owner of FLOW_FLOOR_ML_S — the rules and
    a manual command without cap_s= both size their cap here, so a bench
    retune happens in one place."""
    return min(MAX_CAP_S, ml // FLOW_FLOOR_ML_S + 5)


def parse_command(text: str) -> Command:
    """The `POST /command` body, same dialect and strictness as a report.

    `c=<controller>` plus either `water=<outlet> ml=<dose> [cap_s=<cap>]` or
    `stop=1`; a dose without cap_s= gets the rules' own cap. Unknown keys
    are ignored here too, so the app can grow fields before this service
    reads them.
    """
    controller = None
    outlet = ml = cap_s = None
    stop = False
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, MAX_CONTROLLER + 1)
        elif key == "water":
            if outlet is not None:
                raise ValueError("water= given twice")
            outlet = _int_in(value, "water", 0, MAX_CHANNEL + 1)
        elif key == "ml":
            if ml is not None:
                raise ValueError("ml= given twice")
            ml = _int_in(value, "ml", 1, MAX_DOSE_ML + 1)
        elif key == "cap_s":
            if cap_s is not None:
                raise ValueError("cap_s= given twice")
            cap_s = _int_in(value, "cap_s", 1, MAX_CAP_S + 1)
        elif key == "stop":
            if stop:
                raise ValueError("stop= given twice")
            if value != "1":
                raise ValueError(f"stop= must be 1, got {value!r}")
            stop = True
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the command")
    if stop and not (outlet is None and ml is None and cap_s is None):
        raise ValueError("stop takes no dose")
    if stop:
        return Command(controller, "stop", None, None, None)
    if outlet is None:
        raise ValueError("neither water= nor stop=1")
    if ml is None:
        raise ValueError("water= needs ml=")
    return Command(
        controller, "water", outlet, ml, cap_for(ml) if cap_s is None else cap_s
    )


def parse_interval(text: str) -> tuple[str, int]:
    """The `POST /interval` body: `c=<controller> next=<seconds>`.

    `next=0` clears the override back to the BUTLER_NEXT_S default.
    """
    controller = None
    next_s = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, MAX_CONTROLLER + 1)
        elif key == "next":
            if next_s is not None:
                raise ValueError("next= given twice")
            next_s = _int_in(value, "next", 0, MAX_NEXT_S + 1)
            if 0 < next_s < MIN_NEXT_S:
                raise ValueError(f"next= below {MIN_NEXT_S}s: {value}")
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the request")
    if next_s is None:
        raise ValueError("no next= in the request")
    return controller, next_s


# The whole window at the default bucket (2016); a week at a minute would
# be 10080 rows of JSON.
# A month back, which is what the app's widest chart window asks for. The
# bucket cap is unchanged and still what actually bounds the answer: a month
# at hourly buckets is 744 points, well under it, while a month at five
# minutes would be refused as it always was.
HISTORY_MAX_HOURS = 24 * 31
HISTORY_MAX_BUCKETS = 168 * 3600 // 300


def parse_doses(params: QueryParams) -> tuple[str | None, int, int | None, int]:
    """`GET /doses?pot=<pot id>&limit=<1..200>&before=<ts>&before_id=<id>`.

    No pot means the whole garden. `before`/`before_id` are the last row a
    caller already has: the page after it. Both together, because the list
    is ordered on (when it was handed out, id) and several doses can share
    a second — a cursor on the timestamp alone would skip or repeat them.
    The commands table is never pruned, so without this the older history
    would be permanently out of reach behind the newest `limit` rows.

    Same query-parameter strictness as /history, and the same plain-text
    refusal.
    """

    def one(key: str, default: str | None = None) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else default

    pot = one("pot")
    if pot is not None and not pot:
        raise ValueError("pot= is empty")
    limit = _int_in(one("limit", "50") or "", "limit", 1, DOSES_MAX + 1)
    raw_before = one("before")
    before = None if raw_before is None else _int_in(raw_before, "before", 0, 1 << 42)
    raw_before_id = one("before_id")
    if raw_before_id is not None and before is None:
        raise ValueError("before_id= needs a before=")
    before_id = _int_in(raw_before_id or "0", "before_id", 0, 1 << 42)
    return pot, limit, before, before_id


def parse_history(params: QueryParams) -> tuple[str, int, int]:
    """`GET /history?pot=<pot id>&hours=<1..168>&bucket_s=<60..3600>`.

    Query parameters instead of a body because it is a read; the same
    ASCII-digit strictness and the same "given twice" refusal as every k=v
    field (a multidict would otherwise take the last value quietly), and
    the same plain-text refusal, so the app has one error dialect to show.
    """

    def one(key: str, default: str | None = None) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else default

    pot = one("pot")
    if not pot:
        raise ValueError("no pot= in the request")
    hours = _int_in(one("hours", "24") or "", "hours", 1, HISTORY_MAX_HOURS + 1)
    bucket_s = _int_in(one("bucket_s", "300") or "", "bucket_s", 60, 3601)
    if hours * 3600 // bucket_s > HISTORY_MAX_BUCKETS:
        raise ValueError(
            f"too many buckets: {hours} h at {bucket_s} s is over {HISTORY_MAX_BUCKETS}"
        )
    return pot, hours, bucket_s


POT_INT_FIELDS = {  # half-open bounds, like every other field
    "controller": (0, MAX_CONTROLLER + 1),
    "channel": (0, MAX_CHANNEL + 1),
    "outlet": (0, MAX_CHANNEL + 1),
    "dry_raw": (0, MAX_RAW),
    "wet_raw": (0, MAX_RAW),
    "target_low_pct": (0, 101),
    "target_high_pct": (0, 101),
    "dose_ml": (1, MAX_DOSE_ML + 1),
    "cooldown_h": (0, 8761),  # a year of cooldown is already a config error
    "daily_cap_ml": (0, 100_001),
}
POT_TEXT_FIELDS = ("species",)  # the one still typed rather than picked
POT_CM_FIELDS = {  # centimetres, and a plausible ceiling for each
    "pot_diameter_cm": 200.0,  # across the rim: a half-barrel and no further
    "plant_height_cm": 1000.0,  # a 10 m tree is not in a pot on the balcony
}
POT_MAP_FIELDS = ("controller", "channel", "outlet")  # pot_mappings, not pots
POT_MODES = ("manual", "learning", "auto")
# What a pot IS, where `enabled` was what it was allowed to do. A closed set,
# and shaped so a third word (paused-but-wired, say) is one entry here plus
# one label in the app.
POT_STATUSES = ("alive", "graveyard")
# A positive allow-list, never `!= 'graveyard'`. A status this build has not
# heard of — a newer backend's word reaching an older reader — must not
# water, propose or page. Failure direction is dry (DECISIONS #5).
LIVE_STATUSES = ("alive",)


def waters(status: str | None) -> bool:
    """Whether a pot in this state may be watered, proposed for or alarmed
    about. The Python half of the allow-list; live_sql() is the SQL half."""
    return status in LIVE_STATUSES


def live_sql(col: str = "status") -> str:
    """The same allow-list as a SQL predicate. One source, so a fourth
    status cannot be admitted by one reader and refused by another."""
    return f"{col} IN (" + ", ".join(f"'{s}'" for s in LIVE_STATUSES) + ")"
LAST_DOSE_KEYS = (
    "id",
    "ml",
    "cap_s",
    "flow_ml",
    "state",
    "source",
    "sent_ts",
    "acked_ts",
    "verdict",
)
DOSE_KEYS = (
    "id",
    "kind",
    "ml",
    "cap_s",
    "flow_ml",
    "state",
    "source",
    "created_ts",
    "sent_ts",
    "acked_ts",
    "verdict",
    "pot",
    "pot_name",
)
DOSES_MAX = 200

POT_COLUMNS = (  # the pots_now view's shape: pot columns plus the open mapping
    "id",
    "name",
    "species",
    "controller",
    "channel",
    "outlet",
    "plant_type",
    "plant_height_cm",
    "pot_diameter_cm",
    "soil",
    "dry_raw",
    "wet_raw",
    "target_low_pct",
    "target_high_pct",
    "dose_ml",
    "mode",
    "cooldown_h",
    "daily_cap_ml",
    "status",
)


def window_edge(con: sqlite3.Connection, pot_id: str, now: int) -> int:
    """Where a pot's open mapping window closes and its next one opens.

    `now`, except that the boundary is never allowed to move backwards
    past what the database has already recorded. A window that ends before
    it began, or before a dose it holds, matches nothing at all: the join
    wants from_ts <= sent_ts <= to_ts, and the pot silently stops owning
    that dose's cooldown and daily cap. Fixing the clock afterwards does
    not rewrite the row, so unlike a clock that is merely wrong this is
    permanent — and pot_mappings is what the watering gates read.

    The server clock does step backwards: a container that starts before
    the NAS has synced runs minutes or hours off until NTP corrects it,
    and a wiring save on either side of that correction is ordinary. So
    the floor is the window's own start and the newest dose that went down
    its hose inside it. With the clock behaving, all three are `now`.
    """
    row = con.execute(
        "SELECT from_ts, controller, outlet FROM pot_mappings "
        "WHERE pot_id = ? AND to_ts IS NULL",
        (pot_id,),
    ).fetchone()
    if row is None:
        return now
    from_ts, controller, outlet = row
    (dosed,) = con.execute(
        "SELECT COALESCE(MAX(sent_ts), 0) FROM commands "
        "WHERE controller IS ? AND outlet IS ? AND sent_ts >= ?",
        (controller, outlet, from_ts),
    ).fetchone()
    return max(now, from_ts, dosed)


def _hose_since(pot: str, controller: str, outlet: str) -> str:
    """A scalar SQL expression: when this pot's HOSE last changed.

    A dose belongs to a pot and travels with it, but a proposal is an offer
    to open a hose, so it counts only while this pot is still the one on
    that hose. The open mapping window is the wrong fence for that: a
    correction to the sensor CHANNEL closes it and opens another without
    the hose having moved anywhere, and a pending proposal would silently
    leave the card while its 'proposed' row went on holding the hose slot.

    So: the start of the contiguous run of this pot's windows that share
    its current (controller, outlet). Windows are contiguous by
    construction — a remap closes the open row and opens the next in the
    same second — so that start is the last time a window of this pot
    named a DIFFERENT hose, and the pot's first window when none ever did.
    The three arguments are SQL expressions naming the pot and its current
    hose, never values off the wire.
    """
    return (
        "COALESCE("
        f"(SELECT MAX(w.to_ts) FROM pot_mappings w WHERE w.pot_id = {pot} "
        f"AND (w.controller IS NOT {controller} OR w.outlet IS NOT {outlet})), "
        f"(SELECT MIN(w.from_ts) FROM pot_mappings w WHERE w.pot_id = {pot}), "
        "0)"
    )


def parse_pot(text: str) -> dict:
    """The `POST /pot` body: which pot, plus whatever fields to set.

    An `id=` is an EDIT of that pot — name included, so renaming is an
    ordinary field edit and no history is orphaned by it. A bare `name=`
    is a create, and mints an id.

    A partial upsert either way — only the keys given change, so
    recalibration is `id=pot-3f9a21 dry_raw=13000 wet_raw=4200` and
    nothing else moves. Values are single k=v tokens, so multi-word text
    uses underscores. Unknown keys are ignored, for the same reason as
    everywhere else.
    """
    fields: dict = {}
    known = {
        "id",
        "name",
        "mode",
        "plant_type",
        "soil",
        "status",
        *POT_TEXT_FIELDS,
        *POT_CM_FIELDS,
        *POT_INT_FIELDS,
    }
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key not in known:
            continue
        if key in fields:
            raise ValueError(f"{key}= given twice")
        if not value:
            raise ValueError(f"{key}= is empty")
        if key in POT_INT_FIELDS:
            low, high = POT_INT_FIELDS[key]
            fields[key] = _int_in(value, key, low, high)
        elif key in POT_CM_FIELDS:
            fields[key] = _cm_in(value, key, POT_CM_FIELDS[key])
        elif key == "mode":
            if value not in POT_MODES:
                raise ValueError(f"mode= must be one of {'|'.join(POT_MODES)}")
            fields[key] = value
        elif key == "plant_type":
            # A closed set on the way in, tolerant on the way out: a value
            # written before this set existed still reads, it simply matches
            # no band. Refusing it here is what keeps the dropdown honest —
            # a free-text "basil" used to look saved and quietly pick
            # nothing.
            if value not in PLANT_KINDS:
                raise ValueError(
                    f"plant_type= must be one of {'|'.join(PLANT_KINDS)}"
                )
            fields[key] = value
        elif key == "soil":
            # Closed on the way in for the same reason as plant_type, and
            # tolerant on the way out for the same reason: a row still
            # holding free text from before this set reads fine and simply
            # matches no shift.
            if value not in SOIL_SHIFTS:
                raise ValueError(f"soil= must be one of {'|'.join(SOIL_SHIFTS)}")
            fields[key] = value
        elif key == "status":
            if value not in POT_STATUSES:
                raise ValueError(f"status= must be one of {'|'.join(POT_STATUSES)}")
            fields[key] = value
        else:
            fields[key] = value
    if "id" not in fields and "name" not in fields:
        raise ValueError("no id= or name= in the request")
    if fields.get("status") == "graveyard" and any(k in fields for k in POT_MAP_FIELDS):
        # Graveyarding is what UNWIRES a pot, so a body that does both at
        # once is asking for two opposite things. Asked of the request, not
        # of the merged row: graveyarding a pot that is wired right now is
        # the whole point and must go through.
        raise ValueError("a graveyard pot holds no wiring: send status=graveyard alone")
    return fields


def moisture_pct(raw: int, dry_raw: int | None, wet_raw: int | None) -> int | None:
    """Linear between the two calibration points, clamped to 0..100.

    None while uncalibrated. Works whichever way the sensor counts (dry
    high or dry low) because both endpoints are stored. Derived at read
    time and never stored: recalibrating reinterprets history.
    """
    if dry_raw is None or wet_raw is None or dry_raw == wet_raw:
        return None
    pct = (dry_raw - raw) * 100 / (dry_raw - wet_raw)
    return max(0, min(100, round(pct)))


def parse_approve(text: str) -> int:
    """The `POST /approve` body: `cmd=<id>`."""
    cmd_id = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "cmd":
            if cmd_id is not None:
                raise ValueError("cmd= given twice")
            cmd_id = _int_in(value, "cmd", 1, 2**63)
    if cmd_id is None:
        raise ValueError("no cmd= in the request")
    return cmd_id


def parse_verdict(text: str) -> tuple[int, str]:
    """The `POST /verdict` body: `cmd=<id> verdict=ok|too_much|too_little`."""
    cmd_id = None
    verdict = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "cmd":
            if cmd_id is not None:
                raise ValueError("cmd= given twice")
            cmd_id = _int_in(value, "cmd", 1, 2**63)
        elif key == "verdict":
            if verdict is not None:
                raise ValueError("verdict= given twice")
            if value not in VERDICT_VALUES:
                raise ValueError(f"verdict= must be one of {'|'.join(VERDICT_VALUES)}")
            verdict = value
    if cmd_id is None:
        raise ValueError("no cmd= in the request")
    if verdict is None:
        raise ValueError("no verdict= in the request")
    return cmd_id, verdict


ADVICE_KINDS = ("target",)


def parse_advice(text: str) -> tuple[str, str]:
    """The `POST /advice` body: `pot=<id> kind=target dismiss=1`.

    `dismiss=1` is spelled out rather than implied by the endpoint, so that
    an accept can never be a typo away: there is no accept here at all, and
    a body that asks for one is refused instead of quietly dismissing.
    """
    fields: dict = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        fields[key] = value
    pot_id = fields.get("pot")
    if not pot_id:
        raise ValueError("no pot= in the request")
    kind = fields.get("kind", "target")
    if kind not in ADVICE_KINDS:
        raise ValueError(f"kind= must be one of {'|'.join(ADVICE_KINDS)}")
    if fields.get("dismiss") != "1":
        raise ValueError("dismiss=1 is the only thing this endpoint does")
    return pot_id, kind


def parse_photo(params: QueryParams) -> tuple[str, int | None, int | None]:
    """`POST /photo?pot=<id>&w=&h=`, with the JPEG as the body.

    The one route here whose payload is not k=v, because it is bytes; the
    metadata rides the query string instead of a multipart envelope, which
    would be a dependency and a parser for one field and a file.

    `w`/`h` are what the phone says it downscaled to. They are a hint for
    laying out the strip before the bytes arrive, nothing more — no one
    here opens the JPEG to check, and nothing is decided from them.
    """

    def one(key: str) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else None

    pot = (one("pot") or "").strip()
    if not pot:
        raise ValueError("no pot= in the request")
    if not SAFE_ID.fullmatch(pot):
        raise ValueError(f"not a pot id: {pot!r}")

    def edge(key: str) -> int | None:
        raw = one(key)
        return None if raw is None else _int_in(raw, key, 1, MAX_PHOTO_EDGE)

    return pot, edge("w"), edge("h")


def parse_photos(params: QueryParams) -> tuple[str, int]:
    """`GET /photos?pot=<id>&limit=<1..500>`: one pot's strip, newest first.

    A pot is required, unlike /doses. A garden-wide roll of photographs is
    a gallery, and the pitch is a pot carrying its own growth history.
    """

    def one(key: str, default: str | None = None) -> str | None:
        values = params.getlist(key)
        if len(values) > 1:
            raise ValueError(f"{key}= given twice")
        return values[0] if values else default

    pot = (one("pot") or "").strip()
    if not pot:
        raise ValueError("no pot= in the request")
    if not SAFE_ID.fullmatch(pot):
        raise ValueError(f"not a pot id: {pot!r}")
    # +1 like every other bound in this file (DOSES_MAX, MAX_CHANNEL,
    # MAX_CAP_S): _int_in's top is exclusive, and the named max is meant to
    # be a limit somebody can actually ask for.
    return pot, _int_in(one("limit", str(PHOTO_LIMIT)) or "", "limit", 1, MAX_PHOTO_LIMIT + 1)


def parse_photo_delete(text: str) -> str:
    """The `POST /photo/delete` body: `photo=<id>`.

    Its own route rather than a `delete=1` field on /photo, because /photo
    carries a picture and this one must never be reachable by an upload
    that lost its body.
    """
    fields: dict = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        fields[key] = value
    photo_id = fields.get("photo")
    if not photo_id:
        raise ValueError("no photo= in the request")
    if not SAFE_ID.fullmatch(photo_id):
        raise ValueError(f"not a photo id: {photo_id!r}")
    return photo_id


def parse_pot_delete(text: str) -> str:
    """The `POST /pot/delete` body: `id=<pot id>`.

    Its own route rather than a field on /pot, for the same reason and a
    louder one: a save that lost its body must never become an erasure.
    SAFE_ID is not decoration here — the delete turns this id into the
    directory `photos/<pot id>/` and removes it.
    """
    fields: dict = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        fields[key] = value
    pot_id = fields.get("id")
    if not pot_id:
        raise ValueError("no id= in the request")
    if not SAFE_ID.fullmatch(pot_id):
        raise ValueError(f"not a pot id: {pot_id!r}")
    return pot_id


def parse_quiet(text: str) -> tuple[int, int]:
    """BUTLER_QUIET, `HH-HH` in the server's local time; `0-0` disables.

    The container runs UTC unless TZ is set — set TZ in the deployment or
    the quiet window is quiet somewhere else.
    """
    start, sep, end = text.partition("-")
    ok = sep and start.isascii() and start.isdigit() and end.isascii() and end.isdigit()
    if not ok:
        raise ValueError(f"BUTLER_QUIET must be HH-HH, got {text!r}")
    s, e = int(start), int(end)
    if not (0 <= s <= 23 and 0 <= e <= 23):
        raise ValueError(f"BUTLER_QUIET hours out of range: {text}")
    return s, e


def in_quiet(hour: int, start: int, end: int) -> bool:
    """Whether `hour` falls in the quiet window; start == end means never."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# --- What does this plant want? (cycle 2) --------------------------------
#
# Two hops, both cached, and neither of them a source of watering numbers.
# GBIF turns whatever somebody typed into the accepted binomial: free, no
# key, and it is what lets "Sansevieria trifasciata" find the plant now
# filed under "Dracaena trifasciata". Trefle then answers about that
# binomial, when it knows it at all.
#
# The target band comes from target_band() below and from nowhere else.
# Trefle carries no watering regime: probed with a real key on 2026-09-04,
# `soil_humidity` was NULL for every species asked, houseplants included.
# What it does carry — light and atmospheric humidity on its own 0-10
# scales — is context for a human, not an input to a number.

GBIF_MATCH_URL = "https://api.gbif.org/v1/species/match"
TREFLE_BASE = "https://trefle.io/api/v1"
CARE_TIMEOUT_S = 6  # three hops worst case, so a lookup answers inside ~20 s
CARE_MISS_TTL_S = 30 * 86400  # a complete hit is kept forever; anything else
#                               is re-asked monthly (see taxon_for)
SPECIES_MAX = 120  # longer than any binomial; bounds what goes on the wire
CARE_BODY_CAP = 1 << 20  # a species page is ~40 KB; never read a stream


def fetch_json(url: str) -> dict | None:
    """One GET, parsed as JSON. None for everything else.

    A care source that is down, slow, rate-limiting, redirecting or
    answering HTML is a normal Tuesday, and the caller's answer is the same
    in every one of those cases: nothing is known about this plant. Never
    raises — a lookup must not be able to take the service down.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "plantbutler-backend"}
    )
    try:
        with _OPENER.open(request, timeout=CARE_TIMEOUT_S) as answer:
            if not 200 <= answer.status < 300:
                return None
            body = answer.read(CARE_BODY_CAP)
        parsed = json.loads(body.decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalise_species(text: str) -> str:
    """What both caches are keyed on: underscores back to spaces (a k=v
    token cannot hold one), collapsed whitespace, lowercased."""
    return " ".join(text.replace("_", " ").split()).lower()


def binomial_case(query: str) -> str:
    """What GBIF is asked, which is not what the cache is keyed on.

    GBIF matches a lowercase binomial happily but a lowercase genus not at
    all: `monstera` answers NONE while `Monstera` answers GENUS. So the key
    stays lowercase, and the question goes out in botanical case — genus
    capitalised, epithet not.
    """
    return query[:1].upper() + query[1:]


class Taxon(NamedTuple):
    accepted: str | None  # the binomial to ask about; None when unresolved
    rank: str | None
    matched: str  # exact | fuzzy | genus | none
    family: str | None = None  # what the plant-kind guess is read from


def read_gbif(payload: dict) -> Taxon:
    """GBIF's match answer, read defensively.

    Two traps. A `matchType` of NONE arrives with `confidence: 100`, so
    confidence says nothing on its own and the match type is the only field
    worth believing. And a name that resolves to a genus has no `species`
    at all — inventing one from it would mint exactly the junk cache row the
    taxonomy hop exists to prevent, so a genus resolves to nothing and says
    so, for the screen to ask which species.
    """
    kind = str(payload.get("matchType") or "NONE").upper()
    if kind not in ("EXACT", "FUZZY"):
        return Taxon(None, None, "none")
    if str(payload.get("kingdom") or "") != "Plantae":
        # A plant name that matches an animal is a wrong hop, not a hit.
        return Taxon(None, None, "none")
    rank = str(payload.get("rank") or "").upper() or None
    # `species` is the ACCEPTED name even when the typing was a synonym,
    # which is the entire reason this hop is here.
    accepted = payload.get("species")
    if not isinstance(accepted, str) or not accepted.strip():
        return Taxon(None, rank, "genus" if rank == "GENUS" else "none")
    family = payload.get("family")
    return Taxon(
        accepted.strip(),
        rank,
        "fuzzy" if kind == "FUZZY" else "exact",
        family.strip() if isinstance(family, str) and family.strip() else None,
    )


def pick_species(payload: dict, wanted: str) -> str | None:
    """The slug for `wanted` in a Trefle search answer, or None.

    Trefle's search is fuzzy and ranks by its own relevance: asking for
    Ocimum basilicum also returns Basilicum polystachyon, and a query it
    knows nothing about still returns whatever was nearest. Only an exact
    binomial is this plant; anything else is a different one.
    """
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        if normalise_species(str(row.get("scientific_name") or "")) == wanted:
            slug = str(row.get("slug") or "").strip()
            return slug or None
    return None


def _scale(value: object, low: float, high: float) -> float | None:
    """A number inside [low, high], or None. Booleans are not numbers here
    (True would otherwise read as a light level of 1)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if low <= value <= high else None


CARE_KEYS = (
    "common_name",
    "light",
    "humidity",
    "ph_min",
    "ph_max",
    "temp_min_c",
    "image_url",
)


def read_trefle(payload: dict) -> dict:
    """The handful of fields worth keeping from a Trefle species page.

    Everything is optional and most of it is usually absent — that is the
    normal case, not a failure. `image_url` is only kept when it is https:
    the app loads it, and a plaintext or javascript: URL from a third party
    has no business being handed to a WebView.
    """
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    growth = data.get("growth")
    growth = growth if isinstance(growth, dict) else {}
    temp = growth.get("minimum_temperature")
    temp = temp if isinstance(temp, dict) else {}
    image = str(data.get("image_url") or "")
    light = _scale(growth.get("light"), 0, 10)
    humidity = _scale(growth.get("atmospheric_humidity"), 0, 10)
    common = str(data.get("common_name") or "").strip()
    return {
        "common_name": common or None,
        "light": None if light is None else int(light),
        "humidity": None if humidity is None else int(humidity),
        "ph_min": _scale(growth.get("ph_minimum"), 0, 14),
        "ph_max": _scale(growth.get("ph_maximum"), 0, 14),
        "temp_min_c": _scale(temp.get("deg_c"), -90, 60),
        "image_url": image if image.startswith("https://") else None,
    }


CANDIDATES_MAX = 8  # a screenful of pictures; the rest are worse matches
CANDIDATE_KEYS = ("name", "common", "image", "slug")


def loose(text: str) -> str:
    """Looser than the cache key: hyphens are spaces too. Trefle spells the
    same plant "Peace lily" and "Peace-lily" in adjacent rows, and neither
    spelling is what anybody types."""
    return normalise_species(text.replace("-", " "))


def read_candidates(payload: dict) -> list[dict]:
    """A Trefle search answer as a shortlist to show somebody.

    Species only: Trefle returns varieties and subspecies alongside, and
    "Solanum lycopersicum var. lycopersicum" is a worse answer to "tomato"
    than the species is. The picture comes from the search itself, so a
    shortlist of eight costs one HTTP call rather than nine.
    """
    out = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("rank") or "species") != "species":
            continue
        name = str(row.get("scientific_name") or "").strip()
        slug = str(row.get("slug") or "").strip()
        if not name or not slug:
            continue
        image = str(row.get("image_url") or "")
        common = str(row.get("common_name") or "").strip()
        out.append(
            {
                "name": name,
                "common": common or None,
                # The phone loads this. A plaintext or javascript: URL from
                # a third party has no business being handed to it.
                "image": image if image.startswith("https://") else None,
                "slug": slug,
            }
        )
        if len(out) == CANDIDATES_MAX:
            break
    return out


def sole_match(candidates: list[dict], query: str) -> str | None:
    """The one candidate whose common name IS what was typed, or None.

    "basil" has exactly one Basil among its Basil thymes and African basils,
    so that is not a guess and can be followed. "peace lily" has two, spelt
    "Peace lily" and "Peace-lily", and picking either would be inventing an
    answer — two pictures and a question is the honest response there.
    """
    wanted = loose(query)
    hits = {c["name"] for c in candidates if c["common"] and loose(c["common"]) == wanted}
    return hits.pop() if len(hits) == 1 else None


# The band is on OUR percentage — air is 0, tap water is 100, a straight
# line between two calibration points (decision #6). It is not volumetric
# water content, so no published figure could be copied into it even if a
# source had one. These are a starting offer for a human to correct, which
# is why nothing here is ever applied without one.

BASE_BAND = (35, 55)
BAND_FLOOR, BAND_CEIL, BAND_MIN_WIDTH = 5, 95, 10

# The closed set the form offers, and the band each kind starts from. A
# dropdown rather than free text because this is the biggest lever of the
# four: an unlabelled plant starts at 35-55 and a succulent at 15-30, so a
# typo here is a 20-point error that no pot measurement recovers. Reading
# stays tolerant — a value written before this set existed simply matches
# nothing and falls to the base band — but writing one is refused.
# Insertion order is the dropdown's order and the refusal message's order:
# driest first, so the list reads as the one axis it actually is.
PLANT_KINDS = {
    # A cactus is drier than the succulents it used to share a row with.
    # They were one entry while the field was free text and six words wide;
    # a dropdown can afford to tell them apart, and 5 points is the
    # difference between a barrel cactus and an echeveria.
    "cactus": (10, 25),
    "succulent": (15, 30),
    # An epiphyte in bark is not potted in anything that holds water. The
    # band is low because the medium is, not because the plant likes drought.
    "orchid": (20, 35),
    "mediterranean": (25, 45),  # rosemary, lavender, olive: woody and dry
    "bulb": (30, 50),  # rots wet, and dormant for half the year
    "flower": (35, 50),
    "herb": (35, 55),
    "palm": (40, 55),
    "tropical": (40, 60),  # the leafy houseplants: aroids, marantas, figs
    "vegetable": (45, 65),
    "fern": (55, 75),
    "carnivorous": (70, 90),  # a bog plant: the one kind that wants it wet
}

# The soils that MOVE the band, and the phrase each one contributes to the
# reason. An ordinary potting mix is not here on purpose: it is the
# reference the plant bands are written against, so "not said" and "the bag
# from the shop" are the same answer and the list stays a list of movers.
#
# Every ceiling shift is <= 0, and that is an invariant rather than a
# coincidence. The band is only ever widened DOWNWARDS (see the squeeze at
# the end of target_band): a soil that raised the ceiling above the plant's
# own base would offer a wetter top than the kind allows, and contradict the
# reason printed beside it.
SOIL_SHIFTS = {
    "sphagnum": (10, 0, "sphagnum moss"),  # stays wet by design
    "peat": (5, 0, "peat soil"),  # what most nursery pots arrive in
    "clay": (0, -5, "clay soil"),  # holds water: the risk is the top
    "sandy": (-5, -5, "sandy soil"),  # drains before the sensor notices
    "perlite": (-5, -5, "perlite mix"),
    "cactus": (-8, -5, "cactus mix"),  # gritty, and meant to run dry
    "bark": (-10, -5, "bark mix"),  # orchid bark barely holds water at all
}

# Guessing the plant kind from the taxonomy GBIF hands back anyway, so the
# dropdown opens pre-selected. It is a guess and is treated as one: it fills
# the field only while the field is still empty, and one tap changes it.
# Being wrong costs a tap; being silent costs a 20-point band nobody
# noticed. Nothing here is ever written to a pot by the backend.
#
# Genus is asked before family, because family is wrong exactly where it
# matters most: Asparagaceae holds Dracaena fragrans, a leafy thing that
# wants watering, and Dracaena trifasciata, a succulent in all but name.
SPECIES_KINDS = {
    "dracaena trifasciata": "succulent",
    # Lamiaceae would call it a herb; it is a woody Mediterranean shrub and
    # wants a good deal less water than basil.
    "salvia rosmarinus": "mediterranean",
}
GENUS_KINDS = {
    "aloe": "succulent",
    "haworthia": "succulent",
    "gasteria": "succulent",
    "echeveria": "succulent",
    "sedum": "succulent",
    "kalanchoe": "succulent",
    "euphorbia": "succulent",  # the family is half spurges that are not
    "zamioculcas": "succulent",
    "sansevieria": "succulent",  # the name half the world still uses
    "peperomia": "succulent",  # thick leaves; Piperaceae is otherwise vines
    "schlumbergera": "tropical",  # a cactus that lives on a branch, not sand
    "citrus": "mediterranean",
    "lavandula": "mediterranean",
}
# Lowercased, because GBIF's case is not a promise. Orchidaceae used to be
# left out deliberately — an epiphyte on bark waters nothing like a flowering
# pot plant, and "not sure" beat 20 confident points in the wrong direction.
# There is an `orchid` band now, so it can be answered instead of dodged.
FAMILY_KINDS = {
    "cactaceae": "cactus",
    "crassulaceae": "succulent",
    "aizoaceae": "succulent",
    "asphodelaceae": "succulent",
    "didiereaceae": "succulent",
    "polypodiaceae": "fern",
    "dryopteridaceae": "fern",
    "pteridaceae": "fern",
    "nephrolepidaceae": "fern",
    "aspleniaceae": "fern",
    "athyriaceae": "fern",
    "lamiaceae": "herb",  # basil, mint, rosemary, thyme, sage, oregano
    "apiaceae": "herb",  # parsley, coriander, dill
    "solanaceae": "vegetable",
    "cucurbitaceae": "vegetable",
    "brassicaceae": "vegetable",
    "amaranthaceae": "vegetable",
    "araceae": "tropical",  # monstera, philodendron, pothos, peace lily
    "marantaceae": "tropical",
    "arecaceae": "palm",
    "musaceae": "tropical",
    "strelitziaceae": "tropical",
    "bromeliaceae": "tropical",
    "moraceae": "tropical",  # the figs
    "araliaceae": "tropical",
    "asparagaceae": "tropical",
    "asteraceae": "flower",
    "gesneriaceae": "flower",
    "rosaceae": "flower",
    "begoniaceae": "flower",
    "violaceae": "flower",
    "orchidaceae": "orchid",
    "droseraceae": "carnivorous",
    "nepenthaceae": "carnivorous",
    "sarraceniaceae": "carnivorous",
    "cephalotaceae": "carnivorous",
    "amaryllidaceae": "bulb",
    "iridaceae": "bulb",
    "liliaceae": "bulb",
    "oleaceae": "mediterranean",
    "cistaceae": "mediterranean",
    "rutaceae": "mediterranean",  # the citruses
}


def kind_for(accepted: str | None, family: str | None) -> str | None:
    """The plant kind to pre-select for a resolved name, or None.

    None is a real answer and the common one — an unlisted family means
    nobody here knows, and "not sure" already has correct behaviour.
    """
    if not accepted:
        return None
    name = normalise_species(accepted)
    if name in SPECIES_KINDS:
        return SPECIES_KINDS[name]
    genus = name.split(" ")[0]
    if genus in GENUS_KINDS:
        return GENUS_KINDS[genus]
    return FAMILY_KINDS.get(normalise_species(family or ""))


# What a measurement does to the band. Volume goes as the cube of the
# diameter, but the SHIFT cannot: a 40 cm pot holds 23x the water of a 14 cm
# one, and no band survives being multiplied by 23. What is linear in
# percentage points is the LOG of the volume — each doubling of buffer moves
# the band one step — so the cube arrives as the factor of 3 that log2 turns
# (d/d0)**3 into. The old small/large keywords sat at roughly +5/-5, which is
# what 10 cm and 24 cm still come out at.
POT_REF_CM = 14.0  # the pot the base bands assume
HEIGHT_REF_RATIO = 1.5  # a 21 cm plant in a 14 cm pot: neither tall nor short
BAND_PER_DOUBLING = 2.5  # percentage points per doubling
# Three doublings of volume is twice the diameter, so a pot of 28 cm or
# more moves the band as far as this model will take it. That is not a claim
# that 28 cm and 60 cm want the same water — it is where a table of a dozen
# plant kinds stops being worth extrapolating, and a bounded wrong answer
# beats an unbounded one.
POT_DOUBLINGS_CAP = 3.0  # +-7.5 points
HEIGHT_DOUBLINGS_CAP = 2.0  # +-5
SIZE_WHY_MIN = 0.5  # under half a point cannot move a whole-point band

# Northern hemisphere, because the flat this waters is in one. A pot in the
# southern half of the world wants these two swapped, and this code has no
# way of being told so — a wrong answer worth naming rather than hiding.
SEASONS = {12: "winter", 1: "winter", 2: "winter", 6: "summer", 7: "summer", 8: "summer"}
SEASON_SHIFTS = {"winter": (-10, -10), "summer": (5, 0)}


class Band(NamedTuple):
    low: int
    high: int
    why: str


def _doublings(ratio: float, cap: float) -> float:
    """log2 of a ratio, capped both ways. The cap is what keeps a fat-fingered
    200 cm pot from proposing a band nobody could water to."""
    return max(-cap, min(cap, math.log2(ratio)))


def size_shifts(
    diameter_cm: float | None, height_cm: float | None
) -> tuple[float, float, list[str]]:
    """What the two measurements do to the band, and the phrases to say so.

    Two independent effects, and both move the FLOOR far more than the
    ceiling. The pot is a water buffer: a small one runs out before anybody
    looks again, so its floor rises; a big one holds water around roots that
    rot, so its ceiling drops. The ceiling only ever drops, because no pot
    size is a reason to keep a plant wetter than its own kind wants — and
    lifting it would contradict #5 as well.

    The plant is the demand against that buffer, which is why height is read
    OVER diameter rather than on its own: 40 cm of basil is thirsty in a
    10 cm pot and comfortable in a 30 cm one. A height with no pot to
    measure against falls back to the reference pot, which is the same
    assumption the base bands already make.

    Zero and negative are treated as unsaid rather than refused here: the
    write path rejects them, but a row from before it did must not make the
    whole garden unreadable through a log of zero.
    """
    low = high = 0.0
    why: list[str] = []
    diameter = diameter_cm if diameter_cm and diameter_cm > 0 else None
    height = height_cm if height_cm and height_cm > 0 else None
    if diameter is not None:
        buffer_ratio = (diameter / POT_REF_CM) ** 3
        shift = -BAND_PER_DOUBLING * _doublings(buffer_ratio, POT_DOUBLINGS_CAP)
        low += shift
        high += min(shift, 0.0)
        if abs(shift) >= SIZE_WHY_MIN:
            why.append(f"{diameter:g} cm pot")
    if height is not None:
        against = diameter if diameter is not None else POT_REF_CM
        demand = (height / against) / HEIGHT_REF_RATIO
        shift = BAND_PER_DOUBLING * _doublings(demand, HEIGHT_DOUBLINGS_CAP)
        low += shift
        if abs(shift) >= SIZE_WHY_MIN:
            why.append(f"{height:g} cm plant")
    return low, high, why


def target_band(
    plant_type: str | None,
    soil: str | None,
    diameter_cm: float | None,
    height_cm: float | None,
    month: int,
) -> Band:
    """A target moisture band to offer, and the reason in words.

    The species is in here now, but only through the door marked plant kind:
    a lookup may pre-select that dropdown and a human may overrule it, and
    the band reads whatever the dropdown ends up saying. No care source
    reaches this function, because none of them carries a watering regime.
    What is left is what is actually on hand — what kind of plant it is,
    what it sits in, how big the pot is, how big the plant is, and the time
    of year — which is roughly what a person would use anyway.
    """
    base = PLANT_KINDS.get(plant_type or "", BASE_BAND)
    # Float from here down: three half-point shifts rounded as they land
    # are three points that vanish one at a time.
    low, high = float(base[0]), float(base[1])
    why = [plant_type if plant_type in PLANT_KINDS else "unlabelled plant"]
    shift = SOIL_SHIFTS.get(soil or "")
    if shift:
        low, high = low + shift[0], high + shift[1]
        why.append(shift[2])
    size_low, size_high, size_why = size_shifts(diameter_cm, height_cm)
    low, high = low + size_low, high + size_high
    why.extend(size_why)
    season = SEASONS.get(month)
    if season in SEASON_SHIFTS:
        shift = SEASON_SHIFTS[season]
        low, high = low + shift[0], high + shift[1]
        why.append(season)
    # Back to whole points once, at the end. Rounding each shift as it
    # landed would let three half-points vanish one at a time.
    low, high = round(low), round(high)
    # A band the shifts have squeezed shut is widened DOWNWARDS. Raising the
    # top instead would offer a wetter ceiling than the plant's own base —
    # a succulent in clay came out capped at 35% when its unmodified top is
    # 30% — and would contradict the reason printed beside it. Lowering the
    # floor errs dry, which is the direction #5 asks for.
    low = min(low, high - BAND_MIN_WIDTH)
    low = max(BAND_FLOOR, min(BAND_CEIL - BAND_MIN_WIDTH, low))
    high = max(low + BAND_MIN_WIDTH, min(BAND_CEIL, high))
    return Band(low, high, ", ".join(why))


def create_app(
    db_path: str | None = None,
    token: str | None = None,
    next_s: int | None = None,
    cmd_ttl_s: int | None = None,
    quiet: str | None = None,
    ntfy_topic: str | None = None,
    ntfy_url: str | None = None,
    deadman_url: str | None = None,
    silent_s: int | None = None,
    tick_s: float | None = None,
    send: Callable[[Alert], bool] | None = None,
    ping: Callable[[], bool] | None = None,
    probe: Callable[[], bool] | None = None,
    trefle_token: str | None = None,
    fetch: Callable[[str], dict | None] | None = None,
    photos_dir: str | None = None,
) -> FastAPI:
    """Everything configurable comes from the environment, overridable for tests.

    Refusals to start, all of them loud and specific: a missing token (this
    listens on a LAN with other people's devices on it, and "forgot to set the
    token" must not be a working deployment); a BUTLER_NEXT_S or
    BUTLER_CMD_TTL_S that is not an integer (the alternative is a
    crash-looping container with a bare traceback); a BUTLER_DB under /data
    when /data is not actually a mount (a forgotten bind mount would store
    readings in the container's own layer and lose them all on the next
    recreate, while looking perfectly healthy).
    """
    db = Path(db_path or os.environ.get("BUTLER_DB", "/data/butler.db"))
    secret = token if token is not None else os.environ.get("BUTLER_TOKEN", "")
    if not secret:
        raise ValueError("BUTLER_TOKEN is not set; refusing to serve without one")

    def env_int(given: int | None, name: str, default: str) -> int:
        raw = str(given) if given is not None else (os.environ.get(name) or default)
        try:
            return int(raw)
        except ValueError:
            raise ValueError(
                f"{name} must be an integer number of seconds, got {raw!r}"
            ) from None

    interval = env_int(next_s, "BUTLER_NEXT_S", "60")
    cmd_ttl = env_int(cmd_ttl_s, "BUTLER_CMD_TTL_S", "900")
    if not MIN_NEXT_S <= interval <= MAX_NEXT_S:
        raise ValueError(
            f"BUTLER_NEXT_S out of range ({MIN_NEXT_S}..{MAX_NEXT_S}): {interval}"
        )
    if cmd_ttl < 2 * interval:
        # The TTL backstops are only safe if a live board always reports well
        # within the TTL; otherwise a 'sent' command can be swept aside and a
        # second one queued while the board still holds the first — two doses.
        raise ValueError(
            f"BUTLER_CMD_TTL_S ({cmd_ttl}) must be at least twice "
            f"BUTLER_NEXT_S ({interval}), or a live board could be declared "
            "dead between two on-time reports"
        )

    quiet_window = parse_quiet(
        quiet if quiet is not None else os.environ.get("BUTLER_QUIET") or "22-08"
    )

    topic = (
        ntfy_topic
        if ntfy_topic is not None
        else os.environ.get("BUTLER_NTFY_TOPIC", "")
    )
    base_url = (
        ntfy_url
        if ntfy_url is not None
        else (os.environ.get("BUTLER_NTFY_URL") or "https://ntfy.sh")
    )
    deadman = (
        deadman_url
        if deadman_url is not None
        else os.environ.get("BUTLER_DEADMAN_URL", "")
    )
    silent_after = env_int(silent_s, "BUTLER_SILENT_S", str(SILENT_AFTER_S))
    if not 60 <= silent_after <= 86400:
        raise ValueError(f"BUTLER_SILENT_S out of range (60..86400): {silent_after}")
    beat = tick_s if tick_s is not None else ALERT_TICK_S
    alerts_on = bool(topic) or send is not None
    if deadman and not alerts_on:
        raise ValueError(
            "BUTLER_DEADMAN_URL is set but BUTLER_NTFY_TOPIC is not: the "
            "dead-man would report a healthy butler whose alerting is off"
        )
    check = probe
    if send is None and topic:

        def send(alert: Alert) -> bool:
            return post_ntfy(base_url, topic, alert)

        if check is None:

            def check() -> bool:
                # Reachability for quiet passes: a healthy garden sends no
                # messages, so without this an ntfy outage would never stop
                # the dead-man.
                return ping_deadman(f"{base_url.rstrip('/')}/v1/health")

    if ping is None and deadman:

        def ping() -> bool:
            return ping_deadman(deadman)

    if not alerts_on:
        print("BUTLER_NTFY_TOPIC unset: alerts are off", file=sys.stderr)

    care_token = (
        trefle_token
        if trefle_token is not None
        else os.environ.get("BUTLER_TREFLE_TOKEN", "")
    )
    get_json = fetch or fetch_json
    if not care_token and fetch is None:
        print("BUTLER_TREFLE_TOKEN unset: care lookups are typed in", file=sys.stderr)

    if db.parent == Path("/data") and not os.path.ismount("/data"):
        raise ValueError(
            "BUTLER_DB is under /data but /data is not a mounted volume; "
            "refusing to store readings in the container layer"
        )
    # The photographs sit beside the database by default, so they land on
    # the same bind mount and are backed up or lost together — the one
    # arrangement in which a restore cannot produce rows whose files are
    # from a different day. BUTLER_PHOTOS can move them, and gets the same
    # refusal the database gets if it points into an unmounted /data.
    photos = Path(
        photos_dir or os.environ.get("BUTLER_PHOTOS") or str(db.parent / "photos")
    )
    if photos.parent == Path("/data") and not os.path.ismount("/data"):
        raise ValueError(
            "BUTLER_PHOTOS is under /data but /data is not a mounted volume; "
            "refusing to store photographs in the container layer"
        )

    db.parent.mkdir(parents=True, exist_ok=True)
    photos.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as bootstrap:
        bootstrap.execute("PRAGMA journal_mode=WAL")
        # Before the script: it recreates `pots_now`, and a view over a
        # column its table has not got yet fails on every read, not here.
        grew = add_columns(bootstrap)
        if grew:
            print("added columns: " + ", ".join(grew), file=sys.stderr)
        bootstrap.executescript(SCHEMA_SQL)
        # After the script, never before: a genuinely fresh database is
        # already in the new shape, so migrate() sees no `controller`
        # column on pots and returns immediately.
        migrate(bootstrap, str(db))

    def connect() -> sqlite3.Connection:
        con = sqlite3.connect(db, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def photo_path(pot_id: str, photo_id: str) -> Path:
        """Where one picture's bytes live. One directory per pot, so the
        volume stays readable by a person with a file browser and a backup
        of one pot is a directory.

        Both halves are re-checked here rather than trusted from wherever
        they came: this is the only function that turns an id into a path,
        so it is the only place a traversal could get in.
        """
        if not SAFE_ID.fullmatch(pot_id) or not SAFE_ID.fullmatch(photo_id):
            raise ValueError("not an id")
        return photos / pot_id / f"{photo_id}.jpg"

    def keep_photo(
        pot_id: str, blob: bytes, w: int | None, h: int | None, now: int
    ) -> str:
        """The bytes, then the row.

        A crash between the two leaves a file no row knows about, which
        nothing lists and nothing serves; the other order would leave a row
        whose picture never existed and which the strip would have to show
        as missing forever. Neither connection is held across the disk
        write — a photograph is megabytes over a NAS volume, and a write
        transaction held that long is the board's reports blocked.

        The id is claimed by creating its file exclusively, and a taken one
        is simply tried again. Overwriting the file first and finding out
        from the INSERT would destroy the picture already at that path — an
        earlier photograph, already committed, whose row would then be left
        pointing at nothing. Two ids can collide in two ways: the file is
        there, or only the row is (a photograph whose file was lost), and
        both have to fall out the same way.
        """
        with connect() as con:
            row = con.execute(
                "SELECT species FROM pots WHERE id = ?", (pot_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"no such pot: {pot_id}")
        for _ in range(PHOTO_ID_TRIES):
            photo_id = new_photo_id()
            path = photo_path(pot_id, photo_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                write_new_file(path, blob)
            except FileExistsError:
                continue
            try:
                with connect() as con:
                    con.execute(
                        "INSERT INTO photos (id, pot_id, ts, bytes, w, h, species) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (photo_id, pot_id, now, len(blob), w, h, row[0]),
                    )
            except sqlite3.IntegrityError:
                # The row is there and its file was not: the id belongs to
                # a photograph whose bytes were lost. Leave that row alone
                # and take another id.
                path.unlink(missing_ok=True)
                continue
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return photo_id
        raise sqlite3.IntegrityError(
            f"could not mint a free photograph id in {PHOTO_ID_TRIES} tries"
        )

    def photo_rows(pot_id: str, limit: int) -> list[dict]:
        """One pot's strip, newest first, straight from the rows.

        `missing` is the one thing the disk is asked: a row whose file has
        gone — a half-restored backup, a volume that came back empty — is
        listed and said to be missing rather than served as a picture that
        will not load.
        """
        with connect() as con:
            rows = con.execute(
                "SELECT id, ts, bytes, w, h, species FROM photos "
                "WHERE pot_id = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (pot_id, limit),
            ).fetchall()
        return [
            {
                "id": photo_id,
                "ts": ts,
                "bytes": size,
                "w": w,
                "h": h,
                "species": species,
                "missing": not photo_path(pot_id, photo_id).exists(),
            }
            for photo_id, ts, size, w, h, species in rows
        ]

    def photo_blob(photo_id: str) -> bytes:
        """The picture itself, found through its row and never through the
        directory: a file nothing here minted is not reachable by guessing
        its name."""
        with connect() as con:
            row = con.execute(
                "SELECT pot_id FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"no such photo: {photo_id}")
        try:
            return photo_path(row[0], photo_id).read_bytes()
        except OSError:
            raise ValueError(f"{photo_id} is listed but its file is gone") from None

    def forget_photo(photo_id: str) -> None:
        """The row, then the file — the opposite order to keeping one, and
        for the same reason: whichever way a crash lands, what is left over
        is a file nobody knows about rather than a row nobody can show. The
        person said the picture is gone, so it goes from the listing even
        if the volume refuses to give up the bytes."""
        with connect() as con:
            row = con.execute(
                "SELECT pot_id FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no such photo: {photo_id}")
            # The DELETE decides, not the SELECT before it. Two deletes of
            # one photograph can both see the row — a bare SELECT takes no
            # lock — and only the one that actually removed it may answer
            # ok. Otherwise "deleting twice is refused rather than
            # pretended" would hold only when nobody is in a hurry.
            if con.execute("DELETE FROM photos WHERE id = ?", (photo_id,)).rowcount == 0:
                raise ValueError(f"no such photo: {photo_id}")
        with contextlib.suppress(OSError):
            photo_path(row[0], photo_id).unlink(missing_ok=True)

    def taxon_for(query: str, now: int) -> Taxon | None:
        """The accepted binomial for what somebody typed, cached.

        None means the name service could not be asked — which is not the
        same as "no such plant" and must not be written down as one.

        No database connection is held across the fetch, here or below. The
        first write in a connection opens sqlite's write transaction, and
        holding one for the length of three HTTP timeouts would make every
        board report in that window answer "try again": somebody typing a
        plant's name must not be able to stop the garden reporting.
        """
        with connect() as con:
            row = con.execute(
                "SELECT accepted, rank, matched, fetched_ts, family "
                "FROM species_names WHERE query = ?",
                (query,),
            ).fetchone()
        # A hit is kept for ever only when it is COMPLETE. A row that
        # resolved a name but carries no family is a row that can suggest no
        # plant kind, and a cache hit never re-asks — so without this it
        # would suggest nothing for the life of the database. Rows written
        # before `family` existed are the loud case; a genus-level answer is
        # the ordinary one. Re-asking is TTL-gated, so a name GBIF really has
        # no family for costs one call a month, not one per screen open.
        fresh = row and now - row[3] < CARE_MISS_TTL_S
        complete = row and row[0] is not None and row[4] is not None
        if row and (complete or fresh):
            return Taxon(row[0], row[1], row[2], row[4])
        payload = get_json(
            f"{GBIF_MATCH_URL}?{urlencode({'name': binomial_case(query)})}"
        )
        if payload is None:
            # A re-ask that cannot reach GBIF must not turn a name that
            # resolved yesterday into "the lookup is not answering".
            return Taxon(row[0], row[1], row[2], row[4]) if row and row[0] else None
        taxon = read_gbif(payload)
        with connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO species_names "
                "(query, fetched_ts, accepted, rank, matched, family) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (query, now, taxon.accepted, taxon.rank, taxon.matched, taxon.family),
            )
        return taxon

    def cached_care(con: sqlite3.Connection, key: str) -> dict | None:
        row = con.execute(
            "SELECT fetched_ts, source, found, "
            f"{', '.join(CARE_KEYS)} FROM species_care WHERE species = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        entry = {"fetched": row[0], "source": row[1], "found": bool(row[2])}
        entry.update(zip(CARE_KEYS, row[3:]))
        return entry

    def care_for(accepted: str, now: int) -> dict | None:
        """What the care source says about one binomial, cached.

        A miss is cached too — Trefle's houseplant coverage is empty, not
        thin, so "nothing known" is the ordinary answer and re-asking it on
        every screen open would be the bug. None means it could not be
        asked at all: no token configured, or the source is not answering.
        """
        key = normalise_species(accepted)
        with connect() as con:
            entry = cached_care(con, key)
        if entry and (entry["found"] or now - entry["fetched"] < CARE_MISS_TTL_S):
            return entry
        if not care_token:
            return None
        found = get_json(
            f"{TREFLE_BASE}/species/search?"
            f"{urlencode({'q': accepted, 'token': care_token})}"
        )
        if found is None:
            return None
        slug = pick_species(found, key)
        care = dict.fromkeys(CARE_KEYS)
        if slug:
            detail = get_json(
                f"{TREFLE_BASE}/species/{quote(slug, safe='')}?"
                f"{urlencode({'token': care_token})}"
            )
            if detail is None:
                return None
            care = read_trefle(detail)
        with connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO species_care "
                f"(species, fetched_ts, source, found, {', '.join(CARE_KEYS)}) "
                f"VALUES (?, ?, 'trefle', ?, {', '.join('?' * len(CARE_KEYS))})",
                (key, now, int(bool(slug)), *(care[k] for k in CARE_KEYS)),
            )
        return {"fetched": now, "source": "trefle", "found": bool(slug), **care}

    def search_for(query: str, now: int) -> list[dict]:
        """Trefle's own search on what was typed, cached.

        This is the fuzzy half. GBIF only knows scientific names, so "basil",
        "basilico" and "tomatoe" resolve to nothing there; Trefle's search
        matches common names, survives a typo, and its rows already carry a
        picture — which is what lets somebody confirm by eye rather than by
        spelling. An empty list is both "nothing found" and "could not ask":
        the screen is the same either way, a list with nothing in it.
        """
        with connect() as con:
            row = con.execute(
                "SELECT fetched_ts, candidates FROM species_search WHERE query = ?",
                (query,),
            ).fetchone()
        if row:
            cached = json.loads(row[1])
            if cached or now - row[0] < CARE_MISS_TTL_S:
                return cached
        if not care_token:
            return []
        payload = get_json(
            f"{TREFLE_BASE}/species/search?"
            f"{urlencode({'q': query, 'token': care_token})}"
        )
        if payload is None:
            return []
        candidates = read_candidates(payload)
        with connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO species_search "
                "(query, fetched_ts, candidates) VALUES (?, ?, ?)",
                (query, now, json.dumps(candidates)),
            )
        return candidates

    def care_note(accepted: str, matched: str, care: dict) -> str:
        if care["light"] is None and care["humidity"] is None:
            note = f"Trefle knows {accepted} but has no numbers for it"
        else:
            note = f"Trefle: {accepted}"
        return f"read as {accepted}. {note}" if matched == "fuzzy" else note

    def miss_note(answer: dict) -> str:
        if answer["candidates"]:
            return "not sure which one — pick the plant you recognise"
        if answer["matched"] == "unavailable":
            return "the lookup is not answering — type the numbers in"
        if answer["matched"] == "genus":
            return "that is a genus — which species?"
        if answer["accepted"] is None:
            return "no plant of that name — check the spelling, or type the numbers in"
        if answer["care"] is None:
            return "no care source configured or answering — type the numbers in"
        return f"{answer['accepted']} is not in Trefle — type the numbers in"

    def look_up(query: str, depth: int = 0) -> dict:
        """One species lookup, and a sentence saying what came of it.

        Three ways in, in order of how much they can be trusted. GBIF on the
        typing resolves a scientific name, corrects a typo in one, and
        redirects a synonym to the name the plant was renamed to. Failing
        that, Trefle's search takes the typing as a common name and answers
        with pictures. And if exactly one of those pictures is called what
        was typed, that is not a guess and is followed.

        Every unhappy path ends in a working screen: the numbers are typed
        in, which is what happens for most houseplants anyway.
        """
        now = int(time.time())
        taxon = taxon_for(query, now)
        answer = {
            "query": query,
            "matched": "unavailable",
            "accepted": None,
            "rank": None,
            "kind": None,
            "care": None,
            "candidates": [],
            "note": "",
        }
        if taxon is not None:
            answer["matched"] = taxon.matched
            answer["accepted"] = taxon.accepted
            answer["rank"] = taxon.rank
            answer["kind"] = kind_for(taxon.accepted, taxon.family)
            if taxon.accepted:
                answer["care"] = care_for(taxon.accepted, now)
                care = answer["care"]
                if care is not None and care["found"]:
                    answer["note"] = care_note(taxon.accepted, taxon.matched, care)
                    return answer
                # A name GBIF resolved and Trefle has never heard of is a
                # finished answer, not a reason to go offering other plants:
                # the shortlist is for a name nobody could place at all.
                answer["note"] = miss_note(answer)
                return answer
        candidates = search_for(query, now)
        pick = sole_match(candidates, query)
        if pick and depth == 0 and normalise_species(pick) != query:
            deeper = look_up(normalise_species(pick), depth + 1)
            if deeper["accepted"]:
                deeper["query"] = query
                deeper["matched"] = "common"
                return deeper
        answer["candidates"] = candidates
        answer["note"] = miss_note(answer)
        return answer

    def advice_for(con: sqlite3.Connection, entry: dict, now: int) -> dict | None:
        """The band this pot would be offered, or None when there is nothing
        to say: the pot is off, it already holds those numbers, or the
        person has already refused this exact offer. A different offer — a
        new season, a repot, another soil — is a new question and is asked.
        """
        if not waters(entry["status"]):
            return None
        band = target_band(
            entry["plant_type"],
            entry["soil"],
            entry["pot_diameter_cm"],
            entry["plant_height_cm"],
            time.localtime(now).tm_mon,
        )
        if (entry["target_low_pct"], entry["target_high_pct"]) == (band.low, band.high):
            return None
        row = con.execute(
            "SELECT fingerprint FROM advice_dismissed WHERE pot_id = ? AND kind = ?",
            (entry["id"], "target"),
        ).fetchone()
        if row and row[0] == f"{band.low}-{band.high}":
            return None
        return {"kind": "target", "low": band.low, "high": band.high, "why": band.why}

    def dismiss_advice(pot_id: str, kind: str) -> None:
        now = int(time.time())
        with connect() as con:
            row = con.execute(
                "SELECT id, plant_type, soil, pot_diameter_cm, plant_height_cm, "
                "target_low_pct, target_high_pct FROM pots_now WHERE id = ?",
                (pot_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no pot {pot_id}")
            band = target_band(
                row[1], row[2], row[3], row[4], time.localtime(now).tm_mon
            )
            con.execute(
                "INSERT OR REPLACE INTO advice_dismissed "
                "(pot_id, kind, fingerprint, ts) VALUES (?, ?, ?, ?)",
                (pot_id, kind, f"{band.low}-{band.high}", now),
            )

    def water_rules(con: sqlite3.Connection, r: Report, now: int) -> None:
        """The ladder from the design sketch, statelessly, inside the
        report's own transaction. Median over the last RULES_WINDOW
        readings is both the smoothing and the consecutive-dry test: a
        dry median of five means most of the window was dry. Every gate
        errs dry; a skipped pot is retried on the next report for free.
        """
        con.execute(
            "UPDATE commands SET state = 'expired' "
            "WHERE controller = ? AND state = 'proposed' AND created_ts < ?",
            (r.controller, now - PROPOSAL_TTL_S),
        )
        if r.float_ok != 1 or r.pos != "ok":
            return  # no reservoir, no known position, no report field: dry
        # This board's own beat, so "recent" below means the same number of
        # reports whether it speaks every minute or every hour.
        beat = con.execute(
            "SELECT next_s FROM controllers WHERE controller = ?", (r.controller,)
        ).fetchone()
        cadence = (beat and beat[0]) or interval
        if in_quiet(time.localtime(now).tm_hour, *quiet_window):
            return
        candidates = con.execute(
            "SELECT id, channel, outlet, dry_raw, wet_raw, target_low_pct, "
            "dose_ml, mode, cooldown_h, daily_cap_ml FROM pots_now "
            f"WHERE {live_sql()} AND mode IN ('learning', 'auto') "
            "AND controller = ? AND channel IS NOT NULL AND outlet IS NOT NULL "
            "AND dry_raw IS NOT NULL AND wet_raw IS NOT NULL "
            "AND target_low_pct IS NOT NULL AND dose_ml IS NOT NULL "
            "ORDER BY name",
            (r.controller,),
        ).fetchall()
        for (
            pot_id,
            channel,
            outlet,
            dry,
            wet,
            low,
            dose,
            mode,
            cool_h,
            cap_ml,
        ) in candidates:
            if channel not in r.channels:
                # A sensor that went silent errs dry, exactly like a missing
                # float=: without it the window would freeze on stale values
                # and water the pot at cooldown pace forever.
                continue
            # THIS pot's readings, not this channel's. A socket that has
            # just changed hands still holds the last plant's rows, and four
            # of a dead plant's drought readings under one fresh one make a
            # median that opens a valve on a pot nobody has measured.
            #
            # And only recent ones, which the channel key gave for free and
            # the pot key does not: a pot rewired after a month would
            # otherwise decide on four month-old rows plus today's. Fewer
            # than RULES_WINDOW inside the window means it waits — dry.
            fresh = now - RULES_WINDOW * 3 * cadence
            window = [
                raw
                for (raw,) in con.execute(
                    "SELECT raw FROM readings "
                    "WHERE pot_id = ? AND ts >= ? "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (pot_id, fresh, RULES_WINDOW),
                )
            ]
            if len(window) < RULES_WINDOW:
                continue
            window.sort()  # median of raw == median of pct: the map is monotonic
            median_pct = moisture_pct(window[RULES_WINDOW // 2], dry, wet)
            if median_pct is None or median_pct >= low:
                continue
            # Keyed on the hose, and rightly so: this one asks whether the
            # plumbing is busy, not what this pot has had. The two gates
            # below ask about the pot, through its mapping windows — and
            # then about the hose anyway, as the floor no attribution
            # failure can dig under.
            open_cmd = con.execute(
                "SELECT 1 FROM commands WHERE controller = ? AND outlet = ? "
                "AND state IN ('proposed', 'queued', 'sent') LIMIT 1",
                (r.controller, outlet),
            ).fetchone()
            if open_cmd:
                continue
            # Cooldown counts from the last command the board ever HELD
            # (sent_ts set): an expired-unacked command may still have
            # watered, so it cools the pot just like an acked one. It
            # follows the pot when its hose moves — the six hours belong to
            # the plant, and a remap that reset them would water it twice.
            cooldown_s = (cool_h if cool_h is not None else DEFAULT_COOLDOWN_H) * 3600
            watered = con.execute(
                "SELECT 1 FROM commands WHERE pot_id = ? AND sent_ts IS NOT NULL "
                "AND COALESCE(acked_ts, sent_ts) > ? LIMIT 1",
                (pot_id, now - cooldown_s),
            ).fetchone() or con.execute(
                # ...and the hose underneath it. Attribution is a lookup and
                # a lookup can come back empty for reasons that say nothing
                # about the plant: a dose handed before the pot was ever
                # registered, a clock that stepped while the wiring was
                # saved. Water went down this hose either way, so the old
                # hose-keyed gate stays as the floor — decision #5, unknown
                # state waters LESS, never more.
                "SELECT 1 FROM commands WHERE controller = ? AND outlet = ? "
                "AND sent_ts IS NOT NULL AND COALESCE(acked_ts, sent_ts) > ? "
                "LIMIT 1",
                (r.controller, outlet, now - cooldown_s),
            ).fetchone()
            if watered:
                continue
            cap = cap_ml if cap_ml is not None else DEFAULT_DAILY_CAP_DOSES * dose
            # Acked water only. A handed command the board never acked is far
            # likelier a response that never arrived than an ack that was
            # lost — the firmware never retries once any response bytes came
            # back — and charging its full dose starved the pot for the day
            # on nothing. The cooldown above still counts it: spacing errs
            # dry, the cap counts water. (Jacopo, 2026-09-05.)
            #
            # One row, one owner, one SUM. This used to need a DISTINCT over
            # a join, because a dose handed in the very second of a remap sat
            # in both the window that closed and the one that opened; a
            # stamped row cannot be in two windows at once.
            (spent,) = con.execute(
                "SELECT COALESCE(SUM(CASE WHEN acked_ts IS NOT NULL "
                "THEN COALESCE(flow_ml, ml) ELSE 0 END), 0) FROM commands "
                "WHERE pot_id = ? AND sent_ts > ?",
                (pot_id, now - 86400),
            ).fetchone()
            # The same floor as the cooldown's, for the same reason: what
            # this HOSE poured in the last day, whoever it was attributed
            # to. MAX rather than a sum, because an attributed dose is
            # counted by both queries and must be spent once.
            (hose_spent,) = con.execute(
                "SELECT COALESCE(SUM(CASE WHEN acked_ts IS NOT NULL "
                "THEN COALESCE(flow_ml, ml) ELSE 0 END), 0) FROM commands "
                "WHERE controller = ? AND outlet = ? AND sent_ts > ?",
                (r.controller, outlet, now - 86400),
            ).fetchone()
            spent = max(spent, hose_spent)
            if spent + dose > cap:
                continue
            state = "proposed"
            if mode == "auto":
                slot_busy = con.execute(
                    "SELECT 1 FROM commands WHERE controller = ? "
                    "AND state IN ('queued', 'sent') LIMIT 1",
                    (r.controller,),
                ).fetchone()
                if slot_busy:
                    continue  # the next report retries; dry beats flooded
                state = "queued"
            cap_s = cap_for(dose)
            con.execute(
                "INSERT INTO commands (created_ts, controller, kind, outlet, "
                "ml, cap_s, state, source, pot_id) "
                "VALUES (?, ?, 'water', ?, ?, ?, ?, 'rules', ?)",
                (now, r.controller, outlet, dose, cap_s, state, pot_id),
            )

    def handle_report(r: Report) -> tuple[int, tuple | None]:
        """One report, one transaction: heartbeat, ack, expiries, dedup, the
        readings, the watering rules, and at most one command handed out —
        atomically, so two writers cannot hand the same command twice. A
        command the rules queue here rides out on this very response: the
        safety fields it was judged on are from this same report."""
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")  # writers serialize up front
            con.execute(
                "INSERT INTO controllers (controller, last_seen) VALUES (?, ?) "
                "ON CONFLICT(controller) DO UPDATE SET last_seen = excluded.last_seen",
                (r.controller, now),
            )
            # The latest safety fields, with enough history for the alert
            # rules: when each value last changed (`since`), when each was
            # last sent at all (`seen` — its vanishing is an alarm), and the
            # last two bad sightings (`bad`, `bad_prev` — a float flapping
            # at the waterline must still page). Plus the board's last error
            # (`err`, `err_ts`: a last-error field, left alone by a report
            # without one) and the last pos=ok ever seen (`pos_ok_seen`).
            # All SET expressions read the pre-update row, so the ordering
            # below is safe. latched_ts/latch_reason are deliberately not
            # here: the latch is set and cleared on its own, never through
            # this upsert, so no report can overwrite it.
            con.execute(
                "INSERT INTO status (controller, ts, float_ok, float_since, "
                "pos, pos_since, float_seen, pos_seen, float_bad, "
                "float_bad_prev, pos_bad, pos_bad_prev, err, err_ts, pos_ok_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(controller) DO UPDATE SET ts = excluded.ts, "
                "float_ok = excluded.float_ok, pos = excluded.pos, "
                "float_since = CASE WHEN status.float_ok IS excluded.float_ok "
                "THEN status.float_since ELSE excluded.ts END, "
                "pos_since = CASE WHEN status.pos IS excluded.pos "
                "THEN status.pos_since ELSE excluded.ts END, "
                "float_seen = CASE WHEN excluded.float_ok IS NOT NULL "
                "THEN excluded.ts ELSE status.float_seen END, "
                "pos_seen = CASE WHEN excluded.pos IS NOT NULL "
                "THEN excluded.ts ELSE status.pos_seen END, "
                "float_bad_prev = CASE WHEN excluded.float_ok = 0 "
                "THEN status.float_bad ELSE status.float_bad_prev END, "
                "float_bad = CASE WHEN excluded.float_ok = 0 "
                "THEN excluded.ts ELSE status.float_bad END, "
                "pos_bad_prev = CASE WHEN excluded.pos = 'unknown' "
                "THEN status.pos_bad ELSE status.pos_bad_prev END, "
                "pos_bad = CASE WHEN excluded.pos = 'unknown' "
                "THEN excluded.ts ELSE status.pos_bad END, "
                "err = COALESCE(excluded.err, status.err), "
                "err_ts = CASE WHEN excluded.err IS NOT NULL "
                "THEN excluded.ts ELSE status.err_ts END, "
                "pos_ok_seen = CASE WHEN excluded.pos = 'ok' "
                "THEN excluded.ts ELSE status.pos_ok_seen END",
                (
                    r.controller,
                    now,
                    r.float_ok,
                    now,
                    r.pos,
                    now,
                    now if r.float_ok is not None else None,
                    now if r.pos is not None else None,
                    now if r.float_ok == 0 else None,
                    now if r.pos == "unknown" else None,
                    r.err,
                    now if r.err is not None else None,
                    now if r.pos == "ok" else None,
                ),
            )
            if r.ack is not None:
                con.execute(
                    "UPDATE commands SET state = 'acked', acked_ts = ?, flow_ml = ? "
                    "WHERE id = ? AND controller = ? AND state = 'sent'",
                    (now, r.flow_ml, r.ack, r.controller),
                )
            # A command still 'sent' after the ack step was handed on an
            # earlier response and this report did not ack it: the board
            # does not have it. Gone, per the module docstring.
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE controller = ? AND state = 'sent'",
                (r.controller,),
            )
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE controller = ? AND state = 'queued' AND created_ts < ?",
                (r.controller, now - cmd_ttl),
            )
            duplicate = (
                r.t is not None
                and con.execute(
                    "SELECT 1 FROM readings "
                    "WHERE controller = ? AND t = ? AND ts >= ? LIMIT 1",
                    (r.controller, r.t, now - RETRY_WINDOW_S),
                ).fetchone()
            )
            if not duplicate:
                # Whose reading each channel is, resolved ONCE per report:
                # pot_mappings is not written inside this transaction, so it
                # cannot go stale between two channels of one report. A
                # channel nobody is mapped to stamps NULL — an environment
                # sensor, or a socket no plant has claimed — and that is a
                # real answer, not a gap to fill in later. upsert_pot keeps
                # at most one open window per (controller, channel), which
                # is what makes this dict unambiguous.
                owners = dict(
                    con.execute(
                        "SELECT channel, pot_id FROM pot_mappings "
                        "WHERE controller = ? AND to_ts IS NULL "
                        "AND channel IS NOT NULL",
                        (r.controller,),
                    )
                )
                con.executemany(
                    "INSERT INTO readings (ts, controller, channel, raw, t, pot_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (now, r.controller, ch, raw, r.t, owners.get(ch))
                        for ch, raw in sorted(r.channels.items())
                    ],
                )
                water_rules(con, r, now)
            handed = con.execute(
                "SELECT id, kind, outlet, ml, cap_s FROM commands "
                "WHERE controller = ? AND state = 'queued' ORDER BY id LIMIT 1",
                (r.controller,),
            ).fetchone()
            if handed:
                # The stamp is re-read HERE, not kept from when the command
                # was written. The board is handed an outlet, and the water
                # goes to whoever is on that outlet at this moment — which
                # is not always who was on it when the command was made:
                # a manual dose queued before the pot was registered was
                # stamped NULL, and a hose rearranged while a command waited
                # would file the water under the pot that no longer holds it.
                #
                # Both cost more than a wrong name. A dose the pot half of
                # the cooldown cannot see is a dose the HOSE floor stops
                # covering the moment that pot is rewired, so both layers of
                # DECISIONS #7 go at once and the plant is watered twice.
                owner = con.execute(
                    "SELECT pot_id FROM pot_mappings "
                    "WHERE controller = ? AND outlet IS ? AND to_ts IS NULL "
                    "ORDER BY rowid LIMIT 1",
                    (r.controller, handed[2]),
                ).fetchone()
                con.execute(
                    "UPDATE commands SET state = 'sent', sent_ts = ?, pot_id = ? "
                    "WHERE id = ?",
                    (now, owner and owner[0], handed[0]),
                )
            (override,) = con.execute(
                "SELECT next_s FROM controllers WHERE controller = ?",
                (r.controller,),
            ).fetchone()
            return (override or interval), handed

    def enqueue(c: Command) -> tuple[int, tuple | None]:
        """Fill the slot or report who holds it. The TTL backstop runs here
        too, so a dead board's abandoned command cannot wedge the slot."""
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE controller = ? AND state IN ('queued', 'sent') "
                "AND COALESCE(sent_ts, created_ts) < ?",
                (c.controller, now - cmd_ttl),
            )
            busy = con.execute(
                "SELECT id, state FROM commands "
                "WHERE controller = ? AND state IN ('queued', 'sent') LIMIT 1",
                (c.controller,),
            ).fetchone()
            if busy:
                return 0, busy
            # Whom this dose is for. A manual command names a hose, not a
            # pot, so the pot is the one on that hose right now — the same
            # answer the read-time join used to compute, decided once here
            # instead. A stop has no outlet and stamps NULL, and so does a
            # hose nobody is on.
            owner = (
                con.execute(
                    "SELECT pot_id FROM pot_mappings "
                    "WHERE controller = ? AND outlet = ? AND to_ts IS NULL "
                    "ORDER BY rowid LIMIT 1",
                    (c.controller, c.outlet),
                ).fetchone()
                if c.outlet is not None
                else None
            )
            (cmd_id,) = con.execute(
                "INSERT INTO commands (created_ts, controller, kind, outlet, "
                "ml, cap_s, state, source, pot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', 'manual', ?) RETURNING id",
                (now, c.controller, c.kind, c.outlet, c.ml, c.cap_s,
                 owner and owner[0]),
            ).fetchone()
            return cmd_id, None

    def set_interval(controller: str, value: int) -> int:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO controllers (controller, last_seen, next_s) "
                "VALUES (?, 0, ?) "
                "ON CONFLICT(controller) DO UPDATE SET next_s = excluded.next_s",
                (controller, value or None),
            )
        return value or interval

    def free_alerts(
        con: sqlite3.Connection,
        pot_id: str,
        controller: str | None,
        channel: int | None,
        outlet: int | None,
    ) -> None:
        """Drop the hose-keyed alerts a pot leaves behind when it lets go of
        its wiring — by being buried or by being erased.

        `sensor:<c>:<ch>` is the one that matters, and it is unclearable
        without this. Both its raise AND its clear live inside a loop over
        pots_now (see the sensor rule), so once the pot is gone or buried
        neither branch can ever run again: the row would sit in /health for
        ever and keep inflating the daily up-probe count, which excludes
        dose:, dosefail:, proposal: and meta: but not sensor:.
        `proposal:<c>:<outlet>` is the same shape and cheap to take with it,
        and without it the next pot on that hose inherits up to a day of
        nudge silence.

        Only when nobody alive is left on that pair — another pot may hold
        the channel or the outlet, and its alarm is not this pot's to clear.

        Removing a row is SILENT: no `cleared` goes to the phone, unlike
        clear(). That is the right answer for a condition nobody owns any
        more, and it is chosen here rather than discovered later.
        """
        for key, col, value in (
            (f"sensor:{controller}:{channel}", "m.channel", channel),
            (f"proposal:{controller}:{outlet}", "m.outlet", outlet),
        ):
            if controller is None or value is None:
                continue
            taken = con.execute(
                "SELECT 1 FROM pot_mappings m JOIN pots p ON p.id = m.pot_id "
                "WHERE m.to_ts IS NULL AND m.pot_id != ? AND m.controller = ? "
                f"AND {col} = ? AND {live_sql('p.status')} LIMIT 1",
                (pot_id, controller, value),
            ).fetchone()
            if not taken:
                con.execute("DELETE FROM alerts WHERE key = ?", (key,))

    def delete_pot(pot_id: str) -> None:
        """Erase a pot and everything that is only about it.

        The opposite of the graveyard, and deliberately not reachable from
        the same request: the graveyard keeps the record and frees the
        hardware, this keeps nothing. It overturns the command log's
        "never pruned" rule for exactly one reason — the owner asked for
        the plant to be gone — and it costs something real, recorded in
        DECISIONS: a deleted pot's doses stop floating the hose-keyed
        cooldown and cap floors, so the next pot on that hose can be
        watered sooner than #5 would like, for up to a day.

        Order is forced by reachability, not by foreign keys — there are
        none in this database and nothing cascades. The verdicts and the
        `dose:<id>` ledger rows must go BEFORE the commands they are found
        through, and both must go at all: commands.id is a rowid alias with
        no AUTOINCREMENT, so sqlite hands the same ids out again. A
        leftover verdict would then label a stranger's dose, and a leftover
        dose: row would make the judgement loop skip a real dose for ever
        on its NOT EXISTS guard — a silent hole in "tell me when it's
        wrong", which is the worst kind of leftover this file can have.
        """
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            photo_ids = [
                row[0]
                for row in con.execute(
                    "SELECT id FROM photos WHERE pot_id = ?", (pot_id,)
                )
            ]
            # The board is holding a dose for this pot: it will pour, and
            # it will ack an id that no longer exists. Deleting the row also
            # frees the controller's one slot while the water is still
            # running, so the next command goes out on top of it.
            if con.execute(
                "SELECT 1 FROM commands WHERE pot_id = ? AND state = 'sent' LIMIT 1",
                (pot_id,),
            ).fetchone():
                raise ValueError(
                    f"the board is holding a dose for {pot_id}: "
                    "try again after its next report"
                )
            wiring = con.execute(
                "SELECT controller, channel, outlet FROM pot_mappings "
                "WHERE pot_id = ? AND to_ts IS NULL",
                (pot_id,),
            ).fetchone() or (None, None, None)
            con.execute(
                "DELETE FROM verdicts WHERE command_id IN "
                "(SELECT id FROM commands WHERE pot_id = ?)",
                (pot_id,),
            )
            con.execute(
                "DELETE FROM alerts WHERE key IN "
                "(SELECT 'dose:' || id FROM commands WHERE pot_id = ?)",
                (pot_id,),
            )
            free_alerts(con, pot_id, *wiring)
            for table in ("commands", "readings", "photos", "advice_dismissed"):
                con.execute(f"DELETE FROM {table} WHERE pot_id = ?", (pot_id,))
            con.execute("DELETE FROM pot_mappings WHERE pot_id = ?", (pot_id,))
            # The DELETE decides, never a SELECT before it: a bare read takes
            # no lock, so two concurrent deletes would both see the row and
            # both answer ok. Raising here rolls the whole thing back, which
            # is why the photograph files are only touched afterwards.
            if con.execute("DELETE FROM pots WHERE id = ?", (pot_id,)).rowcount == 0:
                raise ValueError(f"no such pot: {pot_id}")
        # After the commit, with no connection held. A pot with 300
        # photographs is 300 unlinks, and inside BEGIN IMMEDIATE that is 300
        # unlinks of blocked /report. A file no row knows about is invisible
        # and harmless; a row whose file has gone reads `missing` for ever —
        # so the row goes first and the bytes follow, never the other way.
        for photo_id in photo_ids:
            with contextlib.suppress(OSError):
                photo_path(pot_id, photo_id).unlink(missing_ok=True)
        # rmdir, not rmtree: the directory is not the truth, and a tree
        # delete would take bytes belonging to rows this transaction never
        # selected — including one the keep_photo race can create.
        with contextlib.suppress(OSError):
            (photos / pot_id).rmdir()

    def upsert_pot(fields: dict) -> tuple[str, str]:
        """Create or partially update one pot, refusing inconsistent merges.

        `id=` edits that pot, name included, so renaming is an ordinary
        field edit. A bare `name=` creates and mints an id. Validation runs
        on the MERGED row, stored values plus this request, so `dry_raw`
        today and `wet_raw` tomorrow is refused just like both at once.
        Column names come from the parse_pot whitelist, never the wire.

        Mapping keys land in pot_mappings, not pots: a changed wiring
        closes the open row and opens another, so past readings stay
        attributed to the pot that was actually on that channel.
        """
        pot_id = fields.get("id")
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if pot_id is not None:
                row = con.execute(
                    f"SELECT {', '.join(POT_COLUMNS)} FROM pots_now WHERE id = ?",
                    (pot_id,),
                ).fetchone()
                if row is None:
                    # An edit of a pot that is not there is a bug in the
                    # caller, not an invitation to create one under a name
                    # nobody asked for.
                    raise ValueError(f"no pot {pot_id}")
            else:
                # No id is a create, always. Looking the name up here is what
                # made a create silently edit whatever pot already answered to
                # it — and the app's own check against that cannot see a pot
                # added from another phone, or added while its list sat idle.
                # The name clash below refuses it instead.
                row = None
            current = (
                dict(zip(POT_COLUMNS, row))
                if row
                else dict.fromkeys(POT_COLUMNS)
                | {"mode": "manual", "status": "alive", "id": new_pot_id()}
            )
            # The id is minted into `current` before the merge, so a create
            # and an edit take one path from here on.
            sets = {k: v for k, v in fields.items() if k != "id"}
            merged = current | sets
            pot_id = current["id"]
            name = merged["name"]
            if merged["dry_raw"] is not None and merged["dry_raw"] == merged["wet_raw"]:
                raise ValueError("dry_raw and wet_raw must differ")
            if (
                merged["target_low_pct"] is not None
                and merged["target_high_pct"] is not None
                and merged["target_low_pct"] >= merged["target_high_pct"]
            ):
                raise ValueError("target_low_pct must be below target_high_pct")
            if merged["status"] == "graveyard" and any(
                k in sets for k in POT_MAP_FIELDS
            ):
                # parse_pot refuses the two in one body; this catches the two
                # in two bodies, which is the same contradiction spread out.
                # Asked of the MERGED status, so restoring and wiring
                # together still goes through — that one is not a
                # contradiction, it is how a plant comes back.
                raise ValueError(
                    "a graveyard pot holds no wiring: bring it back first"
                )
            clash = con.execute(
                "SELECT id FROM pots WHERE name = ? AND id != ?", (name, pot_id)
            ).fetchone()
            if clash:
                # The UNIQUE index would refuse this anyway, with a message
                # nobody outside sqlite can read.
                if fields.get("id") is None:
                    raise ValueError(
                        f"the name {name} is taken by pot {clash[0]} "
                        "— open it instead of creating one"
                    )
                raise ValueError(f"the name {name} is taken by pot {clash[0]}")
            if merged["controller"] is not None:
                # Two pots on one sensor or one hose is a config error that
                # would misread or miswater — refuse loudly. Asked whatever
                # this pot's own status is: the point is the OTHER pot, and
                # a graveyard pot has already let go of its window, so this
                # clause is now the second line of defence rather than the
                # first.
                for col in ("channel", "outlet"):
                    if merged[col] is None:
                        continue
                    other = con.execute(
                        f"SELECT name FROM pots_now WHERE controller = ? AND {col} = ? "
                        f"AND {live_sql()} AND id != ? LIMIT 1",
                        (merged["controller"], merged[col], pot_id),
                    ).fetchone()
                    if other:
                        raise ValueError(
                            f"{col} {merged[col]} on {merged['controller']} "
                            f"is taken by pot {other[0]}"
                        )
            pot_sets = {k: v for k, v in sets.items() if k not in POT_MAP_FIELDS}
            if row and pot_sets:
                con.execute(
                    f"UPDATE pots SET {', '.join(k + ' = ?' for k in pot_sets)} "
                    "WHERE id = ?",
                    [*pot_sets.values(), pot_id],
                )
            elif not row:
                keys = [
                    k for k in merged if k in POT_COLUMNS and k not in POT_MAP_FIELDS
                ]
                con.execute(
                    f"INSERT INTO pots ({', '.join(keys)}) "
                    f"VALUES ({', '.join('?' * len(keys))})",
                    [merged[k] for k in keys],
                )
            if any(k in sets for k in POT_MAP_FIELDS):
                wiring = tuple(merged[k] for k in POT_MAP_FIELDS)
                # Only when the wiring actually differs: otherwise saving an
                # unrelated field would fragment the history into a row per
                # save, and every one of those windows would be a lie.
                if wiring != tuple(current[k] for k in POT_MAP_FIELDS):
                    # One second for both rows, so the windows stay
                    # contiguous — the attribution join assumes it.
                    edge = window_edge(con, pot_id, now)
                    con.execute(
                        "UPDATE pot_mappings SET to_ts = ? "
                        "WHERE pot_id = ? AND to_ts IS NULL",
                        (edge, pot_id),
                    )
                    # One hose, one pot. A live pot on this wiring was
                    # refused above, and a graveyard one let go of its window
                    # when it was buried — so this should find nothing, and
                    # it stays because it is the only thing that would. A
                    # database that arrived with two open windows on one hose
                    # (the defect fixed on 2026-09-03) now has no read-time
                    # GROUP BY papering over it: the reading stamp would pick
                    # one of the two arbitrarily and the pick would be
                    # permanent. Each displaced window closes on its own
                    # edge, so a backwards clock cannot invert it or orphan
                    # a dose it already holds.
                    displaced = con.execute(
                        "SELECT DISTINCT m.pot_id FROM pot_mappings m "
                        "JOIN pots p ON p.id = m.pot_id "
                        "WHERE m.to_ts IS NULL AND m.pot_id != ? "
                        "AND m.controller = ? AND (m.channel = ? OR m.outlet = ?)",
                        (pot_id, *wiring),
                    ).fetchall()
                    for (other_id,) in displaced:
                        con.execute(
                            "UPDATE pot_mappings SET to_ts = ? "
                            "WHERE pot_id = ? AND to_ts IS NULL",
                            (window_edge(con, other_id, now), other_id),
                        )
                    con.execute(
                        "INSERT INTO pot_mappings "
                        "(pot_id, controller, channel, outlet, from_ts, to_ts) "
                        "VALUES (?, ?, ?, ?, ?, NULL)",
                        (pot_id, *wiring, edge),
                    )
                    # The socket it just left, for the same reason burying
                    # one does it: `sensor:<c>:<ch>` is raised and cleared
                    # inside a loop over the pot's CURRENT wiring, so an
                    # alarm on the old channel has nobody left to clear it.
                    free_alerts(
                        con,
                        pot_id,
                        current["controller"],
                        current["channel"],
                        current["outlet"],
                    )
            if sets.get("status") == "graveyard" and current["status"] != "graveyard":
                # Burying a pot is what UNPLUGS it, and that is the whole
                # difference from the switch this replaced: the hose and the
                # socket go back to the garden. window_edge, never `now` —
                # a to_ts before a dose the window already holds orphans
                # that dose's cooldown and cap for good.
                edge = window_edge(con, pot_id, now)
                con.execute(
                    "UPDATE pot_mappings SET to_ts = ? "
                    "WHERE pot_id = ? AND to_ts IS NULL",
                    (edge, pot_id),
                )
                # No new window: it comes back unwired, because the plant
                # that comes back is not in the socket the old one left.
                # 'queued' as well as 'proposed': burial hands the outlet
                # back to the garden, so a dose still waiting for the board
                # would pour into whatever is wired there next. A 'sent' one
                # is already with the board and expires on its next report.
                con.execute(
                    "UPDATE commands SET state = 'expired' "
                    "WHERE pot_id = ? AND state IN ('proposed', 'queued')",
                    (pot_id,),
                )
                free_alerts(
                    con,
                    pot_id,
                    current["controller"],
                    current["channel"],
                    current["outlet"],
                )
        return pot_id, name

    def approve(cmd_id: int) -> tuple | None:
        """proposed -> queued, slot permitting; returns the blocker if busy.

        created_ts restarts on approval: the queued-TTL clock should time
        the wait for the board, not the hours the proposal sat waiting for
        a human.
        """
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # The proposal-TTL sweep normally runs on the controller's own
            # reports; a board gone dark never sweeps, so enforce the TTL
            # here too — a days-old proposal must not water on the stale
            # evidence it was made from.
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE id = ? AND state = 'proposed' AND created_ts < ?",
                (cmd_id, now - PROPOSAL_TTL_S),
            )
            row = con.execute(
                "SELECT controller FROM commands WHERE id = ? AND state = 'proposed'",
                (cmd_id,),
            ).fetchone()
            if not row:
                # Keep the expiry sweep even though we refuse: the with-block
                # would roll it back along with the raise, and /pots and the
                # database should agree the proposal is gone.
                con.commit()
                raise ValueError(f"no proposed command {cmd_id}")
            # The same TTL backstop enqueue runs: a dead board's abandoned
            # command must not wedge approval behind a 409 forever.
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE controller = ? AND state IN ('queued', 'sent') "
                "AND COALESCE(sent_ts, created_ts) < ?",
                (row[0], now - cmd_ttl),
            )
            busy = con.execute(
                "SELECT id, state FROM commands WHERE controller = ? "
                "AND state IN ('queued', 'sent') LIMIT 1",
                (row[0],),
            ).fetchone()
            if busy:
                return busy
            con.execute(
                "UPDATE commands SET state = 'queued', created_ts = ? WHERE id = ?",
                (now, cmd_id),
            )
            return None

    def record_verdict(cmd_id: int, verdict: str) -> None:
        """One human judgement per executed dose; a re-verdict replaces."""
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT sent_ts FROM commands WHERE id = ?", (cmd_id,)
            ).fetchone()
            if not row or row[0] is None:
                raise ValueError(f"command {cmd_id} was never handed to the board")
            con.execute(
                "INSERT OR REPLACE INTO verdicts (command_id, ts, verdict) "
                "VALUES (?, ?, ?)",
                (cmd_id, now, verdict),
            )

    started = int(time.time())
    observed = {"since": started, "last_tick": None}
    up_sent = False
    with connect() as con:
        prior = con.execute(
            "SELECT raised_ts, detail FROM alerts WHERE key = 'meta:tick'"
        ).fetchone()
    if prior is not None and started - prior[0] <= RESUME_GRACE_S:
        # A short restart (a redeploy, a crash loop) must not blind the
        # silent and sensor rules for another threshold on every
        # incarnation: inherit the observation window the previous process
        # earned.
        with contextlib.suppress(TypeError, ValueError):
            observed["since"] = min(started, int(prior[1]))

    def median_window_pct(
        con: sqlite3.Connection,
        controller: str,
        channel: int,
        dry: int,
        wet: int,
        where: str,
        params: tuple,
    ) -> int | None:
        """Median % of the last up-to-RULES_WINDOW readings matching `where`;
        None when no reading does (or the calibration is missing). With a
        full window and `ts <= sent_ts` this is exactly the window the rules
        judged: same ordering, same tie-break."""
        window = [
            raw
            for (raw,) in con.execute(
                "SELECT raw FROM readings "
                f"WHERE controller = ? AND channel = ? AND {where} "
                "ORDER BY ts DESC, rowid DESC LIMIT ?",
                (controller, channel, *params, RULES_WINDOW),
            )
        ]
        if not window:
            return None
        window.sort()
        return moisture_pct(window[len(window) // 2], dry, wet)

    def evaluate(con: sqlite3.Connection, now: int, since: int) -> list[Alert]:
        """Every alert rule, from database state alone: bare SELECTs on an
        autocommit connection, never a write. Each Alert carries its own
        record step so the tick can apply it only once its message went out.
        """
        found: list[Alert] = []
        paged_hoses: set[str] = set()
        standing = {
            key: (raised_ts, cleared_ts)
            for key, raised_ts, cleared_ts in con.execute(
                "SELECT key, raised_ts, cleared_ts FROM alerts"
            )
        }

        def raised(key: str) -> bool:
            row = standing.get(key)
            return row is not None and row[1] is None

        def floor_ok(key: str) -> bool:
            # A cleared condition may sound again only after the floor: a
            # float bouncing at the waterline is one pair an hour, not a
            # pair per report.
            row = standing.get(key)
            return row is None or row[1] is None or now - row[1] >= REALERT_FLOOR_S

        def mark(key: str, detail: str | None = None) -> Callable:
            def write(con: sqlite3.Connection) -> None:
                con.execute(
                    "INSERT OR REPLACE INTO alerts "
                    "(key, raised_ts, cleared_ts, detail) VALUES (?, ?, NULL, ?)",
                    (key, now, detail),
                )

            return write

        def clear(key: str) -> Callable:
            def write(con: sqlite3.Connection) -> None:
                con.execute(
                    "UPDATE alerts SET cleared_ts = ? WHERE key = ?", (now, key)
                )

            return write

        # A controller that stopped reporting. Silence is measured against
        # the butler's own observation window too: after a redeploy or a NAS
        # reboot, last_seen is stale because the BUTLER was away — that is
        # the dead-man's news, not this rule's.
        heartbeat: dict[str, tuple[int, int | None]] = {}
        for controller, last_seen, override in con.execute(
            "SELECT controller, last_seen, next_s FROM controllers WHERE last_seen > 0"
        ):
            heartbeat[controller] = (last_seen, override)
            threshold = max(silent_after, 3 * (override or interval))
            key = f"silent:{controller}"
            if now - max(last_seen, since) > threshold:
                if not raised(key) and floor_ok(key):
                    found.append(
                        Alert(
                            key,
                            "high",
                            "warning",
                            f"{controller} has been silent for "
                            f"{(now - last_seen) // 60} min "
                            f"(last report {hhmm(last_seen)})",
                            mark(key),
                        )
                    )
            elif now - last_seen <= threshold and raised(key):
                found.append(
                    Alert(
                        key,
                        "default",
                        "white_check_mark",
                        f"{controller} is reporting again",
                        clear(key),
                    )
                )

        # A sensor whose channel stopped arriving while its controller stays
        # healthy: water_rules errs dry and skips the pot forever, which
        # without this rule is a plant quietly dying behind a green
        # dead-man. Same threshold and observation window as the silent
        # rule; a controller that is itself silent already pages there.
        for name, controller, channel in con.execute(
            f"SELECT name, controller, channel FROM pots_now WHERE {live_sql()} "
            "AND controller IS NOT NULL AND channel IS NOT NULL ORDER BY name"
        ):
            pulse = heartbeat.get(controller)
            if pulse is None:
                continue  # never-heard controller: nothing to compare against
            last_seen, override = pulse
            threshold = max(silent_after, 3 * (override or interval))
            if now - last_seen > threshold:
                continue  # the whole controller is silent: that rule pages
            (latest,) = con.execute(
                "SELECT MAX(ts) FROM readings WHERE controller = ? AND channel = ?",
                (controller, channel),
            ).fetchone()
            key = f"sensor:{controller}:{channel}"
            if now - max(latest or 0, since) > threshold:
                if not raised(key) and floor_ok(key):
                    found.append(
                        Alert(
                            key,
                            "high",
                            "warning",
                            f"the sensor for {name} ({controller} ch{channel}) "
                            "stopped reporting: the rules cannot water it",
                            mark(key),
                        )
                    )
            elif latest is not None and now - latest <= threshold and raised(key):
                found.append(
                    Alert(
                        key,
                        "default",
                        "white_check_mark",
                        f"the sensor for {name} is back",
                        clear(key),
                    )
                )

        # The board said so itself: reservoir empty, or the manifold no
        # longer knows where it is — both mean the rules refuse to water,
        # silently. Two bad sightings inside FLAP_WINDOW_S raise (a single
        # blip at the waterline is slosh; a flap or a steady bad is an
        # empty tank — a sub-persistence debounce would sleep through the
        # flap); the clear needs the value good with no bad sighting for a
        # full window. NULL never counts as bad, but a field VANISHING
        # after the board had been sending it is its own alarm: absence
        # disables all watering with every tick looking clean.
        for (
            controller,
            _ts,
            float_ok,
            float_since,
            pos,
            pos_since,
            float_seen,
            pos_seen,
            float_bad,
            float_bad_prev,
            pos_bad,
            pos_bad_prev,
        ) in con.execute(
            "SELECT controller, ts, float_ok, float_since, pos, pos_since, "
            "float_seen, pos_seen, float_bad, float_bad_prev, pos_bad, "
            "pos_bad_prev FROM status"
        ):
            pos_value = {"ok": 1, "unknown": 0}.get(pos)
            for kind, value, value_since, seen, bad, bad_prev, trouble, relief in (
                (
                    "float",
                    float_ok,
                    float_since,
                    float_seen,
                    float_bad,
                    float_bad_prev,
                    (
                        f"the reservoir on {controller} is empty or at the "
                        "waterline: watering is on hold"
                    ),
                    f"the reservoir on {controller} is full again",
                ),
                (
                    "pos",
                    pos_value,
                    pos_since,
                    pos_seen,
                    pos_bad,
                    pos_bad_prev,
                    (
                        f"{controller} lost track of its manifold position: "
                        "watering is on hold"
                    ),
                    f"{controller} knows its manifold position again",
                ),
            ):
                key = f"{kind}:{controller}"
                flapped = (
                    bad is not None
                    and bad_prev is not None
                    and now - bad <= FLAP_WINDOW_S
                    and now - bad_prev <= FLAP_WINDOW_S
                )
                if flapped:
                    if not raised(key) and floor_ok(key):
                        found.append(Alert(key, "high", "warning", trouble, mark(key)))
                elif (
                    value == 1
                    and (bad is None or now - bad > FLAP_WINDOW_S)
                    and raised(key)
                ):
                    found.append(
                        Alert(key, "default", "white_check_mark", relief, clear(key))
                    )
                vkey = f"fields:{kind}:{controller}"
                if (
                    value is None
                    and seen is not None
                    and now - value_since >= PERSIST_S
                ):
                    if not raised(vkey) and floor_ok(vkey):
                        found.append(
                            Alert(
                                vkey,
                                "high",
                                "warning",
                                f"{controller} stopped sending {kind}=: "
                                "watering is on hold",
                                mark(vkey),
                            )
                        )
                elif (
                    value is not None
                    and now - value_since >= PERSIST_S
                    and raised(vkey)
                ):
                    found.append(
                        Alert(
                            vkey,
                            "default",
                            "white_check_mark",
                            f"{controller} sends {kind}= again",
                            clear(vkey),
                        )
                    )

        # Every dose the board was handed gets judged exactly once: never
        # acked (judged immediately — the loss is proven the moment the next
        # report failed to ack), short on the meter, or no moisture rise
        # (both a soak later). A dose that worked is recorded silently —
        # this is "tell me when it's wrong", not a watering feed. The 24 h
        # lookback bounds the first-deploy burst; its cost is that a
        # judgement ntfy could not take for a full day is dropped, and by
        # then the dead-man has been quiet for most of it.
        for (
            cmd_id,
            controller,
            outlet,
            ml,
            flow_ml,
            _,
            sent_ts,
            acked_ts,
            owner,
        ) in con.execute(
            "SELECT id, controller, outlet, ml, flow_ml, state, sent_ts, "
            "acked_ts, pot_id FROM commands "
            "WHERE kind = 'water' AND sent_ts IS NOT NULL "
            "AND state IN ('acked', 'expired') "
            "AND COALESCE(acked_ts, sent_ts) >= ? "
            "AND NOT EXISTS "
            "(SELECT 1 FROM alerts WHERE key = 'dose:' || commands.id)",
            (now - DOSE_LOOKBACK_S,),
        ).fetchall():
            row = con.execute(
                "SELECT next_s FROM controllers WHERE controller = ?", (controller,)
            ).fetchone()
            # Slow reporters get a longer soak, or the after-window would
            # hold no readings at all and every dose would judge on nothing.
            soak = max(SOAK_S, 3 * ((row and row[0]) or interval))
            judge_at = (acked_ts or sent_ts) + soak
            if acked_ts is not None and now < judge_at:
                continue  # still soaking in; an expiry needs no wait
            key = f"dose:{cmd_id}"
            # Two questions that used to be one join, and they have
            # different answers. WHOSE dose it was is the stamp on the row,
            # decided when the command was written. WHICH SENSOR it should
            # be judged on is still a window question — the pot may have
            # been rewired between the dose and the soak, and the rise
            # belongs to the probe that was in that soil at the time.
            #
            # No status filter: a dose that happened is a dose worth
            # judging and naming, even if the plant has since been buried.
            pot = (
                con.execute(
                    "SELECT name, dry_raw, wet_raw FROM pots WHERE id = ?",
                    (owner,),
                ).fetchone()
                if owner
                else None
            )
            channel_row = (
                con.execute(
                    "SELECT channel FROM pot_mappings WHERE pot_id = ? "
                    "AND ? >= from_ts AND (to_ts IS NULL OR ? < to_ts)",
                    (owner, sent_ts, sent_ts),
                ).fetchone()
                if owner
                else None
            )
            channel = channel_row[0] if channel_row else None
            name = pot[0] if pot else f"outlet {outlet}"
            symptoms: list[str] = []
            priority = "default"
            evidence = False  # did anything actually vouch for this dose?
            if acked_ts is None:  # handed out, expired unacknowledged
                symptoms.append("it was handed to the board and never acknowledged")
                priority = "high"
                evidence = True
            elif flow_ml is not None and ml is not None:
                evidence = True
                if 2 * flow_ml < ml:
                    symptoms.append(f"the meter counted {flow_ml} of {ml} ml")
                    priority = "high"
            if (
                pot is not None
                and acked_ts is not None
                and channel is not None
                and pot[1] is not None
                and pot[2] is not None
            ):
                _, dry, wet = pot
                before = median_window_pct(
                    con, controller, channel, dry, wet, "ts <= ?", (sent_ts,)
                )
                after = median_window_pct(
                    con,
                    controller,
                    channel,
                    dry,
                    wet,
                    "ts > ? AND ts <= ?",
                    (acked_ts, judge_at),
                )
                # An already-wet pot has no headroom to rise: skip, or every
                # hose-priming test dose would page the phone. The rise-only
                # symptom stays at default priority until the bench rig says
                # what a dose actually does to a sensor.
                if before is not None and after is not None:
                    evidence = True
                    if before < 100 - MIN_RISE_PCT and after - before < MIN_RISE_PCT:
                        symptoms.append(f"moisture went {before}% to {after}%")
            if symptoms:
                # Failures correlate: a dead pump takes every pot down at
                # once, and one high page per dose per pot is the muted
                # topic the docstring warns about. One page per controller
                # per floor; the rest are judged and recorded silently.
                hose = f"dosefail:{controller}"
                floored = standing.get(hose)
                if hose in paged_hoses or (
                    floored is not None and now - floored[0] < REALERT_FLOOR_S
                ):
                    found.append(
                        Alert(key, "min", "droplet", None, mark(key, "failed"))
                    )
                else:
                    paged_hoses.add(hose)

                    def record_failure(
                        con: sqlite3.Connection, _key=key, _hose=hose
                    ) -> None:
                        mark(_key, "failed")(con)
                        mark(_hose)(con)

                    found.append(
                        Alert(
                            key,
                            priority,
                            "warning,droplet",
                            f"the {ml} ml dose on {name} did not work: "
                            + "; ".join(symptoms),
                            record_failure,
                        )
                    )
            else:
                # No symptom, but 'ok' only when something vouched for it: a
                # dose with no meter number and no usable readings is
                # 'unverified', not quietly fine.
                found.append(
                    Alert(
                        key,
                        "min",
                        "droplet",
                        None,
                        mark(key, "ok" if evidence else "unverified"),
                    )
                )

        # A learning proposal nobody is polling /pots for. Keyed on the
        # hose, not the command: proposals expire and respawn with fresh ids
        # every PROPOSAL_TTL_S while the pot stays dry, and one nudge a day
        # is a reminder where one per respawn is a mute button.
        for (
            controller,
            outlet,
            cmd_id,
            ml,
            created_ts,
            name,
            channel,
            dry,
            wet,
            low,
        ) in con.execute(
            # The pot that is on the hose now AND was already on it when the
            # proposal was made: an offer to open a hose is not something
            # the next pot along inherits, and /pots stops showing it too.
            # Since it was on the HOSE, not since its wiring last changed —
            # correcting a sensor channel must not mute the nudge.
            "SELECT c.controller, c.outlet, c.id, c.ml, c.created_ts, p.name, "
            "m.channel, p.dry_raw, p.wet_raw, p.target_low_pct FROM commands c "
            "JOIN pot_mappings m ON m.controller = c.controller "
            "AND m.outlet = c.outlet AND m.to_ts IS NULL "
            f"AND c.created_ts >= {_hose_since('m.pot_id', 'm.controller', 'm.outlet')} "
            f"JOIN pots p ON p.id = m.pot_id AND {live_sql('p.status')} "
            "WHERE c.state = 'proposed' AND c.created_ts >= ? ORDER BY c.id",
            (now - PROPOSAL_TTL_S,),
        ):
            key = f"proposal:{controller}:{outlet}"
            row = standing.get(key)
            if row is not None and now - row[0] < PROPOSAL_NUDGE_S:
                continue
            pct = median_window_pct(
                con, controller, channel, dry, wet, "ts <= ?", (now,)
            )
            found.append(
                Alert(
                    key,
                    "default",
                    "seedling",
                    f"{name} looks dry ({pct}%, target {low}%): proposal "
                    f"{cmd_id} for {ml} ml waits until "
                    f"{hhmm(created_ts + PROPOSAL_TTL_S)} - approve it from /pots",
                    mark(key),
                )
            )
        return found

    def tick(now: int | None = None) -> bool:
        """One alert pass; True only when everything it tried succeeded.

        Reads run on an autocommit connection — bare SELECTs, so the report
        path's BEGIN IMMEDIATE is never blocked behind a network call. Each
        record step is its own short write transaction, applied only after
        its message went out: a failed send leaves no row and the next tick
        retries it (at-least-once — a crash between send and record repeats
        a message; loud beats lost). The first failed send stops the loop,
        since everything behind it would fail the same way, and any unclean
        tick withholds the dead-man ping: an unreachable ntfy must trip the
        dead man, not feed it.
        """
        nonlocal up_sent
        now = int(time.time()) if now is None else now
        if observed["last_tick"] is not None and now - observed["last_tick"] > 3 * beat:
            observed["since"] = now  # the butler was away, not the boards
        observed["last_tick"] = now
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # The observation window survives short restarts through this
            # row (read back in create_app): a crash-looping butler must not
            # blind the silent and sensor rules on every incarnation.
            con.execute(
                "INSERT OR REPLACE INTO alerts "
                "(key, raised_ts, cleared_ts, detail) "
                "VALUES ('meta:tick', ?, NULL, ?)",
                (now, str(observed["since"])),
            )
        with connect() as con:
            pending = evaluate(con, now, observed["since"])
        ok = True
        attempted = False
        for alert in pending:
            if alert.message is not None:
                attempted = True
                if not send(alert):
                    ok = False
                    break
            with connect() as con:
                con.execute("BEGIN IMMEDIATE")
                alert.record(con)
        if ok and not up_sent and now - started >= UP_AFTER_S:
            # One end-to-end probe of the topic: uptime-gated so a fast
            # crash loop never sends it, floored at one a day across
            # restarts so a slow loop cannot spam either. Without it, a
            # typo'd topic is a permanent, undetectable alert blackout:
            # ntfy answers 200 on any topic, and a healthy garden is also
            # silent.
            with connect() as con:
                (raised_count,) = con.execute(
                    "SELECT COUNT(*) FROM alerts WHERE cleared_ts IS NULL "
                    "AND key NOT LIKE 'dose:%' AND key NOT LIKE 'dosefail:%' "
                    "AND key NOT LIKE 'proposal:%' AND key NOT LIKE 'meta:%'"
                ).fetchone()
                last_probe = con.execute(
                    "SELECT raised_ts FROM alerts WHERE key = 'meta:up'"
                ).fetchone()
            if last_probe is not None and now - last_probe[0] < UP_PROBE_FLOOR_S:
                up_sent = True  # probed recently enough, across restarts
            else:
                probe_alert = Alert(
                    None,
                    "min",
                    "robot",
                    f"the butler is up; {raised_count} condition(s) raised",
                )
                attempted = True
                if send(probe_alert):
                    up_sent = True
                    with connect() as con:
                        con.execute("BEGIN IMMEDIATE")
                        con.execute(
                            "INSERT OR REPLACE INTO alerts "
                            "(key, raised_ts, cleared_ts, detail) "
                            "VALUES ('meta:up', ?, NULL, NULL)",
                            (now,),
                        )
                else:
                    ok = False
        if ok and not attempted and check is not None and not check():
            # A pass that sent nothing proved nothing: a healthy garden is
            # quiet, and the dead-man must still stop when ntfy has been
            # unreachable for days.
            ok = False
        if ok and ping is not None:
            ok = ping()
        return ok

    async def ticker() -> None:
        # The first tick comes a full beat after startup, never at t=0: a
        # crash-looping container must not reach the dead-man ping.
        while True:
            await asyncio.sleep(beat)
            try:
                await run_in_threadpool(tick)
            except Exception as why:  # noqa: BLE001 - the ticker survives anything
                print(f"alert tick failed: {why!r}", file=sys.stderr)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(ticker()) if alerts_on else None
        yield
        if task is not None:
            # The cancel lands between ticks; a tick already running in the
            # threadpool finishes on its own, worst case ~one network
            # timeout. If docker's stop grace expires first, SIGKILL may
            # repeat one sent-but-unrecorded message on the next start:
            # at-least-once, loud beats lost.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan)
    app.state.tick = tick
    app.state.observed = observed

    def bad_token(request: Request) -> bool:
        given = request.headers.get("x-token", "")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII str,
        # which would turn a garbled header into a 500 instead of a 401.
        return not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))

    async def slurp(request: Request, cap: int = BODY_CAP) -> bytes | PlainTextResponse:
        body = b""
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > cap:
                    return PlainTextResponse("body too large\n", status_code=413)
        except ClientDisconnect:
            # Half-sent body on a WiFi drop: the client is gone, the response
            # goes nowhere, and a traceback per drop would just fill the log.
            return PlainTextResponse("client went away\n", status_code=400)
        return body

    @app.post("/report")
    async def report(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            parsed = parse_report(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            # In the threadpool: a stalled disk must not freeze the event loop.
            next_out, handed = await run_in_threadpool(handle_report, parsed)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        answer = f"next={next_out}\n"
        if handed:
            cmd_id, kind, outlet, ml, cap_s = handed
            if kind == "water":
                answer += f"cmd={cmd_id} water={outlet} ml={ml} cap_s={cap_s}\n"
            else:
                answer += f"cmd={cmd_id} stop=1\n"
        return PlainTextResponse(answer)

    @app.post("/command")
    async def command(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            parsed = parse_command(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            cmd_id, busy = await run_in_threadpool(enqueue, parsed)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        if busy:
            return PlainTextResponse(
                f"busy: cmd={busy[0]} state={busy[1]}\n", status_code=409
            )
        return PlainTextResponse(f"cmd={cmd_id}\n")

    @app.post("/interval")
    async def interval_knob(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            controller, value = parse_interval(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        if value and 2 * value > cmd_ttl:
            return PlainTextResponse(
                f"refused: next={value} would let a live board outlive the "
                f"command TTL ({cmd_ttl}s); raise BUTLER_CMD_TTL_S first\n",
                status_code=400,
            )
        try:
            effective = await run_in_threadpool(set_interval, controller, value)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"next={effective}\n")

    @app.post("/pot")
    async def pot(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            parsed = parse_pot(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            pot_id, name = await run_in_threadpool(upsert_pot, parsed)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"pot={pot_id} name={name}\n")

    @app.post("/pot/delete")
    async def erase_pot(request: Request):
        """`id=<pot id>`. Its own route rather than a field on /pot, for the
        same reason /photo/delete is: a save that lost its body must never
        become an erasure. Total and with no undo — the graveyard is the
        reversible one."""
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            pot_id = parse_pot_delete(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(delete_pot, pot_id)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse("ok\n")

    @app.post("/approve")
    async def approve_proposal(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            cmd_id = parse_approve(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            busy = await run_in_threadpool(approve, cmd_id)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        if busy:
            return PlainTextResponse(
                f"busy: cmd={busy[0]} state={busy[1]}\n", status_code=409
            )
        return PlainTextResponse(f"cmd={cmd_id}\n")

    @app.post("/verdict")
    async def verdict_knob(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            cmd_id, verdict = parse_verdict(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(record_verdict, cmd_id, verdict)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"cmd={cmd_id} verdict={verdict}\n")

    @app.get("/pots")
    def pots():
        try:
            with connect() as con:
                garden = []
                for row in con.execute(
                    f"SELECT {', '.join(POT_COLUMNS)} FROM pots_now ORDER BY name"
                ):
                    entry = dict(zip(POT_COLUMNS, row))
                    entry["raw"] = entry["read_ts"] = entry["pct"] = None
                    if entry["controller"] is not None and entry["channel"] is not None:
                        # By pot, not by channel: after a remap the newest
                        # row on the new channel was taken while another
                        # plant sat there. The guard above stays on the
                        # WIRING, because a percentage on the card is a
                        # now-fact and an unwired pot has no now.
                        latest = con.execute(
                            "SELECT raw, ts FROM readings WHERE pot_id = ? "
                            "ORDER BY ts DESC LIMIT 1",
                            (entry["id"],),
                        ).fetchone()
                        if latest:
                            entry["raw"], entry["read_ts"] = latest
                            entry["pct"] = moisture_pct(
                                entry["raw"], entry["dry_raw"], entry["wet_raw"]
                            )
                    entry["proposal"] = entry["last_dose"] = None
                    if entry["controller"] is not None and entry["outlet"] is not None:
                        # An offer to open a hose, so unlike the dose below
                        # it does NOT travel with the pot: it counts only
                        # while this pot is still the one on that hose, and
                        # a proposal older than this pot's arrival there was
                        # sized for whoever hung there before. Fenced on the
                        # hose, not on the open window — see _hose_since.
                        prop = con.execute(
                            "SELECT id, ml, cap_s, created_ts FROM commands "
                            "WHERE controller = ? AND outlet = ? "
                            "AND state = 'proposed' AND created_ts >= ? "
                            f"AND created_ts >= {_hose_since('?', '?', '?')} "
                            "ORDER BY id LIMIT 1",
                            (
                                entry["controller"],
                                entry["outlet"],
                                int(time.time()) - PROPOSAL_TTL_S,
                                entry["id"],
                                entry["controller"],
                                entry["outlet"],
                                entry["id"],
                            ),
                        ).fetchone()
                        if prop:
                            entry["proposal"] = dict(
                                zip(("id", "ml", "cap_s", "created_ts"), prop)
                            )
                    # The newest dose this pot was ever handed, with its
                    # human verdict: the id POST /verdict needs was
                    # otherwise visible only in the log. By the stamp, so
                    # moving a hose takes the history with it instead of
                    # filing it under the next pot along — which would judge
                    # one pot's soil against another pot's dose, in the
                    # table the learning log is made of. `sent_ts IS NOT
                    # NULL` was implicit in the old join (NULL >= from_ts is
                    # never true) and has to be said now.
                    dose = con.execute(
                        "SELECT c.id, c.ml, c.cap_s, c.flow_ml, c.state, "
                        "c.source, c.sent_ts, c.acked_ts, v.verdict "
                        "FROM commands c "
                        "LEFT JOIN verdicts v ON v.command_id = c.id "
                        "WHERE c.pot_id = ? AND c.kind = 'water' "
                        "AND c.sent_ts IS NOT NULL "
                        "ORDER BY c.sent_ts DESC, c.id DESC LIMIT 1",
                        (entry["id"],),
                    ).fetchone()
                    if dose:
                        entry["last_dose"] = dict(zip(LAST_DOSE_KEYS, dose))
                    # Both of these read caches only. The garden is fetched
                    # on every screen open and a care source in the middle
                    # of that would make the app as slow as the internet.
                    entry["advice"] = advice_for(con, entry, int(time.time()))
                    # The newest picture, for the thumbnail beside the name
                    # in the list. The id only — the bytes come from
                    # GET /photo/<id>, which the app already caches, so the
                    # garden answer stays a page of text.
                    #
                    # The disk is NOT asked here, unlike the strip: /pots is
                    # fetched on every screen open and one stat() per pot on
                    # a NAS bind mount is a cost the list should not carry.
                    # A row whose file has gone gives a thumbnail that does
                    # not load, and the strip is where that is diagnosed.
                    newest = con.execute(
                        "SELECT id FROM photos WHERE pot_id = ? "
                        "ORDER BY ts DESC, rowid DESC LIMIT 1",
                        (entry["id"],),
                    ).fetchone()
                    entry["photo"] = newest and newest[0]
                    entry["care"] = None
                    if entry["species"]:
                        # The pot usually stores the accepted binomial — the
                        # lookup offers it and the form takes it — which is
                        # a key in species_care but NOT in species_names, so
                        # asking the alias table first would find nothing.
                        key = normalise_species(entry["species"])
                        entry["care"] = cached_care(con, key)
                        if entry["care"] is None:
                            name = con.execute(
                                "SELECT accepted FROM species_names WHERE query = ?",
                                (key,),
                            ).fetchone()
                            if name and name[0]:
                                entry["care"] = cached_care(
                                    con, normalise_species(name[0])
                                )
                    garden.append(entry)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse({"pots": garden})

    @app.get("/species")
    async def species(request: Request):
        """What is known about a plant by name. Never writes to a pot: the
        numbers a human ends up with are written by POST /pot, by that human.

        The one GET here that asks for the token, because it is the one that
        spends something not ours: an unauthenticated caller could burn the
        Trefle quota for the whole household. Reads of our own data stay open
        on the tailnet as they always were.
        """
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        query = normalise_species(request.query_params.get("q") or "")
        if not query:
            return PlainTextResponse("refused: q= is empty\n", status_code=400)
        if len(query) > SPECIES_MAX:
            return PlainTextResponse(
                f"refused: q= is longer than {SPECIES_MAX} characters\n",
                status_code=400,
            )
        try:
            # In the threadpool: two HTTP hops with their own timeouts have
            # no business on the event loop, and neither has the disk.
            answer = await run_in_threadpool(look_up, query)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse(answer)

    @app.post("/advice")
    async def advice(request: Request):
        """`pot=<id> kind=target dismiss=1` — this offer was seen and
        refused. Only the refusal is stored; accepting an offer is an
        ordinary POST /pot, so no watering number is ever written from here.
        """
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            pot_id, kind = parse_advice(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(dismiss_advice, pot_id, kind)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse("ok\n")

    @app.get("/doses")
    def doses(request: Request):
        """The watering history: what was asked, what the meter counted,
        how it ended and what the human made of it.

        Attributed through the pot's own mapping windows, so a remapping
        moves a pot's past with it instead of relabelling it with whoever
        hangs on that hose now. Proposals are left out — they are offers
        the rules made, not water that was poured; the rest stays, because
        the row worth reading is the one that expired or flowed short, and
        filtering those out would hide exactly what the list is for.

        Without a pot the whole garden is listed, and a dose nobody can be
        attributed (handed out on a hose no pot held, or never handed out
        at all) carries a null pot rather than vanishing. With a pot only
        its own doses can appear, and an unhanded one therefore cannot:
        a dose belongs to a pot from the moment the board is given it.
        """
        try:
            pot_id, limit, before, before_id = parse_doses(request.query_params)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        columns = (
            "c.id, c.kind, c.ml, c.cap_s, c.flow_ml, c.state, c.source, "
            "c.created_ts, c.sent_ts, c.acked_ts, v.verdict, p.id, p.name"
        )
        # Newest first, by when the board was handed it; an unhanded one
        # sorts by when it was made, which is the only time it has.
        # A stop is not a dose: it has no outlet and no millilitres, so it
        # could never be attributed anyway, and listing it as an
        # unattributable dose would make the row that matters — a dose no
        # window claims — impossible to pick out. Same filter /pots uses
        # for last_dose.
        page = ""
        cursor: tuple = ()
        if before is not None:
            # The cursor is the whole sort key, not just its timestamp.
            page = (
                "AND (COALESCE(c.sent_ts, c.created_ts) < ? "
                "OR (COALESCE(c.sent_ts, c.created_ts) = ? AND c.id < ?)) "
            )
            cursor = (before, before, before_id)
        tail = (
            f"AND c.kind = 'water' AND c.state != 'proposed' {page}"
            "ORDER BY COALESCE(c.sent_ts, c.created_ts) DESC, c.id DESC LIMIT ?"
        )
        # No GROUP BY any more. It was there because two overlapping windows
        # on one hose would list one dose twice; a stamped row has exactly
        # one owner and cannot.
        if pot_id is None:
            sql = (
                f"SELECT {columns} FROM commands c "
                "LEFT JOIN pots p ON p.id = c.pot_id AND c.sent_ts IS NOT NULL "
                "LEFT JOIN verdicts v ON v.command_id = c.id "
                f"WHERE 1 {tail}"
            )
            args: tuple = (*cursor, limit)
        else:
            sql = (
                f"SELECT {columns} FROM commands c "
                "JOIN pots p ON p.id = c.pot_id "
                "LEFT JOIN verdicts v ON v.command_id = c.id "
                f"WHERE c.pot_id = ? AND c.sent_ts IS NOT NULL {tail}"
            )
            args = (pot_id, *cursor, limit)
        try:
            with connect() as con:
                rows = [dict(zip(DOSE_KEYS, row)) for row in con.execute(sql, args)]
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse({"doses": rows, "now": int(time.time())})

    @app.get("/history")
    def history(request: Request):
        """Bucketed raw counts for one POT: the chart's wire. Raw only, so
        the app derives % from the pot's current calibration and a
        recalibration re-reads the whole curve; `to` is the server's clock
        so the axis never trusts the phone's.

        By pot rather than by channel, which is what stops a plant wired
        into a dead one's socket opening its chart onto somebody else's
        moisture curve. Two consequences, both accepted rather than
        overlooked. This route takes no token — it never has — so it now
        confirms to an unauthenticated caller which pot ids exist; it
        answers 200 with no points for one that does not, so the confirmation
        is of the id, not of anything about the plant. And readings stamped
        with no pot (an environment channel, a socket nobody claimed) are no
        longer reachable through any route.
        """
        try:
            pot_id, hours, bucket_s = parse_history(request.query_params)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        now = int(time.time())
        # A bucket boundary, so `since` bounds every point and the first
        # bucket is whole instead of a partial that wobbles with the clock.
        since = (now - hours * 3600) // bucket_s * bucket_s
        try:
            with connect() as con:
                points = [
                    {"ts": bucket, "raw": round(avg), "lo": lo, "hi": hi, "n": n}
                    for bucket, avg, lo, hi, n in con.execute(
                        "SELECT (ts / ?) * ?, AVG(raw), MIN(raw), MAX(raw), COUNT(*) "
                        "FROM readings WHERE pot_id = ? AND ts >= ? "
                        "GROUP BY 1 ORDER BY 1",
                        (bucket_s, bucket_s, pot_id, since),
                    )
                ]
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse(
            {
                "pot": pot_id,
                "since": since,
                "to": now,
                "bucket_s": bucket_s,
                "points": points,
            }
        )

    @app.post("/photo")
    async def add_photo(request: Request):
        """`?pot=<id>&w=&h=` with the JPEG as the body.

        JPEG only, checked by its first bytes rather than by what the
        uploader called it. The store then holds one kind of file, so what
        goes back out can always be labelled image/jpeg and never sniffed
        by a browser into something it would run.
        """
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        try:
            pot_id, w, h = parse_photo(request.query_params)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        body = await slurp(request, PHOTO_CAP)
        if isinstance(body, PlainTextResponse):
            return body
        if not body.startswith(JPEG_HEAD):
            return PlainTextResponse(
                "refused: that is not a JPEG — the phone downscales and "
                "re-encodes before it uploads\n",
                status_code=400,
            )
        now = int(time.time())
        try:
            photo_id = await run_in_threadpool(keep_photo, pot_id, body, w, h, now)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.IntegrityError as why:
            # Every id keep_photo tried was taken. Retryable, and at four
            # bytes of randomness it never happens — but a 500 with a bare
            # traceback is not how anything else here fails.
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        except OSError as why:
            # A full volume, or one that went read-only. Its own status,
            # because it is the one failure here that nobody can retry away.
            return PlainTextResponse(f"refused: {why}\n", status_code=507)
        return PlainTextResponse(f"photo={photo_id} ts={now}\n")

    @app.get("/photos")
    def list_photos(request: Request):
        """`?pot=<id>&limit=`: one pot's strip, newest first.

        Gated, unlike every other read here, and so is the picture itself.
        The rest of them are numbers about plants; these are the one thing
        in this system that could show the inside of somebody's house. It
        costs nothing — the app puts the token on every GET already.
        """
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        try:
            pot_id, limit = parse_photos(request.query_params)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            rows = photo_rows(pot_id, limit)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse(
            {
                "pot": pot_id,
                "photos": rows,
                # A full page may have older ones behind it. Nothing pages
                # yet: the strip asks for more by raising limit, and this is
                # what tells it there would be a point.
                "more": len(rows) >= limit,
                "now": int(time.time()),
            }
        )

    @app.get("/photo/{photo_id}")
    async def get_photo(photo_id: str, request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        if not SAFE_ID.fullmatch(photo_id):
            return PlainTextResponse("refused: not a photo id\n", status_code=400)
        try:
            blob = await run_in_threadpool(photo_blob, photo_id)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=404)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return Response(
            blob,
            media_type="image/jpeg",
            headers={
                "X-Content-Type-Options": "nosniff",
                # An id is minted once and its bytes never change, so a
                # phone may keep the picture for as long as it likes. This
                # is what stops a strip re-downloading megabytes on every
                # refresh over the tailnet.
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @app.post("/photo/delete")
    async def delete_photo(request: Request):
        """`photo=<id>`. Its own route rather than a field on /photo: that
        one carries a picture, and losing a body must never become a
        deletion."""
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            photo_id = parse_photo_delete(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(forget_photo, photo_id)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse("ok\n")

    @app.get("/hello")
    def hello(request: Request):
        """Is this a butler, and is that the token?

        The one call a phone can make to tell a wrong address from a wrong
        token, which are different mistakes and only one of them is the
        user's to fix. Nothing else here can answer it. Most of the reads
        are ungated and answer a wrong token exactly as they answer a right
        one; the photo routes do check it, but they read the database and
        the disk, so their refusals are not only about the token; and every
        other gated route writes something.

        Touches no database, so it stays an answer about the address and
        the token and never about the disk.
        """
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        return PlainTextResponse(f"butler={VERSION}\n")

    @app.get("/health")
    def health():
        try:
            with connect() as con:
                count, last = con.execute(
                    "SELECT COUNT(*), MAX(ts) FROM readings"
                ).fetchone()

                def entry(controller: str) -> dict:
                    return {
                        "controller": controller,
                        "last_seen": 0,
                        "next_s": None,
                        "command": None,
                        "float": None,
                        "pos": None,
                        "err": None,
                        "err_ts": None,
                        "pos_ok_seen": None,
                        "retired": 0,
                        "latched": None,
                        "last_refill": None,
                    }

                known: dict[str, dict] = {}
                for controller, seen in con.execute(
                    "SELECT controller, MAX(ts) FROM readings GROUP BY controller"
                ):
                    known.setdefault(controller, entry(controller))["last_seen"] = seen
                for controller, seen, override, retired in con.execute(
                    "SELECT controller, last_seen, next_s, retired FROM controllers"
                ):
                    e = known.setdefault(controller, entry(controller))
                    e["last_seen"] = max(e["last_seen"], seen)
                    e["next_s"] = override
                    e["retired"] = retired
                for (
                    controller, float_ok, pos, err, err_ts, pos_ok_seen, latched_ts, reason,
                ) in con.execute(
                    "SELECT controller, float_ok, pos, err, err_ts, pos_ok_seen, "
                    "latched_ts, latch_reason FROM status"
                ):
                    e = known.setdefault(controller, entry(controller))
                    e["float"] = float_ok
                    e["pos"] = pos
                    e["err"] = err
                    e["err_ts"] = err_ts
                    e["pos_ok_seen"] = pos_ok_seen
                    e["latched"] = (
                        {"since": latched_ts, "reason": reason}
                        if latched_ts is not None
                        else None
                    )
                for controller, ts in con.execute(
                    "SELECT controller, MAX(ts) FROM refills GROUP BY controller"
                ):
                    known.setdefault(controller, entry(controller))["last_refill"] = ts
                raised = [
                    {"key": key, "raised_ts": ts}
                    for key, ts in con.execute(
                        "SELECT key, raised_ts FROM alerts "
                        "WHERE cleared_ts IS NULL AND key NOT LIKE 'dose:%' "
                        "AND key NOT LIKE 'dosefail:%' "
                        "AND key NOT LIKE 'proposal:%' "
                        "AND key NOT LIKE 'meta:%' ORDER BY key"
                    )
                ]
                for cmd_id, controller, kind, state in con.execute(
                    "SELECT id, controller, kind, state FROM commands "
                    "WHERE state IN ('queued', 'sent')"
                ):
                    known.setdefault(controller, entry(controller))["command"] = {
                        "id": cmd_id,
                        "kind": kind,
                        "state": state,
                    }
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse(
            {
                "ok": True,
                "readings": count,
                "last_ts": last,
                "next_default": interval,
                "controllers": [known[k] for k in sorted(known)],
                "alerts": raised,
            }
        )

    return app
