"""The board's own wire, and the knobs a person turns on one board.

POST /report is the only route the Arduino ever calls, and everything else
here is the app asking for the same slot: one command at a time, queued,
approved or judged. Every one of them refuses rather than waters.
"""

from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from . import common
from ..commands import (
    approve,
    enqueue,
    handle_report,
    record_refill,
    record_verdict,
    resume,
    set_interval,
    set_retired,
)
from ..config import Config
from ..tank import Latched, Retired
from ..wire import (
    parse_approve,
    parse_board,
    parse_command,
    parse_controller,
    parse_interval,
    parse_report,
    parse_verdict,
)


def router(cfg: Config, clock: Callable[[], int]) -> APIRouter:
    """The eight routes about a board and its one command slot."""
    api = APIRouter()

    @api.post("/report")
    async def report(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_report)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        done = await common.worked(
            handle_report,
            cfg.db,
            parsed,
            clock(),
            cfg.interval,
            cfg.cmd_ttl,
            cfg.quiet_window,
        )
        if isinstance(done, PlainTextResponse):
            return done
        next_out, handed = done
        answer = f"next={next_out}\n"
        if handed:
            cmd_id, kind, outlet, ml, cap_s = handed
            if kind == "water":
                answer += f"cmd={cmd_id} water={outlet} ml={ml} cap_s={cap_s}\n"
            else:
                answer += f"cmd={cmd_id} stop=1\n"
        return PlainTextResponse(answer)

    @api.post("/command")
    async def command(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_command)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        done = await common.worked(
            enqueue,
            cfg.db,
            parsed,
            clock(),
            cfg.cmd_ttl,
            refusals={Retired: 409, Latched: 409},
        )
        if isinstance(done, PlainTextResponse):
            return done
        cmd_id, busy = done
        if busy:
            return PlainTextResponse(
                f"busy: cmd={busy[0]} state={busy[1]}\n", status_code=409
            )
        return PlainTextResponse(f"cmd={cmd_id}\n")

    @api.post("/interval")
    async def interval_knob(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_interval)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        controller, value = parsed
        if value and 2 * value > cfg.cmd_ttl:
            return PlainTextResponse(
                f"refused: next={value} would let a live board outlive the "
                f"command TTL ({cfg.cmd_ttl}s); raise BUTLER_CMD_TTL_S first\n",
                status_code=400,
            )
        effective = await common.worked(
            set_interval, cfg.db, controller, value, cfg.interval
        )
        if isinstance(effective, PlainTextResponse):
            return effective
        return PlainTextResponse(f"next={effective}\n")

    @api.post("/controller")
    async def controller_knob(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_controller)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        controller, retired = parsed
        done = await common.worked(
            set_retired, cfg.db, controller, retired, clock()
        )
        if isinstance(done, PlainTextResponse):
            return done
        return PlainTextResponse(f"controller={controller} retired={retired}\n")

    @api.post("/resume")
    async def resume_board(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_board)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        controller = parsed
        done = await common.worked(resume, cfg.db, controller, clock())
        if isinstance(done, PlainTextResponse):
            return done
        return PlainTextResponse(f"resumed={controller}\n")

    @api.post("/refill")
    async def refill(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_board)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        controller = parsed
        ts = await common.worked(record_refill, cfg.db, controller, clock())
        if isinstance(ts, PlainTextResponse):
            return ts
        return PlainTextResponse(f"refill={ts}\n")

    @api.post("/approve")
    async def approve_proposal(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_approve)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        cmd_id = parsed
        busy = await common.worked(
            approve, cfg.db, cmd_id, clock(), cfg.cmd_ttl, refusals={ValueError: 400}
        )
        if isinstance(busy, PlainTextResponse):
            return busy
        if busy:
            return PlainTextResponse(
                f"busy: cmd={busy[0]} state={busy[1]}\n", status_code=409
            )
        return PlainTextResponse(f"cmd={cmd_id}\n")

    @api.post("/verdict")
    async def verdict_knob(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_verdict)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        cmd_id, verdict = parsed
        done = await common.worked(
            record_verdict,
            cfg.db,
            cmd_id,
            verdict,
            clock(),
            refusals={ValueError: 400},
        )
        if isinstance(done, PlainTextResponse):
            return done
        return PlainTextResponse(f"cmd={cmd_id} verdict={verdict}\n")

    return api
