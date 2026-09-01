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
"""

import hmac
import os
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

BODY_CAP = 4096  # a full 15-channel report is ~200 bytes; 4 KB is generous
RETRY_WINDOW_S = 300  # how long an identical (controller, t) counts as a retry
MAX_CHANNEL = 255
MAX_RAW = 2**31  # 14-bit ADC today; headroom without letting 2**63 near sqlite
MAX_DOSE_ML = 1000  # a liter in one command is already implausible for a pot
MAX_CAP_S = 60  # the firmware enforces its own cap; this bounds what we ask
MIN_NEXT_S, MAX_NEXT_S = 5, 3600  # the interval knob's sane range
RULES_WINDOW = 5  # median of this many readings is the whole smoothing story
PROPOSAL_TTL_S = 7200  # a proposal nobody approved in 2 h expires
DEFAULT_COOLDOWN_H = 6  # when a pot does not set its own; 0 disables
DEFAULT_DAILY_CAP_DOSES = 3  # a NULL daily_cap_ml means this many doses
FLOW_FLOOR_ML_S = 20  # worst-case pump flow, sizes cap_s; bench-rig-tunable
VERDICT_VALUES = ("ok", "too_much", "too_little")


class Report(NamedTuple):
    controller: str
    channels: dict[int, int]
    t: int | None  # board uptime, ms
    ack: int | None  # command id the board says it executed
    flow_ml: int | None  # what the flow meter counted while executing it
    float_ok: int | None  # reservoir float switch: 1 floats, 0 empty
    pos: str | None  # manifold position: 'ok' or 'unknown'


class Command(NamedTuple):
    controller: str
    kind: str  # 'water' | 'stop'
    outlet: int | None
    ml: int | None
    cap_s: int | None


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


def parse_report(text: str) -> Report:
    """`k=v` tokens, whitespace-separated; line breaks are whitespace too.

    Strict about shape — a malformed token, a duplicate key, a non-integer or
    out-of-range value refuses the whole report — and silent about unknown
    keys, for the reasons the module docstring gives. ASCII digits only in a
    channel key: Unicode digits would alias onto ASCII channel numbers.
    """
    controller = None
    channels: dict[int, int] = {}
    t = ack = flow_ml = float_ok = pos = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = value
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
        elif key.startswith("ch") and key[2:].isascii() and key[2:].isdigit():
            channel = int(key[2:])
            if channel > MAX_CHANNEL:
                raise ValueError(f"channel index out of range: {key}")
            if channel in channels:
                raise ValueError(f"channel given twice: {key}")
            channels[channel] = _int_in(value, key, 0, MAX_RAW)
    if not controller:
        raise ValueError("no c= in the report")
    if not channels:
        raise ValueError("no chN= in the report")
    if flow_ml is not None and ack is None:
        raise ValueError("flow_ml= without ack=")
    return Report(controller, channels, t, ack, flow_ml, float_ok, pos)


def parse_command(text: str) -> Command:
    """The `POST /command` body, same dialect and strictness as a report.

    `c=<controller>` plus either `water=<outlet> ml=<dose> cap_s=<cap>` or
    `stop=1`. Unknown keys are ignored here too, so the app can grow fields
    before this service reads them.
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
            controller = value
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
    if not controller:
        raise ValueError("no c= in the command")
    if stop and not (outlet is None and ml is None and cap_s is None):
        raise ValueError("stop takes no dose")
    if stop:
        return Command(controller, "stop", None, None, None)
    if outlet is None:
        raise ValueError("neither water= nor stop=1")
    if ml is None or cap_s is None:
        raise ValueError("water= needs both ml= and cap_s=")
    return Command(controller, "water", outlet, ml, cap_s)


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
            controller = value
        elif key == "next":
            if next_s is not None:
                raise ValueError("next= given twice")
            next_s = _int_in(value, "next", 0, MAX_NEXT_S + 1)
            if 0 < next_s < MIN_NEXT_S:
                raise ValueError(f"next= below {MIN_NEXT_S}s: {value}")
    if not controller:
        raise ValueError("no c= in the request")
    if next_s is None:
        raise ValueError("no next= in the request")
    return controller, next_s


