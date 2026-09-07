"""The four photograph routes, and the only gated reads in the service.

Everything else here is numbers about plants. A photograph is the one thing
that could show the inside of somebody's house, so the strip and the bytes
ask for the token like the writes do — which costs nothing, the app puts it
on every GET already.
"""

import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import common
from ..config import Config
from ..constants import JPEG_HEAD, PHOTO_CAP, SAFE_ID
from ..store import forget_photo, keep_photo, photo_blob, photo_rows
from ..wire import parse_photo, parse_photo_delete, parse_photos


def router(cfg: Config, clock: Callable[[], int]) -> APIRouter:
    """Uploading a picture, listing a pot's strip, serving one, forgetting one."""
    api = APIRouter()

    @api.post("/photo")
    async def add_photo(request: Request):
        """`?pot=<id>&w=&h=` with the JPEG as the body.

        JPEG only, checked by its first bytes rather than by what the
        uploader called it. The store then holds one kind of file, so what
        goes back out can always be labelled image/jpeg and never sniffed
        by a browser into something it would run.
        """
        if common.bad_token(request, cfg.secret):
            return PlainTextResponse("bad token\n", status_code=401)
        try:
            pot_id, w, h = parse_photo(request.query_params)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        body = await common.slurp(request, PHOTO_CAP)
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
                keep_photo, cfg.db, cfg.photos, pot_id, body, w, h, now
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

    @api.get("/photos")
    def list_photos(request: Request):
        """`?pot=<id>&limit=`: one pot's strip, newest first.

        Gated, unlike every other read here, and so is the picture itself.
        The rest of them are numbers about plants; these are the one thing
        in this system that could show the inside of somebody's house. It
        costs nothing — the app puts the token on every GET already.
        """
        if common.bad_token(request, cfg.secret):
            return PlainTextResponse("bad token\n", status_code=401)
        try:
            pot_id, limit = parse_photos(request.query_params)
        except ValueError as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            rows = photo_rows(cfg.db, cfg.photos, pot_id, limit)
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

    @api.get("/photo/{photo_id}")
    async def get_photo(photo_id: str, request: Request):
        if common.bad_token(request, cfg.secret):
            return PlainTextResponse("bad token\n", status_code=401)
        if not SAFE_ID.fullmatch(photo_id):
            return PlainTextResponse("refused: not a photo id\n", status_code=400)
        blob = await common.worked(
            photo_blob, cfg.db, cfg.photos, photo_id, refusals={ValueError: 404}
        )
        if isinstance(blob, PlainTextResponse):
            return blob
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

    @api.post("/photo/delete")
    async def delete_photo(request: Request):
        """`photo=<id>`. Its own route rather than a field on /photo: that
        one carries a picture, and losing a body must never become a
        deletion."""
        parsed = await common.taken(request, cfg.secret, parse_photo_delete)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        photo_id = parsed
        done = await common.worked(
            forget_photo, cfg.db, cfg.photos, photo_id, refusals={ValueError: 400}
        )
        if isinstance(done, PlainTextResponse):
            return done
        return PlainTextResponse("ok\n")

    return api
