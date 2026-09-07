"""A pot: made, edited, buried, erased — and the offer it is shown.

Three of these are one write and a refusal. The fourth, `delete_pot`, is the
only thing in this service that overturns "the command log is never pruned",
and its order is forced by reachability: nothing cascades in this database, so
what is found THROUGH the commands goes before them.

Burying and erasing both drop the hose-keyed alerts the pot leaves behind,
which is what `free_alerts` is for: `sensor:` and `proposal:` are raised and
cleared inside loops over the live garden, so once a pot has left it neither
branch can ever run again.

`now` is passed in rather than read here: the facade owns the clock, and a
wiring save is the one write two tests step it backwards over.
"""

import contextlib
import sqlite3
import time
from pathlib import Path

from . import pots, schema, store, wire

# By name, because both advice functions bind a local called `band` — the
# offer they are about to make — which would shadow the module for the rest
# of the function.
from .band import target_band


def advice_for(con: sqlite3.Connection, entry: dict, now: int) -> dict | None:
    """The band this pot would be offered, or None when there is nothing
    to say: the pot is off, it already holds those numbers, or the
    person has already refused this exact offer. A different offer — a
    new season, a repot, another soil — is a new question and is asked.
    """
    if not pots.waters(entry["status"]):
        return None
    band = target_band(
        entry["plant_type"],
        entry["soil"],
        entry["pot_diameter_cm"],
        entry["plant_height_cm"],
        time.localtime(now).tm_mon,
    )
    if (entry["target_low_pct"], entry["target_high_pct"]) == (band.low, band.high):
        return None
    row = con.execute(
        "SELECT fingerprint FROM advice_dismissed WHERE pot_id = ? AND kind = ?",
        (entry["id"], "target"),
    ).fetchone()
    if row and row[0] == f"{band.low}-{band.high}":
        return None
    return {"kind": "target", "low": band.low, "high": band.high, "why": band.why}