POT_INT_FIELDS = {  # half-open bounds, like every other field
    "channel": (0, MAX_CHANNEL + 1),
    "outlet": (0, MAX_CHANNEL + 1),
    "dry_raw": (0, MAX_RAW),
    "wet_raw": (0, MAX_RAW),
    "target_low_pct": (0, 101),
    "target_high_pct": (0, 101),
    "dose_ml": (1, MAX_DOSE_ML + 1),
    "cooldown_h": (0, 8761),  # a year of cooldown is already a config error
    "daily_cap_ml": (0, 100_001),
    "enabled": (0, 2),
}
POT_TEXT_FIELDS = ("controller", "plant_type", "plant_size", "pot_size", "soil")
POT_MODES = ("manual", "learning", "auto")
POT_COLUMNS = (
    "id",
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


def parse_pot(text: str) -> dict:
    """The `POST /pot` body: `name=<pot>` plus whatever fields to set.

    A partial upsert — only the keys given change, so recalibration is
    `name=basil dry_raw=13000 wet_raw=4200` and nothing else moves. Values
    are single k=v tokens, so multi-word text uses underscores. Unknown
    keys are ignored, for the same reason as everywhere else.
    """
    fields: dict = {}
    known = {"name", "mode", *POT_TEXT_FIELDS, *POT_INT_FIELDS}
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
        elif key == "mode":
            if value not in POT_MODES:
                raise ValueError(f"mode= must be one of {'|'.join(POT_MODES)}")
            fields[key] = value
        else:
            fields[key] = value
    if "name" not in fields:
        raise ValueError("no name= in the request")
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


def create_app(
    db_path: str | None = None,
    token: str | None = None,
    next_s: int | None = None,
    cmd_ttl_s: int | None = None,
    quiet: str | None = None,
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

    if db.parent == Path("/data") and not os.path.ismount("/data"):
        raise ValueError(
            "BUTLER_DB is under /data but /data is not a mounted volume; "
            "refusing to store readings in the container layer"
        )
    db.parent.mkdir(parents=True, exist_ok=True)
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with sqlite3.connect(db) as bootstrap:
        bootstrap.execute("PRAGMA journal_mode=WAL")
        bootstrap.executescript(schema)

    def connect() -> sqlite3.Connection:
        con = sqlite3.connect(db, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        return con

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
        if in_quiet(time.localtime(now).tm_hour, *quiet_window):
            return
        candidates = con.execute(
            "SELECT name, channel, outlet, dry_raw, wet_raw, target_low_pct, "
            "dose_ml, mode, cooldown_h, daily_cap_ml FROM pots "
            "WHERE enabled = 1 AND mode IN ('learning', 'auto') "
            "AND controller = ? AND channel IS NOT NULL AND outlet IS NOT NULL "
            "AND dry_raw IS NOT NULL AND wet_raw IS NOT NULL "
            "AND target_low_pct IS NOT NULL AND dose_ml IS NOT NULL "
            "ORDER BY name",
            (r.controller,),
        ).fetchall()
        for (
            name,
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
            window = [
                raw
                for (raw,) in con.execute(
                    "SELECT raw FROM readings "
                    "WHERE controller = ? AND channel = ? "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (r.controller, channel, RULES_WINDOW),
                )
            ]
            if len(window) < RULES_WINDOW:
                continue
            window.sort()  # median of raw == median of pct: the map is monotonic
            median_pct = moisture_pct(window[RULES_WINDOW // 2], dry, wet)
            if median_pct is None or median_pct >= low:
                continue
            open_cmd = con.execute(
                "SELECT 1 FROM commands WHERE controller = ? AND outlet = ? "
                "AND state IN ('proposed', 'queued', 'sent') LIMIT 1",
                (r.controller, outlet),
            ).fetchone()
            if open_cmd:
                continue
            # Cooldown counts from the last command the board ever HELD
            # (sent_ts set): an expired-unacked command may still have
            # watered, so it cools the pot just like an acked one.
            cooldown_s = (cool_h if cool_h is not None else DEFAULT_COOLDOWN_H) * 3600
            watered = con.execute(
                "SELECT 1 FROM commands WHERE controller = ? AND outlet = ? "
                "AND sent_ts IS NOT NULL AND COALESCE(acked_ts, sent_ts) > ? "
                "LIMIT 1",
                (r.controller, outlet, now - cooldown_s),
            ).fetchone()
            if watered:
                continue
            cap = cap_ml if cap_ml is not None else DEFAULT_DAILY_CAP_DOSES * dose
            (spent,) = con.execute(
                "SELECT COALESCE(SUM(COALESCE(flow_ml, ml)), 0) FROM commands "
                "WHERE controller = ? AND outlet = ? AND sent_ts IS NOT NULL "
                "AND sent_ts > ?",
                (r.controller, outlet, now - 86400),
            ).fetchone()
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
            cap_s = min(MAX_CAP_S, max(5, dose // FLOW_FLOOR_ML_S + 5))
            con.execute(
                "INSERT INTO commands "
                "(created_ts, controller, kind, outlet, ml, cap_s, state, source) "
                "VALUES (?, ?, 'water', ?, ?, ?, ?, 'rules')",
                (now, r.controller, outlet, dose, cap_s, state),
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
                con.executemany(
                    "INSERT INTO readings (ts, controller, channel, raw, t) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (now, r.controller, ch, raw, r.t)
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
                con.execute(
                    "UPDATE commands SET state = 'sent', sent_ts = ? WHERE id = ?",
                    (now, handed[0]),
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
            (cmd_id,) = con.execute(
                "INSERT INTO commands "
                "(created_ts, controller, kind, outlet, ml, cap_s, state, source) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', 'manual') RETURNING id",
                (now, c.controller, c.kind, c.outlet, c.ml, c.cap_s),
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

    def upsert_pot(fields: dict) -> None:
        """Create or partially update one pot, refusing inconsistent merges.

        Validation runs on the MERGED row (stored values plus this
        request), so `dry_raw=5000` today and `wet_raw=5000` tomorrow is
        refused just like both in one request. Column names come from the
        parse_pot whitelist, never from the wire, so building SQL from
        them is safe.
        """
        name = fields["name"]
        sets = {k: v for k, v in fields.items() if k != "name"}
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                f"SELECT {', '.join(POT_COLUMNS)} FROM pots WHERE name = ?",
                (name,),
            ).fetchone()
            current = (
                dict(zip(POT_COLUMNS, row))
                if row
                else dict.fromkeys(POT_COLUMNS) | {"mode": "manual", "enabled": 1}
            )
            merged = current | sets
            if merged["dry_raw"] is not None and merged["dry_raw"] == merged["wet_raw"]:
                raise ValueError("dry_raw and wet_raw must differ")
            if (
                merged["target_low_pct"] is not None
                and merged["target_high_pct"] is not None
                and merged["target_low_pct"] >= merged["target_high_pct"]
            ):
                raise ValueError("target_low_pct must be below target_high_pct")
            if merged["enabled"] and merged["controller"] is not None:
                # Two enabled pots on one sensor or one hose is a config
                # error that would misread or miswater — refuse loudly.
                for col in ("channel", "outlet"):
                    if merged[col] is None:
                        continue
                    other = con.execute(
                        f"SELECT name FROM pots WHERE controller = ? AND {col} = ? "
                        "AND enabled = 1 AND name != ? LIMIT 1",
                        (merged["controller"], merged[col], name),
                    ).fetchone()
                    if other:
                        raise ValueError(
                            f"{col} {merged[col]} on {merged['controller']} "
                            f"is taken by pot {other[0]}"
                        )
            if row and sets:
                con.execute(
                    f"UPDATE pots SET {', '.join(k + ' = ?' for k in sets)} "
                    "WHERE name = ?",
                    [*sets.values(), name],
                )
            elif not row:
                keys = list(fields)
                con.execute(
                    f"INSERT INTO pots ({', '.join(keys)}) "
                    f"VALUES ({', '.join('?' * len(keys))})",
                    [fields[k] for k in keys],
                )

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

    app = FastAPI()

    def bad_token(request: Request) -> bool:
        given = request.headers.get("x-token", "")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII str,
        # which would turn a garbled header into a 500 instead of a 401.
        return not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))

    async def slurp(request: Request) -> bytes | PlainTextResponse:
        body = b""
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > BODY_CAP:
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
            await run_in_threadpool(upsert_pot, parsed)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"pot={parsed['name']}\n")

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
                    f"SELECT {', '.join(POT_COLUMNS)} FROM pots ORDER BY name"
                ):
                    entry = dict(zip(POT_COLUMNS, row))
                    entry["raw"] = entry["read_ts"] = entry["pct"] = None
                    if entry["controller"] is not None and entry["channel"] is not None:
                        latest = con.execute(
                            "SELECT raw, ts FROM readings "
                            "WHERE controller = ? AND channel = ? "
                            "ORDER BY ts DESC LIMIT 1",
                            (entry["controller"], entry["channel"]),
                        ).fetchone()
                        if latest:
                            entry["raw"], entry["read_ts"] = latest
                            entry["pct"] = moisture_pct(
                                entry["raw"], entry["dry_raw"], entry["wet_raw"]
                            )
                    entry["proposal"] = None
                    if entry["controller"] is not None and entry["outlet"] is not None:
                        prop = con.execute(
                            "SELECT id, ml, cap_s, created_ts FROM commands "
                            "WHERE controller = ? AND outlet = ? "
                            "AND state = 'proposed' AND created_ts >= ? "
                            "ORDER BY id LIMIT 1",
                            (
                                entry["controller"],
                                entry["outlet"],
                                int(time.time()) - PROPOSAL_TTL_S,
                            ),
                        ).fetchone()
                        if prop:
                            entry["proposal"] = dict(
                                zip(("id", "ml", "cap_s", "created_ts"), prop)
                            )
                    garden.append(entry)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse({"pots": garden})

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
                    }

                known: dict[str, dict] = {}
                for controller, seen in con.execute(
                    "SELECT controller, MAX(ts) FROM readings GROUP BY controller"
                ):
                    known.setdefault(controller, entry(controller))["last_seen"] = seen
                for controller, seen, override in con.execute(
                    "SELECT controller, last_seen, next_s FROM controllers"
                ):
                    e = known.setdefault(controller, entry(controller))
                    e["last_seen"] = max(e["last_seen"], seen)
                    e["next_s"] = override
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
                "controllers": [known[k] for k in sorted(known)],
            }
        )

    return app
