"""Plant Butler backend.

One container on the NAS, LAN only. The board talks first: one plain-HTTP
POST per report interval, `k=v` tokens in the body, one static token in the
`X-Token` header. The response is `k=v` too and carries the next report
interval — later, at most one pending command (see AGENTS.md for the sketch).

Storage is stdlib sqlite3 on a bind-mounted volume, schema in `schema.sql`,
WAL so a reader never blocks the writer. Timestamps are stamped on arrival:
the board has no clock worth trusting.

A malformed report is refused WHOLE with a 400 — half a report stored looks
exactly like a working system with dead sensors, and a board bug should be
loud. Strictness covers the encoding too: invalid UTF-8 is a refusal, not a
repair, because one flipped byte in `c=` would otherwise mint a phantom
controller and quietly fork the readings. Unknown KEYS, by contrast, are
ignored on purpose: the board will grow `float=`, `pos=`, `ack=` and friends
before this service learns to read them, and a report must land whole the day
either side updates first.

The board's failure mode is the design load-bearer: firmware retries a report
once when the response is lost, so an identical `t=` (board uptime, ms) from
the same controller within a short window is the same report arriving twice
and is answered 200 without storing again. The window matters — uptime
restarts at every reboot, so old `t` values legitimately recur, and a
permanent uniqueness rule would silently drop genuine readings.
"""

import hmac
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

BODY_CAP = 4096  # a full 15-channel report is ~200 bytes; 4 KB is generous
RETRY_WINDOW_S = 300  # how long an identical (controller, t) counts as a retry
MAX_CHANNEL = 255
MAX_RAW = 2**31  # 14-bit ADC today; headroom without letting 2**63 near sqlite


def parse_report(text: str) -> tuple[str, dict[int, int], int | None]:
    """`k=v` tokens, whitespace-separated; line breaks are whitespace too.

    Returns (controller, channels, board uptime `t=` in ms or None). Strict
    about shape — a malformed token, a duplicate key, a non-integer or
    out-of-range value refuses the whole report — and silent about unknown
    keys, for the reasons the module docstring gives. ASCII digits only in a
    channel key: Unicode digits would alias onto ASCII channel numbers.
    """
    controller = ""
    channels: dict[int, int] = {}
    t: int | None = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller:
                raise ValueError("c= given twice")
            controller = value
        elif key == "t":
            if t is not None:
                raise ValueError("t= given twice")
            try:
                t = int(value)
            except ValueError:
                raise ValueError(f"t= is not an integer: {value!r}") from None
            if not 0 <= t < 2**63:
                raise ValueError(f"t= out of range: {value}")
        elif key.startswith("ch") and key[2:].isascii() and key[2:].isdigit():
            channel = int(key[2:])
            if channel > MAX_CHANNEL:
                raise ValueError(f"channel index out of range: {key}")
            if channel in channels:
                raise ValueError(f"channel given twice: {key}")
            try:
                raw = int(value)
            except ValueError:
                raise ValueError(f"channel {key} is not an integer: {value!r}") from None
            if not 0 <= raw < MAX_RAW:
                raise ValueError(f"channel {key} out of range: {value}")
            channels[channel] = raw
    if not controller:
        raise ValueError("no c= in the report")
    if not channels:
        raise ValueError("no chN= in the report")
    return controller, channels, t


def create_app(
    db_path: str | None = None,
    token: str | None = None,
    next_s: int | None = None,
) -> FastAPI:
    """Everything configurable comes from the environment, overridable for tests.

    Refusals to start, all of them loud and specific: a missing token (this
    listens on a LAN with other people's devices on it, and "forgot to set the
    token" must not be a working deployment); a BUTLER_NEXT_S that is not an
    integer (the alternative is a crash-looping container with a bare
    traceback); a BUTLER_DB under /data when /data is not actually a mount (a
    forgotten bind mount would store readings in the container's own layer and
    lose them all on the next recreate, while looking perfectly healthy).
    """
    db = Path(db_path or os.environ.get("BUTLER_DB", "/data/butler.db"))
    secret = token if token is not None else os.environ.get("BUTLER_TOKEN", "")
    if not secret:
        raise ValueError("BUTLER_TOKEN is not set; refusing to serve without one")
    raw_interval = str(next_s) if next_s is not None else (os.environ.get("BUTLER_NEXT_S") or "60")
    try:
        interval = int(raw_interval)
    except ValueError:
        raise ValueError(
            f"BUTLER_NEXT_S must be an integer number of seconds, got {raw_interval!r}"
        ) from None

    if db.parent == Path("/data") and not os.path.ismount("/data"):
        raise ValueError(
            "BUTLER_DB is under /data but /data is not a mounted volume; "
            "refusing to store readings in the container layer"
        )
    db.parent.mkdir(parents=True, exist_ok=True)
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with sqlite3.connect(db) as bootstrap:
        bootstrap.executescript(schema)

    def connect() -> sqlite3.Connection:
        con = sqlite3.connect(db, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def store(controller: str, channels: dict[int, int], t: int | None) -> None:
        """One report, one transaction; an identical retry lands exactly once."""
        now = int(time.time())
        with connect() as con:
            if t is not None:
                seen = con.execute(
                    "SELECT 1 FROM readings WHERE controller = ? AND t = ? AND ts >= ? LIMIT 1",
                    (controller, t, now - RETRY_WINDOW_S),
                ).fetchone()
                if seen:
                    return
            con.executemany(
                "INSERT INTO readings (ts, controller, channel, raw, t) VALUES (?, ?, ?, ?, ?)",
                [(now, controller, ch, raw, t) for ch, raw in sorted(channels.items())],
            )

    app = FastAPI()

    @app.post("/report")
    async def report(request: Request):
        given = request.headers.get("x-token", "")
        # Bytes, not str: compare_digest raises TypeError on non-ASCII str,
        # which would turn a garbled header into a 500 instead of a 401.
        if not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8")):
            return PlainTextResponse("bad token\n", status_code=401)
        body = b""
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > BODY_CAP:
                    return PlainTextResponse("report too large\n", status_code=413)
        except ClientDisconnect:
            # Half-sent body on a WiFi drop: the client is gone, the response
            # goes nowhere, and a traceback per drop would just fill the log.
            return PlainTextResponse("client went away\n", status_code=400)
        try:
            text = body.decode("utf-8")
            controller, channels, t = parse_report(text)
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            # In the threadpool: a stalled disk must not freeze the event loop.
            await run_in_threadpool(store, controller, channels, t)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"next={interval}\n")

    @app.get("/health")
    def health():
        with connect() as con:
            count, last = con.execute("SELECT COUNT(*), MAX(ts) FROM readings").fetchone()
            controllers = [
                row[0]
                for row in con.execute("SELECT DISTINCT controller FROM readings ORDER BY 1")
            ]
        return JSONResponse(
            {"ok": True, "readings": count, "last_ts": last, "controllers": controllers}
        )

    return app
