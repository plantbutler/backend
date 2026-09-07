"""The garden: the pots, what they are wired to, and what they were given.

`GET /pots` is the app's home screen and is fetched on every screen open, so
it reads caches and rows only — never the care source, never the disk.
"""

import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import common
from ..care import cached_care
from ..config import Config
from ..constants import PROPOSAL_TTL_S
from ..garden import advice_for, delete_pot, dismiss_advice, upsert_pot
from ..pots import (
    DOSE_KEYS,
    LAST_DOSE_KEYS,
    POT_COLUMNS,
    _hose_since,
    moisture_pct,
)
from ..species import normalise_species
from ..store import connect
from ..wire import (
    parse_advice,
    parse_doses,
    parse_history,
    parse_pot,
    parse_pot_delete,
)


def router(cfg: Config, clock: Callable[[], int]) -> APIRouter:
    """The six routes about a pot: the list, the form, the two histories."""
    api = APIRouter()

    @api.get("/pots")
    def pots():
        try:
            with connect(cfg.db) as con:
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

    @api.post("/pot")
    async def pot(request: Request):
        parsed = await common.taken(request, cfg.secret, parse_pot)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        done = await common.worked(
            upsert_pot, cfg.db, parsed, clock(), refusals={ValueError: 400}
        )
        if isinstance(done, PlainTextResponse):
            return done
        pot_id, name = done
        return PlainTextResponse(f"pot={pot_id} name={name}\n")

    @api.post("/pot/delete")
    async def erase_pot(request: Request):
        """`id=<pot id>`. Its own route rather than a field on /pot, for the
        same reason /photo/delete is: a save that lost its body must never
        become an erasure. Total and with no undo — the graveyard is the
        reversible one."""
        parsed = await common.taken(request, cfg.secret, parse_pot_delete)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        pot_id = parsed
        done = await common.worked(
            delete_pot, cfg.db, cfg.photos, pot_id, refusals={ValueError: 400}
        )
        if isinstance(done, PlainTextResponse):
            return done
        return PlainTextResponse("ok\n")

    @api.post("/advice")
    async def advice(request: Request):
        """`pot=<id> kind=target dismiss=1` — this offer was seen and
        refused. Only the refusal is stored; accepting an offer is an
        ordinary POST /pot, so no watering number is ever written from here.
        """
        parsed = await common.taken(request, cfg.secret, parse_advice)
        if isinstance(parsed, PlainTextResponse):
            return parsed
        pot_id, kind = parsed
        done = await common.worked(
            dismiss_advice, cfg.db, pot_id, kind, clock(), refusals={ValueError: 400}
        )
        if isinstance(done, PlainTextResponse):
            return done
        return PlainTextResponse("ok\n")

    @api.get("/doses")
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
            with connect(cfg.db) as con:
                rows = [dict(zip(DOSE_KEYS, row)) for row in con.execute(sql, args)]
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return JSONResponse({"doses": rows, "now": clock()})

    @api.get("/history")
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
            with connect(cfg.db) as con:
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

    return api
