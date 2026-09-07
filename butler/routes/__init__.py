"""The routes, grouped by what they answer about.

Each module builds one router from the configuration and the clock, and
nothing else: a route knows the shape of a request and the shape of a reply,
and every decision behind it belongs to the module it calls. `common` holds
the preamble twelve of the write routes repeat.
"""

from collections.abc import Callable

from fastapi import APIRouter

from . import board, common, garden, photos, service, species
from ..config import Config


def routers(cfg: Config, clock: Callable[[], int]) -> list[APIRouter]:
    """Every router, in the order create_app mounts them.

    No two routes here answer the same method and path, so the order is for
    a reader rather than for the matcher: the board's wire first, then the
    garden the app spends its time in, then the pictures, the lookup, and
    the two questions about the butler itself.
    """
    return [
        board.router(cfg, clock),
        garden.router(cfg, clock),
        photos.router(cfg, clock),
        species.router(cfg, clock),
        service.router(cfg, clock),
    ]
