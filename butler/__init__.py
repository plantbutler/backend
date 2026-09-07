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
from .commands import (
    approve,
    enqueue,
    handle_report,
    record_refill,
    record_verdict,
    resume,
    set_interval,
    set_retired,
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
from .garden import (
    advice_for,
    delete_pot,
    dismiss_advice,
    free_alerts,
    upsert_pot,
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

    def clock() -> int:
        """The server's clock, read here and nowhere else below.

        Every write path is handed its `now` rather than reading one, so this
        is the single `time.time()` the whole request side goes through — and
        `butler.time` stays the binding that decides it, which is how two
        tests step the clock backwards over a wiring save without moving
        anybody else's.
        """
        return int(time.time())

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
            next_out, handed = await run_in_threadpool(
                handle_report,
                db,
                parsed,
                clock(),
                interval,
                cmd_ttl,
                cfg.quiet_window,
            )
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
            cmd_id, busy = await run_in_threadpool(enqueue, db, parsed, clock(), cmd_ttl)
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
            effective = await run_in_threadpool(
                set_interval, db, controller, value, interval
            )
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
            await run_in_threadpool(set_retired, db, controller, retired, clock())
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
            await run_in_threadpool(resume, db, controller, clock())
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
            ts = await run_in_threadpool(record_refill, db, controller, clock())
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
            pot_id, name = await run_in_threadpool(upsert_pot, db, parsed, clock())
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
            await run_in_threadpool(delete_pot, db, photos, pot_id)
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
            busy = await run_in_threadpool(approve, db, cmd_id, clock(), cmd_ttl)
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
            await run_in_threadpool(record_verdict, db, cmd_id, verdict, clock())
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
                                clock() - PROPOSAL_TTL_S,
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
                    entry["advice"] = advice_for(con, entry, clock())
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
            await run_in_threadpool(dismiss_advice, db, pot_id, kind, clock())
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
        return JSONResponse({"doses": rows, "now": clock()})

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
        now = clock()
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
        now = clock()
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
                "now": clock(),
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
