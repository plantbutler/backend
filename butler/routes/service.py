"""Is this a butler, and is it well? The two routes that are about it.
"""

import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import common
from .. import VERSION
from ..config import Config
from ..pots import RAISED_SQL
from ..store import connect
from ..tank import counter_origin, is_over, over_stands, pumped_since, tank_ml


def router(cfg: Config, clock: Callable[[], int]) -> APIRouter:
    """The handshake a phone makes on setup, and the whole picture for /health."""
    api = APIRouter()

    @api.get("/hello")
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
        if common.bad_token(request, cfg.secret):
            return PlainTextResponse("bad token\n", status_code=401)
        return PlainTextResponse(f"butler={VERSION}\n")

    @api.get("/health")
    def health():
        try:
            with connect(cfg.db) as con:
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
                "next_default": cfg.interval,
                "controllers": [known[k] for k in sorted(known)],
                "alerts": raised,
            }
        )

    return api
