"""The one route that asks the outside world anything.
"""

from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import common
from ..care import look_up
from ..config import Config
from ..species import SPECIES_MAX, normalise_species


def router(cfg: Config, clock: Callable[[], int]) -> APIRouter:
    """What is known about a plant by the name somebody typed."""
    api = APIRouter()

    @api.get("/species")
    async def species(request: Request):
        """What is known about a plant by name. Never writes to a pot: the
        numbers a human ends up with are written by POST /pot, by that human.

        The one GET here that asks for the token, because it is the one that
        spends something not ours: an unauthenticated caller could burn the
        care source's quota for the whole household.
        """
        if common.bad_token(request, cfg.secret):
            return PlainTextResponse("bad token\n", status_code=401)
        query = normalise_species(request.query_params.get("q") or "")
        if not query:
            return PlainTextResponse("refused: q= is empty\n", status_code=400)
        if len(query) > SPECIES_MAX:
            return PlainTextResponse(
                f"refused: q= is longer than {SPECIES_MAX} characters\n",
                status_code=400,
            )
        # In the threadpool: two HTTP hops with their own timeouts have
        # no business on the event loop, and neither has the disk.
        answer = await common.worked(
            look_up, cfg.db, cfg.get_json, cfg.care_token, query
        )
        if isinstance(answer, PlainTextResponse):
            return answer
        return JSONResponse(answer)

    return api