def dismiss_advice(db: Path, pot_id: str, kind: str, now: int) -> None:
    with store.connect(db) as con:
        row = con.execute(
            "SELECT id, plant_type, soil, pot_diameter_cm, plant_height_cm, "
            "target_low_pct, target_high_pct FROM pots_now WHERE id = ?",
            (pot_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no pot {pot_id}")
        band = target_band(
            row[1], row[2], row[3], row[4], time.localtime(now).tm_mon
        )
        con.execute(
            "INSERT OR REPLACE INTO advice_dismissed "
            "(pot_id, kind, fingerprint, ts) VALUES (?, ?, ?, ?)",
            (pot_id, kind, f"{band.low}-{band.high}", now),
        )

def free_alerts(
    con: sqlite3.Connection,
    pot_id: str,
    controller: str | None,
    channel: int | None,
    outlet: int | None,
) -> None:
    """Drop the hose-keyed alerts a pot leaves behind when it lets go of
    its wiring — by being buried or by being erased.

    `sensor:<c>:<ch>` is unclearable without this: both its raise and its
    clear live inside a loop over pots_now, so once the pot is gone or
    buried neither branch can run again and the row sits in /health for
    ever, inflating the daily up-probe count that leaves out ONE_SHOT_KEYS
    but not sensor:. `proposal:<c>:<outlet>` is the same shape, and
    without it the next pot on that hose inherits a day of nudge silence.

    Only when nobody alive is left on that pair: another pot may hold the
    channel or the outlet, and its alarm is not this pot's to clear.
    Removing a row is SILENT, unlike clear() — no `cleared` reaches the
    phone, which is the right answer for a condition nobody owns.
    """
    for key, col, value in (
        (f"sensor:{controller}:{channel}", "m.channel", channel),
        (f"proposal:{controller}:{outlet}", "m.outlet", outlet),
    ):
        if controller is None or value is None:
            continue
        taken = con.execute(
            "SELECT 1 FROM pot_mappings m JOIN pots p ON p.id = m.pot_id "
            "WHERE m.to_ts IS NULL AND m.pot_id != ? AND m.controller = ? "
            f"AND {col} = ? AND {pots.live_sql('p.status')} LIMIT 1",
            (pot_id, controller, value),
        ).fetchone()
        if not taken:
            con.execute("DELETE FROM alerts WHERE key = ?", (key,))

def delete_pot(db: Path, photos: Path, pot_id: str) -> None:
    """Erase a pot and everything that is only about it.

    The opposite of the graveyard, and deliberately not reachable from the
    same request: the graveyard keeps the record and frees the hardware,
    this keeps nothing. It overturns the command log's "never pruned" rule
    for one reason — the owner asked for the plant to be gone — and it
    costs something real: a deleted pot's doses stop floating the
    hose-keyed cooldown and cap floors, so for up to a day the next pot on
    that hose can be watered sooner than the dry direction wants.

    Order is forced by reachability: there are no foreign keys in this
    database and nothing cascades, so the verdicts and the `dose:<id>`
    ledger rows must go BEFORE the commands they are found through. Both
    must go at all, because a leftover verdict labels a stranger's dose
    and a leftover dose: row makes the judgement loop skip a real dose for
    ever on its NOT EXISTS guard — a silent hole in "tell me when it's
    wrong". commands.id is AUTOINCREMENT so no id is handed out twice,
    which is the belt to this pair of braces.
    """
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        photo_ids = [
            row[0]
            for row in con.execute(
                "SELECT id FROM photos WHERE pot_id = ?", (pot_id,)
            )
        ]
        # The board is holding a dose for this pot: it will pour, and
        # it will ack an id that no longer exists. Deleting the row also
        # frees the controller's one slot while the water is still
        # running, so the next command goes out on top of it.
        if con.execute(
            "SELECT 1 FROM commands WHERE pot_id = ? AND state = 'sent' LIMIT 1",
            (pot_id,),
        ).fetchone():
            raise ValueError(
                f"the board is holding a dose for {pot_id}: "
                "try again after its next report"
            )
        wiring = con.execute(
            "SELECT controller, channel, outlet FROM pot_mappings "
            "WHERE pot_id = ? AND to_ts IS NULL",
            (pot_id,),
        ).fetchone() or (None, None, None)
        con.execute(
            "DELETE FROM verdicts WHERE command_id IN "
            "(SELECT id FROM commands WHERE pot_id = ?)",
            (pot_id,),
        )
        con.execute(
            "DELETE FROM alerts WHERE key IN "
            "(SELECT 'dose:' || id FROM commands WHERE pot_id = ?)",
            (pot_id,),
        )
        free_alerts(con, pot_id, *wiring)
        for table in ("commands", "readings", "photos", "advice_dismissed"):
            con.execute(f"DELETE FROM {table} WHERE pot_id = ?", (pot_id,))
        con.execute("DELETE FROM pot_mappings WHERE pot_id = ?", (pot_id,))
        # The DELETE decides, never a SELECT before it: a bare read takes
        # no lock, so two concurrent deletes would both see the row and
        # both answer ok. Raising here rolls the whole thing back, which
        # is why the photograph files are only touched afterwards.
        if con.execute("DELETE FROM pots WHERE id = ?", (pot_id,)).rowcount == 0:
            raise ValueError(f"no such pot: {pot_id}")
    # After the commit, with no connection held. A pot with 300
    # photographs is 300 unlinks, and inside BEGIN IMMEDIATE that is 300
    # unlinks of blocked /report. A file no row knows about is invisible
    # and harmless; a row whose file has gone reads `missing` for ever —
    # so the row goes first and the bytes follow, never the other way.
    for photo_id in photo_ids:
        with contextlib.suppress(OSError):
            store.photo_path(photos, pot_id, photo_id).unlink(missing_ok=True)
    # rmdir, not rmtree: the directory is not the truth, and a tree
    # delete would take bytes belonging to rows this transaction never
    # selected — including one the keep_photo race can create.
    with contextlib.suppress(OSError):
        (photos / pot_id).rmdir()

def upsert_pot(db: Path, fields: dict, now: int) -> tuple[str, str]:
    """Create or partially update one pot, refusing inconsistent merges.

    `id=` edits that pot, name included, so renaming is an ordinary
    field edit. A bare `name=` creates and mints an id. Validation runs
    on the MERGED row, stored values plus this request, so `dry_raw`
    today and `wet_raw` tomorrow is refused just like both at once.
    Column names come from the parse_pot whitelist, never the wire.

    Mapping keys land in pot_mappings, not pots: a changed wiring
    closes the open row and opens another, so past readings stay
    attributed to the pot that was actually on that channel.
    """
    pot_id = fields.get("id")
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        if pot_id is not None:
            row = con.execute(
                f"SELECT {', '.join(pots.POT_COLUMNS)} FROM pots_now WHERE id = ?",
                (pot_id,),
            ).fetchone()
            if row is None:
                # An edit of a pot that is not there is a bug in the
                # caller, not an invitation to create one under a name
                # nobody asked for.
                raise ValueError(f"no pot {pot_id}")
        else:
            # No id is a create, always. Looking the name up here would
            # make a create silently edit whatever pot already answers to
            # it, and the app's own check against that cannot see a pot
            # added from another phone or while its list sat idle. The
            # name clash below refuses it instead.
            row = None
        current = (
            dict(zip(pots.POT_COLUMNS, row))
            if row
            else dict.fromkeys(pots.POT_COLUMNS)
            | {"mode": "manual", "status": "alive", "id": schema.new_pot_id()}
        )
        # The id is minted into `current` before the merge, so a create
        # and an edit take one path from here on.
        sets = {k: v for k, v in fields.items() if k != "id"}
        merged = current | sets
        pot_id = current["id"]
        name = merged["name"]
        if merged["dry_raw"] is not None and merged["dry_raw"] == merged["wet_raw"]:
            raise ValueError("dry_raw and wet_raw must differ")
        if (
            merged["target_low_pct"] is not None
            and merged["target_high_pct"] is not None
            and merged["target_low_pct"] >= merged["target_high_pct"]
        ):
            raise ValueError("target_low_pct must be below target_high_pct")
        if merged["status"] == "graveyard" and any(
            k in sets for k in wire.POT_MAP_FIELDS
        ):
            # parse_pot refuses the two in one body; this catches the two
            # in two bodies, which is the same contradiction spread out.
            # Asked of the MERGED status, so restoring and wiring
            # together still goes through — that one is not a
            # contradiction, it is how a plant comes back.
            raise ValueError(
                "a graveyard pot holds no wiring: bring it back first"
            )
        clash = con.execute(
            "SELECT id FROM pots WHERE name = ? AND id != ?", (name, pot_id)
        ).fetchone()
        if clash:
            # The UNIQUE index would refuse this anyway, with a message
            # nobody outside sqlite can read.
            if fields.get("id") is None:
                raise ValueError(
                    f"the name {name} is taken by pot {clash[0]} "
                    "— open it instead of creating one"
                )
            raise ValueError(f"the name {name} is taken by pot {clash[0]}")
        if merged["controller"] is not None:
            # Two pots on one sensor or one hose is a config error that
            # would misread or miswater. Asked whatever this pot's own
            # status is, since the point is the OTHER pot; burying is what
            # unplugs, so this is the second line of defence.
            for col in ("channel", "outlet"):
                if merged[col] is None:
                    continue
                other = con.execute(
                    f"SELECT name FROM pots_now WHERE controller = ? AND {col} = ? "
                    f"AND {pots.live_sql()} AND id != ? LIMIT 1",
                    (merged["controller"], merged[col], pot_id),
                ).fetchone()
                if other:
                    raise ValueError(
                        f"{col} {merged[col]} on {merged['controller']} "
                        f"is taken by pot {other[0]}"
                    )
        pot_sets = {k: v for k, v in sets.items() if k not in wire.POT_MAP_FIELDS}
        if row and pot_sets:
            con.execute(
                f"UPDATE pots SET {', '.join(k + ' = ?' for k in pot_sets)} "
                "WHERE id = ?",
                [*pot_sets.values(), pot_id],
            )
        elif not row:
            keys = [
                k for k in merged if k in pots.POT_COLUMNS and k not in wire.POT_MAP_FIELDS
            ]
            con.execute(
                f"INSERT INTO pots ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' * len(keys))})",
                [merged[k] for k in keys],
            )
        if any(k in sets for k in wire.POT_MAP_FIELDS):
            wiring = tuple(merged[k] for k in wire.POT_MAP_FIELDS)
            # Only when the wiring actually differs: otherwise saving an
            # unrelated field would fragment the history into a row per
            # save, and every one of those windows would be a lie.
            if wiring != tuple(current[k] for k in wire.POT_MAP_FIELDS):
                # One second for both rows, so the windows stay
                # contiguous — the attribution join assumes it.
                edge = pots.window_edge(con, pot_id, now)
                con.execute(
                    "UPDATE pot_mappings SET to_ts = ? "
                    "WHERE pot_id = ? AND to_ts IS NULL",
                    (edge, pot_id),
                )
                # One hose, one pot. A live pot on this wiring was refused
                # above and a graveyard one let go of its window when it
                # was buried, so this should find nothing and stays
                # because it is the only thing that would: nothing papers
                # over two open windows on one hose at read time any more,
                # and the reading stamp would pick one of them arbitrarily
                # and permanently. Each displaced window closes on its own
                # edge, so a backwards clock cannot invert it or orphan a
                # dose it already holds.
                displaced = con.execute(
                    "SELECT DISTINCT m.pot_id FROM pot_mappings m "
                    "JOIN pots p ON p.id = m.pot_id "
                    "WHERE m.to_ts IS NULL AND m.pot_id != ? "
                    "AND m.controller = ? AND (m.channel = ? OR m.outlet = ?)",
                    (pot_id, *wiring),
                ).fetchall()
                for (other_id,) in displaced:
                    con.execute(
                        "UPDATE pot_mappings SET to_ts = ? "
                        "WHERE pot_id = ? AND to_ts IS NULL",
                        (pots.window_edge(con, other_id, now), other_id),
                    )
                con.execute(
                    "INSERT INTO pot_mappings "
                    "(pot_id, controller, channel, outlet, from_ts, to_ts) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (pot_id, *wiring, edge),
                )
                # The socket it just left, for the same reason burying
                # one does it: `sensor:<c>:<ch>` is raised and cleared
                # inside a loop over the pot's CURRENT wiring, so an
                # alarm on the old channel has nobody left to clear it.
                free_alerts(
                    con,
                    pot_id,
                    current["controller"],
                    current["channel"],
                    current["outlet"],
                )
        if sets.get("status") == "graveyard" and current["status"] != "graveyard":
            # Burying a pot is what UNPLUGS it: the hose and the socket
            # go back to the garden. window_edge, never `now` — a to_ts
            # before a dose the window already holds orphans that dose's
            # cooldown and cap for good.
            edge = pots.window_edge(con, pot_id, now)
            con.execute(
                "UPDATE pot_mappings SET to_ts = ? "
                "WHERE pot_id = ? AND to_ts IS NULL",
                (edge, pot_id),
            )
            # No new window: it comes back unwired, because the plant
            # that comes back is not in the socket the old one left.
            # 'queued' as well as 'proposed': burial hands the outlet
            # back to the garden, so a dose still waiting for the board
            # would pour into whatever is wired there next. A 'sent' one
            # is already with the board and expires on its next report.
            con.execute(
                "UPDATE commands SET state = 'expired' "
                "WHERE pot_id = ? AND state IN ('proposed', 'queued')",
                (pot_id,),
            )
            free_alerts(
                con,
                pot_id,
                current["controller"],
                current["channel"],
                current["outlet"],
            )
    return pot_id, name
