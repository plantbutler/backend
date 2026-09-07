"""Plant Butler backend: one container on the NAS, LAN only.

Two callers, both of which start the conversation. The Arduino board POSTs a
report every report interval — `k=v` tokens in the body, a static token in the
`X-Token` header — and the reply is `k=v` too: the next interval and, when one
is queued, at most one command. The phone app writes `k=v` as well; what it
reads back comes as JSON on the list and history routes, `k=v` elsewhere.

Everything here errs dry. A malformed report is refused whole rather than
stored in part; every watering gate refuses rather than waters; a command the
board may or may not be holding is expired rather than handed out twice.
Timestamps are stamped on arrival, because the board has no clock.

Storage is stdlib sqlite3 in WAL on a bind-mounted volume; `schema.sql` holds
the tables and the rules about them. The alert ticker is the only periodic
thing here. `fake_device.py` drives the whole wire without a board, and
`README.md` is the endpoint-by-endpoint reference.
"""

import asyncio
import contextlib
import hmac
import sqlite3
import sys
import time
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

# The package's own modules. This file is the facade the tests and uvicorn
# import, so the names come across rather than the modules: code below calls
# them unqualified, which is what keeps `monkeypatch.setattr(butler, ...)`
# intercepting a call create_app makes.
from .alerts import (
    Context,
    RULES,
    Ticker,
    clear,
    doses,
    evaluate,
    float_judged,
    latches,
    mark,
    median_window_pct,
    proposals,
    read_state,
    safety_fields,
    sensors,
    silent,
    tank_runs,
)
from .band import (
    BAND_CEIL,
    BAND_FLOOR,
    BAND_MIN_WIDTH,
    BAND_PER_DOUBLING,
    BASE_BAND,
    FAMILY_KINDS,
    GENUS_KINDS,
    HEIGHT_DOUBLINGS_CAP,
    HEIGHT_REF_RATIO,
    PLANT_KINDS,
    POT_DOUBLINGS_CAP,
    POT_REF_CM,
    SEASON_SHIFTS,
    SEASONS,
    SIZE_WHY_MIN,
    SOIL_SHIFTS,
    SPECIES_KINDS,
    Band,
    kind_for,
    size_shifts,
    target_band,
)
from .care import (
    care_for,
    care_note,
    cached_care,
    look_up,
    miss_note,
    search_for,
    taxon_for,
)
from .config import Config, configure, env_int
from .constants import (
    ALERT_TICK_S,
    BODY_CAP,
    CONTRA_CHANNEL,
    DEFAULT_COOLDOWN_H,
    DEFAULT_DAILY_CAP_DOSES,
    DOSE_LOOKBACK_S,
    DRY_CHANNEL,
    ERR_TOKEN,
    FLAP_CHANNEL,
    FLAP_WINDOW_S,
    FLOW_FLOOR_ML_S,
    JPEG_HEAD,
    LATCH_STEP,
    LATCH_TEXT,
    MAX_CAP_S,
    MAX_CHANNEL,
    MAX_CONTROLLER,
    MAX_DOSE_ML,
    MAX_NEXT_S,
    MAX_PHOTO_EDGE,
    MAX_PHOTO_LIMIT,
    MAX_RAW,
    MIN_NEXT_S,
    MIN_RISE_PCT,
    NTFY_TIMEOUT_S,
    PERSIST_S,
    PHOTO_CAP,
    PHOTO_ID_TRIES,
    PHOTO_LIMIT,
    PROPOSAL_NUDGE_S,
    PROPOSAL_TTL_S,
    REALERT_FLOOR_S,
    RESUME_GRACE_S,
    RETRY_WINDOW_S,
    RULES_WINDOW,
    SAFE_ID,
    SILENT_AFTER_S,
    SOAK_S,
    TANK_DRIFT_PCT,
    TANK_MEDIAN_OF,
    TANK_SAMPLES_TO_ARM,
    TANK_TOLERANCE_PCT,
    UP_AFTER_S,
    UP_PROBE_FLOOR_S,
    VERDICT_VALUES,
)
from .notify import Alert, hhmm, ping_deadman, post_ntfy
from .pots import (
    DOSE_KEYS,
    LAST_DOSE_KEYS,
    LIVE_STATUSES,
    ONE_SHOT_KEYS,
    POT_COLUMNS,
    RAISED_SQL,
    RULES_POT_SQL,
    _hose_since,
    live_sql,
    moisture_pct,
    waters,
    window_edge,
)
from .schema import (
    ADDED_COLUMNS,
    SCHEMA_SQL,
    Added,
    add_columns,
    migrate,
    name_standing_latches,
    new_photo_id,
    new_pot_id,
)
from .rules import water_rules
from .species import (
    CANDIDATE_KEYS,
    CANDIDATES_MAX,
    CARE_BODY_CAP,
    CARE_KEYS,
    CARE_MISS_TTL_S,
    CARE_TIMEOUT_S,
    GBIF_MATCH_URL,
    SPECIES_MAX,
    TREFLE_BASE,
    Taxon,
    binomial_case,
    fetch_json,
    loose,
    normalise_species,
    pick_species,
    read_candidates,
    read_gbif,
    read_trefle,
    sole_match,
)
from .store import (
    connect,
    forget_photo,
    keep_photo,
    photo_blob,
    photo_path,
    photo_rows,
    write_new_file,
)
from .tank import (
    Latched,
    Retired,
    base_tap,
    counter_origin,
    flap_tap,
    flap_try_pending,
    float_dead,
    is_over,
    is_retired,
    latch_of,
    latch_reason,
    latch_steps,
    latest_refill,
    over_stands,
    pumped_since,
    tank_history,
    tank_median,
    tank_ml,
    tank_state,
    tap_answers_flap,
    unannounced_samples,
)
from .wire import (
    ADVICE_KINDS,
    DOSES_MAX,
    HISTORY_MAX_BUCKETS,
    HISTORY_MAX_HOURS,
    POT_CM_FIELDS,
    POT_INT_FIELDS,
    POT_MAP_FIELDS,
    POT_MODES,
    POT_STATUSES,
    POT_TEXT_FIELDS,
    Command,
    Report,
    cap_for,
    cm_from_text,
    in_quiet,
    parse_advice,
    parse_approve,
    parse_board,
    parse_command,
    parse_controller,
    parse_doses,
    parse_history,
    parse_interval,
    parse_photo,
    parse_photo_delete,
    parse_photos,
    parse_pot,
    parse_pot_delete,
    parse_quiet,
    parse_report,
    parse_verdict,
)

