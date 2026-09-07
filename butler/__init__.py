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

# The container installs no package — it copies this one beside fastapi and
# runs it — so the version lives here. A test holds it to pyproject.toml.
# Above the imports because routes/service.py reads it back for GET /hello,
# and this file's own imports are what pull that module in.
VERSION = "0.20.0"


import asyncio
import contextlib
import sqlite3
import sys
import time
from collections.abc import Callable

from fastapi import FastAPI

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
from .routes import routers
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
    cfg.db.parent.mkdir(parents=True, exist_ok=True)
    cfg.photos.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.db) as bootstrap:
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
        migrate(bootstrap, str(cfg.db))
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
        task = asyncio.create_task(ticker.run()) if cfg.alerts_on else None
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

    # The routes themselves are in `routes/`, one module per thing they
    # answer about. Each is built from the configuration and the clock and
    # holds nothing else: this factory is what wires them to a database.
    for api in routers(cfg, clock):
        app.include_router(api)

    return app
