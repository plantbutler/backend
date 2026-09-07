"""What counts as a live pot, and which hose it was on when.

The allow-list is here twice, once in Python and once as SQL, because both
halves have to answer the same way: a status this build has never heard of
must not water, propose or page in either language.
"""

import sqlite3


# A positive allow-list, never `!= 'graveyard'`: a status this build has not
# heard of — a newer backend's word reaching an older reader — must not water,
# propose or page. The failure direction is dry.
LIVE_STATUSES = ("alive",)


def waters(status: str | None) -> bool:
    """Whether a pot in this state may be watered, proposed for or alarmed
    about. The Python half of the allow-list; live_sql() is the SQL half."""
    return status in LIVE_STATUSES


def live_sql(col: str = "status") -> str:
    """The same allow-list as a SQL predicate. One source, so a fourth
    status cannot be admitted by one reader and refused by another."""
    return f"{col} IN (" + ", ".join(f"'{s}'" for s in LIVE_STATUSES) + ")"


def moisture_pct(raw: int, dry_raw: int | None, wet_raw: int | None) -> int | None:
    """Linear between the two calibration points, clamped to 0..100.

    None while uncalibrated. Works whichever way the sensor counts (dry
    high or dry low) because both endpoints are stored. Derived at read
    time and never stored: recalibrating reinterprets history.
    """
    if dry_raw is None or wet_raw is None or dry_raw == wet_raw:
        return None
    pct = (dry_raw - raw) * 100 / (dry_raw - wet_raw)
    return max(0, min(100, round(pct)))


# A pot the rules can water on a board (the one `?`): live, on a channel and
# an outlet, calibrated, with a target and a dose. The ladder's candidates and
# the stale page's "a try can still come" are one predicate, so the page
# cannot wait on a pot the ladder would skip.
RULES_POT_SQL = (
    f"{live_sql()} AND controller = ? AND channel IS NOT NULL "
    "AND outlet IS NOT NULL AND dry_raw IS NOT NULL AND wet_raw IS NOT NULL "
    "AND target_low_pct IS NOT NULL AND dose_ml IS NOT NULL"
)


# The key families that are recorded and never cleared: a dose judgement, a
# hose's failure floor, a tank announcement, a proposal nudge, the ticker's own
# rows. What stands raised — to /health's list and to the up-probe's count
# alike — is a condition with none of these among it, in one predicate, so the
# list cannot show what the count leaves out.
ONE_SHOT_KEYS = ("dose:", "dosefail:", "tank:", "proposal:", "meta:")
RAISED_SQL = "cleared_ts IS NULL" + "".join(
    f" AND key NOT LIKE '{family}%'" for family in ONE_SHOT_KEYS
)
LAST_DOSE_KEYS = (
    "id",
    "ml",
    "cap_s",
    "flow_ml",
    "state",
    "source",
    "sent_ts",
    "acked_ts",
    "verdict",
)
DOSE_KEYS = (
    "id",
    "kind",
    "ml",
    "cap_s",
    "flow_ml",
    "state",
    "source",
    "created_ts",
    "sent_ts",
    "acked_ts",
    "verdict",
    "pot",
    "pot_name",
)

POT_COLUMNS = (  # the pots_now view's shape: pot columns plus the open mapping
    "id",
    "name",
    "species",
    "controller",
    "channel",
    "outlet",
    "plant_type",
    "plant_height_cm",
    "pot_diameter_cm",
    "soil",
    "dry_raw",
    "wet_raw",
    "target_low_pct",
    "target_high_pct",
    "dose_ml",
    "mode",
    "cooldown_h",
    "daily_cap_ml",
    "status",
)


def window_edge(con: sqlite3.Connection, pot_id: str, now: int) -> int:
    """Where a pot's open mapping window closes and its next one opens.

    `now`, except that the boundary never moves backwards past what the
    database has already recorded. A window that ends before it began, or
    before a dose it holds, matches nothing: the join wants
    from_ts <= sent_ts <= to_ts, so the pot silently stops owning that dose's
    cooldown and daily cap, and fixing the clock afterwards does not rewrite
    the row.

    The server clock does step backwards — a container that starts before the
    NAS has synced runs hours off until NTP corrects it, and a wiring save on
    either side of that is ordinary. So the floor is the window's own start
    and the newest dose that went down its hose inside it; with the clock
    behaving, all three are `now`.
    """
    row = con.execute(
        "SELECT from_ts, controller, outlet FROM pot_mappings "
        "WHERE pot_id = ? AND to_ts IS NULL",
        (pot_id,),
    ).fetchone()
    if row is None:
        return now
    from_ts, controller, outlet = row
    (dosed,) = con.execute(
        "SELECT COALESCE(MAX(sent_ts), 0) FROM commands "
        "WHERE controller IS ? AND outlet IS ? AND sent_ts >= ?",
        (controller, outlet, from_ts),
    ).fetchone()
    return max(now, from_ts, dosed)


def _hose_since(pot: str, controller: str, outlet: str) -> str:
    """A scalar SQL expression: when this pot's HOSE last changed.

    A dose belongs to a pot and travels with it, but a proposal is an offer to
    open a hose, so it counts only while this pot is still on that hose. The
    open mapping window is the wrong fence: a correction to the sensor CHANNEL
    closes it and opens another without the hose moving, and a pending
    proposal would leave the card while its 'proposed' row went on holding the
    hose slot.

    So: the start of the contiguous run of this pot's windows sharing its
    current (controller, outlet). Windows are contiguous by construction — a
    remap closes the open row and opens the next in the same second — so that
    start is the last time a window of this pot named a DIFFERENT hose, and
    the pot's first window when none ever did. The three arguments are SQL
    expressions, never values off the wire.
    """
    return (
        "COALESCE("
        f"(SELECT MAX(w.to_ts) FROM pot_mappings w WHERE w.pot_id = {pot} "
        f"AND (w.controller IS NOT {controller} OR w.outlet IS NOT {outlet})), "
        f"(SELECT MIN(w.from_ts) FROM pot_mappings w WHERE w.pot_id = {pot}), "
        "0)"
    )