# The container installs no package — it copies this one beside fastapi and
# runs it — so the version lives here. A test holds it to pyproject.toml.
VERSION = "0.20.0"


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
    """The whole service: the configuration, the database, and the routes.

    Nothing is read from the environment below this line — `configure` did
    that, and refused to start if any of it was wrong — so everything here
    takes its numbers from `cfg` and the app is built the same way whether it
    came up on the NAS or in a test.
    """
    cfg = configure(
        db_path=db_path,
        token=token,
        next_s=next_s,
        cmd_ttl_s=cmd_ttl_s,
        quiet=quiet,
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        deadman_url=deadman_url,
        silent_s=silent_s,
        tick_s=tick_s,
        send=send,
        ping=ping,
        probe=probe,
        trefle_token=trefle_token,
        fetch=fetch,
        photos_dir=photos_dir,
    )
    # The fields the closures below still read as locals. They go one by one
    # as each of those closures becomes a top-level function taking what it
    # needs; `cfg` is then the only thing this factory carries.
    db, photos = cfg.db, cfg.photos
    interval, cmd_ttl = cfg.interval, cfg.cmd_ttl
    send, ping, check = cfg.send, cfg.ping, cfg.check
    beat, alerts_on = cfg.beat, cfg.alerts_on
    care_token, get_json = cfg.care_token, cfg.get_json

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
        named = name_standing_latches(bootstrap)
        if named:
            print(f"named {named} standing latch(es)", file=sys.stderr)

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
        with connect(db) as con:
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

    def handle_report(r: Report) -> tuple[int, tuple | None]:
        """One report, one transaction: heartbeat, ack, expiries, dedup, the
        readings, the watering rules, and at most one command handed out —
        atomically, so two writers cannot hand the same command twice. A
        command the rules queue here rides out on this very response: the
        safety fields it was judged on are from this same report."""
        now = int(time.time())
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")  # writers serialize up front
            con.execute(
                "INSERT INTO controllers (controller, last_seen) VALUES (?, ?) "
                "ON CONFLICT(controller) DO UPDATE SET last_seen = excluded.last_seen",
                (r.controller, now),
            )
            # The firmware retries a report with the body kept when the
            # response is lost, so an identical (controller, t) inside the
            # window is one report heard twice, not two — and t is board
            # uptime, which recurs after every reboot, so the window is what
            # keeps a permanent uniqueness rule from dropping real readings.
            # Judged here, before the status upsert and the float's edge and
            # not only before the readings: a one-glitch sighting delivered
            # twice would otherwise agree with itself into the firm word. The
            # heartbeat above still counts and the hand-off below still runs,
            # since the board never saw the response the retry stands in for.
            duplicate = (
                r.t is not None
                and con.execute(
                    "SELECT 1 FROM readings "
                    "WHERE controller = ? AND t = ? AND ts >= ? LIMIT 1",
                    (r.controller, r.t, now - RETRY_WINDOW_S),
                ).fetchone()
            )
            if not duplicate:
                # The board's error and float before this report: the resetmid
                # latch is an edge on the one and a tank sample on the other,
                # and the upsert below overwrites both. The float is its last
                # word rather than float_ok, because a report that omits
                # float= blanks that column — its vanishing being its own
                # alarm — and must not hide the edge.
                prev = con.execute(
                    "SELECT err, float_word, float_firm, float_word_since "
                    "FROM status WHERE controller = ?",
                    (r.controller,),
                ).fetchone()
                prev_err, prev_word, prev_firm, prev_fell = prev or (None,) * 4
                # The firm word going 1 -> 0: firmly full, the last report
                # that carried float= said empty, and so does this one. One
                # sighting is a glitch by the board's own design — any of its
                # three samples failing fails the word — and a slosh at report
                # time must not close a sample early and hand the origin to
                # its recovery; a firm word of NULL was never firmly full and
                # is no edge. The drop belongs to the tap the word fell after,
                # since a person who filled and tapped between the two
                # sightings made a tap this run did not start from. Not on a
                # report carrying ch207=1: the contra latch is what forces the
                # word to 0 — "float OK, zero pulses" is a fault, not a drop —
                # and the upsert remembers the forced 0 so the word coming
                # back out of it is no rise. Under any other latch the float's
                # word is its own and a drain is an ordinary drop.
                edge = prev_firm == 1 and prev_word == 0 and r.float_ok == 0
                forced = r.channels.get(CONTRA_CHANNEL) == 1
                flap = int(r.channels.get(FLAP_CHANNEL) == 1)
                tap = None
                if edge and not forced:
                    tap = base_tap(con, r.controller, prev_fell)
                # The latest safety fields, with enough history for the
                # alert rules: when each value last changed (`since`), when
                # each was last sent at all (`seen` — its vanishing is an
                # alarm), and the last two bad sightings (`bad`, `bad_prev`
                # — a float flapping at the waterline must still page).
                # Plus the board's last error (`err`, left alone by a
                # report without one, and `err_ts`, when it last changed
                # value — the board repeats its last error on every report,
                # so "last seen" would just be the report clock) and the
                # last pos=ok ever seen (`pos_ok_seen`).
                # All SET expressions read the pre-update row, so the
                # ordering below is safe. latched_ts/latch_reason are
                # deliberately not here: the latch is set and cleared on
                # its own, never through this upsert, so no report can
                # overwrite it.
                con.execute(
                    "INSERT INTO status (controller, ts, float_ok, float_since, "
                    "pos, pos_since, float_seen, pos_seen, float_bad, "
                    "float_bad_prev, pos_bad, pos_bad_prev, err, err_ts, pos_ok_seen, "
                    "float_word, float_word_since, float_rise, contra, float_firm, "
                    "flap, flap_since, dry) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, "
                    "NULL, ?, NULL, ?, ?, ?) "
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
                    "AND status.err IS NOT excluded.err "
                    "THEN excluded.ts ELSE status.err_ts END, "
                    "pos_ok_seen = CASE WHEN excluded.pos = 'ok' "
                    "THEN excluded.ts ELSE status.pos_ok_seen END, "
                    "float_word = COALESCE(excluded.float_word, status.float_word), "
                    # float_word_since is the word's own clock, unlike
                    # float_since (float_ok's, which a report omitting float=
                    # moves and the fields: rule counts from): it moves on
                    # 1 -> 0 and 0 -> 1 and nothing else, so a report that
                    # said nothing leaves the float's last move where it was.
                    # The firm word is the word once this report and the last
                    # that carried float= agree — one sighting is a glitch by
                    # the board's own design, and a report that says nothing
                    # agrees with nothing (NULL IS 1 is false). It starts
                    # NULL, and NULL is never an edge: out of it, becoming 1
                    # is no rise and becoming 0 no drop.
                    #
                    # Its two clocks are the only edges the tank is measured
                    # on: the rise here, and the drop the report path stamps
                    # on the tap below, each where the word MOVED rather than
                    # where it was confirmed — the report that raises the word
                    # hands its queued dose with that clock, and the counter
                    # must not lose it. A firm drop arriving with ch207=1 is
                    # the contra latch forcing the word, remembered as
                    # float_forced, so the word coming back out of a forced 0
                    # is `clear contra` typed rather than a refill and stamps
                    # no rise: stamped, it would become the origin and launder
                    # the counter of an untapped refill's run. Every SET reads
                    # the row before this update. ch207, ch210 and ch211 are
                    # this report's, absent being 0; flap_since is when the
                    # flap last tripped, the clock a tap must be later than to
                    # answer it, left where it was when the flap lets go.
                    "float_word_since = CASE WHEN excluded.float_word IS NULL "
                    "OR status.float_word IS excluded.float_word "
                    "THEN status.float_word_since ELSE excluded.ts END, "
                    "float_firm = CASE WHEN status.float_word IS excluded.float_word "
                    "THEN excluded.float_word ELSE status.float_firm END, "
                    "float_rise = CASE WHEN excluded.float_word = 1 "
                    "AND status.float_word = 1 AND status.float_firm = 0 "
                    "AND status.float_forced = 0 "
                    "THEN status.float_word_since ELSE status.float_rise END, "
                    "float_forced = CASE WHEN excluded.float_word = 0 "
                    "AND status.float_word = 0 AND status.float_firm = 1 "
                    "THEN excluded.contra ELSE status.float_forced END, "
                    "contra = excluded.contra, "
                    "flap = excluded.flap, "
                    "flap_since = CASE WHEN excluded.flap = 1 AND status.flap = 0 "
                    "THEN excluded.ts ELSE status.flap_since END, "
                    "dry = excluded.dry",
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
                        r.float_ok,
                        now if r.float_ok is not None else None,
                        int(forced),
                        flap,
                        now if flap else None,
                        int(r.channels.get(DRY_CHANNEL) == 1),
                    ),
                )
                reason = latch_reason(r, prev_err)
                if reason is not None:
                    # `since` is set once and never refreshed while the latch
                    # stands: it is when the trouble began. The reason is the
                    # newest fault's — the one to fix — and a fault the board
                    # merely repeats is not a newer one. A dose still waiting
                    # would pour into a tank nobody has looked at, so it is
                    # expired; a 'sent' one is with the board, whose own latch
                    # holds it.
                    con.execute(
                        "UPDATE status SET latched_ts = COALESCE(latched_ts, ?), "
                        "latch_reason = ? WHERE controller = ?",
                        (now, reason, r.controller),
                    )
                    con.execute(
                        "UPDATE commands SET state = 'expired' WHERE controller = ? "
                        "AND kind = 'water' AND state IN ('proposed', 'queued')",
                        (r.controller,),
                    )
                if r.ack is not None:
                    con.execute(
                        "UPDATE commands SET state = 'acked', acked_ts = ?, flow_ml = ? "
                        "WHERE id = ? AND controller = ? AND state = 'sent'",
                        (now, r.flow_ml, r.ack, r.controller),
                    )
                if tap is not None and tap[2] is None:
                    # The float went empty for the first time since the tap
                    # and this report confirms it: stamped on the tap where
                    # the word fell, so a rise from here is a refill nobody
                    # said was full and a later drop is a second drain of it.
                    # The water the meter counted since the tap is then one
                    # measurement of the tank — after the ack step, because
                    # the dose that drained it acks on the sighting or on this
                    # one. One per tap, and nothing on zero: a tank drained by
                    # something the meter never saw is not a measurement. No
                    # sample while the board's latch stands, a fault having
                    # stood over the run, nor for a retired board; the drop is
                    # a fact either way.
                    since, rowid, _ = tap
                    con.execute(
                        "UPDATE refills SET drop_ts = ? WHERE rowid = ?",
                        (prev_fell, rowid),
                    )
                    pumped = pumped_since(con, r.controller, since)
                    if (
                        pumped > 0
                        and latch_of(con, r.controller) is None
                        and not is_retired(con, r.controller)
                    ):
                        con.execute(
                            "INSERT OR IGNORE INTO tank_samples "
                            "(ts, controller, refill_ts, ml) VALUES (?, ?, ?, ?)",
                            (now, r.controller, since, pumped),
                        )
            # A command still 'sent' after the ack step was handed on an
            # earlier response and this report did not ack it, so either that
            # response was lost or the board dropped it — both mean the board
            # does not have it. Expired, never re-handed: re-handing a
            # watering command the board might still execute is how a plant
            # drowns. Whoever wants water asks again.
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
                water_rules(con, r, now, interval, cfg.quiet_window)
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
                # the cooldown cannot see is one the HOSE floor stops covering
                # the moment that pot is rewired, so both watering floors go
                # at once and the plant is watered twice.
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
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE controller = ? AND state IN ('queued', 'sent') "
                "AND COALESCE(sent_ts, created_ts) < ?",
                (c.controller, now - cmd_ttl),
            )
            # Water for a retired board is refused here, the only door the
            # rules do not guard. A stop still goes: it is the safe
            # direction, and the board may be mid-dose from before.
            if c.kind == "water" and is_retired(con, c.controller):
                raise Retired(c.controller)
            if c.kind == "water":
                standing = latch_of(con, c.controller)
                if standing is not None:
                    raise Latched(c.controller, *standing)
            busy = con.execute(
                "SELECT id, state FROM commands "
                "WHERE controller = ? AND state IN ('queued', 'sent') LIMIT 1",
                (c.controller,),
            ).fetchone()
            if busy:
                return 0, busy
            # Whom this dose is for. A manual command names a hose, not a
            # pot, so the pot is whoever is on that hose right now, decided
            # once here. A stop has no outlet and stamps NULL, and so does a
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
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO controllers (controller, last_seen, next_s) "
                "VALUES (?, 0, ?) "
                "ON CONFLICT(controller) DO UPDATE SET next_s = excluded.next_s",
                (controller, value or None),
            )
        return value or interval

    def set_retired(controller: int, flag: int) -> None:
        """A retired board is a quiet one, not a rejected one: its reports
        still land, but nothing pages for it and nothing waters from it.
        Retiring drops the water still waiting for the board — a proposal
        or a queued dose would otherwise be handed out on its next report,
        so it goes the way burial sends one; a 'sent' one is with the board
        and expires on that report. It also clears whatever page stands for
        the board — silence, a sensor, the float, the position, a field that
        vanished, the latch, a float stuck either way: every rule skips the
        board from now on, so nobody else would ever clear them. The latch
        row itself stays: nobody checked that tank, and the board comes back
        with it."""
        now = int(time.time())
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO controllers (controller, last_seen, retired) "
                "VALUES (?, 0, ?) "
                "ON CONFLICT(controller) DO UPDATE SET retired = excluded.retired",
                (controller, flag),
            )
            if flag:
                con.execute(
                    "UPDATE commands SET state = 'expired' WHERE controller = ? "
                    "AND kind = 'water' AND state IN ('proposed', 'queued')",
                    (controller,),
                )
                con.execute(
                    "UPDATE alerts SET cleared_ts = ? WHERE cleared_ts IS NULL "
                    "AND (key IN (?, ?, ?, ?, ?, ?, ?, ?) OR key LIKE ?)",
                    (
                        now,
                        f"silent:{controller}",
                        f"float:{controller}",
                        f"pos:{controller}",
                        f"fields:float:{controller}",
                        f"fields:pos:{controller}",
                        f"latch:{controller}",
                        f"over:{controller}",
                        f"stale:{controller}",
                        f"sensor:{controller}:%",
                    ),
                )

    def resume(controller: int) -> None:
        """The human's half: the tank was checked. Clears the row and the
        page together, so /health and the phone agree the moment it answers;
        idempotent on a board that was not latched."""
        now = int(time.time())
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE status SET latched_ts = NULL, latch_reason = NULL "
                "WHERE controller = ?",
                (controller,),
            )
            con.execute(
                "UPDATE alerts SET cleared_ts = ? WHERE key = ? AND cleared_ts IS NULL",
                (now, f"latch:{controller}"),
            )

    def record_refill(controller: int) -> int:
        """A human refilled this board's tank. Returns the tap's timestamp.

        The tap means "full to the top", and the board's last real word on the
        float goes on the row: float_word rather than float_ok, which one
        report omitting float= blanks — a tap made under a row reading
        "float ?" must not be a tap that counts for nothing. NULL only for a
        board that has never sent float=, and then the tap judges nothing.
        Read under the write lock, so no report can slip between the look and
        the insert. The tap also answers over:<c>, cleared here in the tap's
        own transaction as /resume clears latch:<c>: the person did the thing,
        so the rules, /health and the phone let go the moment it lands.
        """
        now = int(time.time())
        with connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT float_word FROM status WHERE controller = ?", (controller,)
            ).fetchone()
            con.execute(
                "INSERT INTO refills (ts, controller, float_ok) VALUES (?, ?, ?)",
                (now, controller, row[0] if row else None),
            )
            con.execute(
                "UPDATE alerts SET cleared_ts = ? WHERE key = ? AND cleared_ts IS NULL",
                (now, f"over:{controller}"),
            )
        return now

    def free_alerts(
        con: sqlite3.Connection,
        pot_id: str,
        controller: str | None,
        channel: int | None,
        outlet: int | None,
    ) -> None:
        """Drop the hose-keyed alerts a pot leaves behind when it lets go of
        its wiring — by being buried or by being erased.

        `sensor:<c>:<ch>` is unclearable without this: both its raise and its
        clear live inside a loop over pots_now, so once the pot is gone or
        buried neither branch can run again and the row sits in /health for
        ever, inflating the daily up-probe count that leaves out ONE_SHOT_KEYS
        but not sensor:. `proposal:<c>:<outlet>` is the same shape, and
        without it the next pot on that hose inherits a day of nudge silence.

        Only when nobody alive is left on that pair: another pot may hold the
        channel or the outlet, and its alarm is not this pot's to clear.
        Removing a row is SILENT, unlike clear() — no `cleared` reaches the
        phone, which is the right answer for a condition nobody owns.
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

        The opposite of the graveyard, and deliberately not reachable from the
        same request: the graveyard keeps the record and frees the hardware,
        this keeps nothing. It overturns the command log's "never pruned" rule
        for one reason — the owner asked for the plant to be gone — and it
        costs something real: a deleted pot's doses stop floating the
        hose-keyed cooldown and cap floors, so for up to a day the next pot on
        that hose can be watered sooner than the dry direction wants.

        Order is forced by reachability: there are no foreign keys in this
        database and nothing cascades, so the verdicts and the `dose:<id>`
        ledger rows must go BEFORE the commands they are found through. Both
        must go at all, because a leftover verdict labels a stranger's dose
        and a leftover dose: row makes the judgement loop skip a real dose for
        ever on its NOT EXISTS guard — a silent hole in "tell me when it's
        wrong". commands.id is AUTOINCREMENT so no id is handed out twice,
        which is the belt to this pair of braces.
        """
        with connect(db) as con:
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
                photo_path(photos, pot_id, photo_id).unlink(missing_ok=True)
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
        with connect(db) as con:
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
                # No id is a create, always. Looking the name up here would
                # make a create silently edit whatever pot already answers to
                # it, and the app's own check against that cannot see a pot
                # added from another phone or while its list sat idle. The
                # name clash below refuses it instead.
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
                # would misread or miswater. Asked whatever this pot's own
                # status is, since the point is the OTHER pot; burying is what
                # unplugs, so this is the second line of defence.
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
                    # One hose, one pot. A live pot on this wiring was refused
                    # above and a graveyard one let go of its window when it
                    # was buried, so this should find nothing and stays
                    # because it is the only thing that would: nothing papers
                    # over two open windows on one hose at read time any more,
                    # and the reading stamp would pick one of them arbitrarily
                    # and permanently. Each displaced window closes on its own
                    # edge, so a backwards clock cannot invert it or orphan a
                    # dose it already holds.
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
                # Burying a pot is what UNPLUGS it: the hose and the socket
                # go back to the garden. window_edge, never `now` — a to_ts
                # before a dose the window already holds orphans that dose's
                # cooldown and cap for good.
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
        with connect(db) as con:
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
        with connect(db) as con:
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

    # The alert ticker: the observation window, the up-probe and the loop,
    # in the one object that keeps state between passes.
    ticker = Ticker(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(ticker.run()) if alerts_on else None
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
    app.state.tick = ticker.tick
    app.state.observed = ticker.observed

    def bad_token(request: Request) -> bool:
        given = request.headers.get("x-token", "")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII str,
        # which would turn a garbled header into a 500 instead of a 401.
        return not hmac.compare_digest(
            given.encode("utf-8"), cfg.secret.encode("utf-8")
        )

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
        except Retired as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=409)
        except Latched as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=409)
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

    @app.post("/controller")
    async def controller_knob(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            controller, retired = parse_controller(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(set_retired, controller, retired)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"controller={controller} retired={retired}\n")

    @app.post("/resume")
    async def resume_board(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            controller = parse_board(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(resume, controller)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"resumed={controller}\n")

    @app.post("/refill")
    async def refill(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            controller = parse_board(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            ts = await run_in_threadpool(record_refill, controller)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"refill={ts}\n")

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
            with connect(db) as con:
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
                    # The newest dose this pot was ever handed, with its human
                    # verdict: the id POST /verdict needs. By the stamp, so
                    # moving a hose takes the history with it instead of
                    # filing it under the next pot along — which would judge
                    # one pot's soil against another pot's dose, in the very
                    # table the learning log is made of.
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
        care source's quota for the whole household.
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
            answer = await run_in_threadpool(look_up, db, get_json, care_token, query)
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
        # No GROUP BY: a stamped row has exactly one owner, so no dose here
        # can be listed twice.
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
            with connect(db) as con:
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

        By pot rather than by channel, which is what stops a plant wired into
        a dead one's socket opening its chart onto somebody else's moisture
        curve. Two consequences, both accepted. This route takes no token, so
        an unauthenticated caller can confirm a pot id exists — it answers 200
        with no points for one that does not, so the confirmation is of the id
        and of nothing about the plant. And readings stamped with no pot (an
        environment channel, a socket nobody claimed) are reachable through no
        route at all.
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
            with connect(db) as con:
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
            photo_id = await run_in_threadpool(
                keep_photo, db, photos, pot_id, body, w, h, now
            )
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
            rows = photo_rows(db, photos, pot_id, limit)
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
            blob = await run_in_threadpool(photo_blob, db, photos, photo_id)
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
            await run_in_threadpool(forget_photo, db, photos, photo_id)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse("ok\n")

    @app.get("/hello")
    def hello(request: Request):
        """Is this a butler, and is that the token?

        The one call a phone can make to tell a wrong address from a wrong
        token, which are different mistakes and only one of them is the user's
        to fix. Nothing else here can answer it: the ungated reads answer a
        wrong token as they answer a right one, the photo routes touch the
        database and the disk so their refusals are not only about the token,
        and every other gated route writes something.

        Touches no database, so it stays an answer about the address and the
        token and never about the disk.
        """
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        return PlainTextResponse(f"butler={VERSION}\n")

    @app.get("/health")
    def health():
        try:
            with connect(db) as con:
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
                        "flap": 0,
                        "last_refill": None,
                        "tank_ml": None,
                        "tank_samples": 0,
                        "pumped_ml": 0,
                        "over": 0,
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
                firm_word: dict[int, int | None] = {}
                for (
                    controller, float_ok, pos, err, err_ts, pos_ok_seen, latched_ts, reason,
                    float_firm, flap,
                ) in con.execute(
                    "SELECT controller, float_ok, pos, err, err_ts, pos_ok_seen, "
                    "latched_ts, latch_reason, float_firm, flap FROM status"
                ):
                    e = known.setdefault(controller, entry(controller))
                    e["float"] = float_ok
                    e["flap"] = flap  # why it is 0, when it is: the app says so
                    firm_word[controller] = float_firm
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
                for controller, n in con.execute(
                    "SELECT controller, COUNT(*) FROM tank_samples GROUP BY controller"
                ):
                    e = known.setdefault(controller, entry(controller))
                    e["tank_samples"] = n
                    e["tank_ml"] = tank_ml(con, controller)
                raised = [
                    {"key": key, "raised_ts": ts}
                    for key, ts in con.execute(
                        "SELECT key, raised_ts FROM alerts "
                        f"WHERE {RAISED_SQL} ORDER BY key"
                    )
                ]
                for controller, e in known.items():
                    origin = counter_origin(con, controller)
                    e["pumped_ml"] = (
                        pumped_since(con, controller, origin[0]) if origin else 0
                    )
                    # The same predicate the rules and the ticker use, on the
                    # three numbers the entry already carries; `float` stays
                    # the raw word for the app. Or on the page standing, which
                    # only a tap clears. Retired is the last word, and quiet.
                    e["over"] = int(
                        not e["retired"]
                        and (
                            is_over(
                                e["tank_ml"],
                                e["pumped_ml"],
                                e["float"],
                                firm_word.get(controller),
                            )
                            or over_stands(con, controller)
                        )
                    )
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
