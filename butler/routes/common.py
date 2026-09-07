"""What a route does before it does its own work, and what twelve of them share.

Twelve write routes repeat one preamble: check the token, read the body under
the cap, parse it, run the work off the event loop, and turn a refusal into a
400 (or a 409) and a locked database into a 503. It lives here in two halves,
because most of those routes have something of their own to say between them —
an extra refusal, a different status, an answer built out of what was parsed.

Every failure is a plain sentence with a status. Nothing here raises: a
traceback reaching the board would be a 500 where a 400 or a 503 is the truth,
and the board's own answer to a 500 is to try again for ever.
"""

from collections.abc import Callable

import hmac
import sqlite3
from typing import Any

from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .. import constants


def bad_token(request: Request, secret: str) -> bool:
    given = request.headers.get("x-token", "")
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str,
    # which would turn a garbled header into a 500 instead of a 401.
    return not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))


async def slurp(
    request: Request, cap: int = constants.BODY_CAP
) -> bytes | PlainTextResponse:
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


async def taken(
    request: Request, secret: str, parse: Callable[[str], Any]
) -> Any | PlainTextResponse:
    """The first half of the preamble: the token, the body, the parser.

    Answers what the parser made of the body, or the response that refuses
    it — 401 for the token, 413 for a body over the cap, 400 for a client
    that went away and for anything the parser would not take.
    """
    if bad_token(request, secret):
        return PlainTextResponse("bad token\n", status_code=401)
    body = await slurp(request)
    if isinstance(body, PlainTextResponse):
        return body
    try:
        return parse(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as why:
        return PlainTextResponse(f"refused: {why}\n", status_code=400)


async def worked(
    work: Callable,
    *args: Any,
    refusals: dict[type[BaseException], int] | None = None,
) -> Any | PlainTextResponse:
    """The second half: the work, off the event loop, and what it refuses.

    In the threadpool, because a stalled disk must not freeze the event
    loop. `refusals` names the exceptions this particular route treats as
    the caller's fault and the status each answers with — a ValueError is
    usually a 400, the two the command slot raises are 409s. A locked
    database is a 503 everywhere, and is the only one not worth naming.
    """
    refusals = refusals or {}
    try:
        return await run_in_threadpool(work, *args)
    except tuple(refusals) as why:
        status = next(s for kind, s in refusals.items() if isinstance(why, kind))
        return PlainTextResponse(f"refused: {why}\n", status_code=status)
    except sqlite3.OperationalError as why:
        return PlainTextResponse(f"try again: {why}\n", status_code=503)
