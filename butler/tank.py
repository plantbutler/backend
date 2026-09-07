"""The tank: what the float says, what the meter counted, and the latches.

The size is measured rather than configured — the millilitres between a
refill tap and the float going empty — and the float is judged against that
volume and never against a clock. Every question here errs dry.
"""

import sqlite3

from . import constants, notify, pots, wire


class Retired(Exception):
    """POST /command's refusal of water for a retired board."""

    def __init__(self, controller: int):
        super().__init__(f"board {controller} is retired: un-retire it first")


def is_retired(con: sqlite3.Connection, controller: int) -> bool:
    row = con.execute(
        "SELECT retired FROM controllers WHERE controller = ?", (controller,)
    ).fetchone()
    return bool(row and row[0])


class Latched(Exception):
    """POST /command's refusal while a board's durable latch stands."""

    def __init__(self, controller: int, since: int, reason: str):
        super().__init__(
            f"board {controller} stopped watering ({reason} since {notify.hhmm(since)}): "
            + latch_steps(reason)
        )


def latch_steps(reason: str) -> str:
    """The steps a person takes, in order: look at the tank, type the board's
    own word for the reason (the contra words for one it does not know), then
    /resume. The one place they are spelt out."""
    step = constants.LATCH_STEP.get(reason, constants.LATCH_STEP["contra"])
    return f"check the tank, {step}, then resume"


def latch_reason(r: wire.Report, prev_err: str | None) -> str | None:
    """What in this report latches the backend, and under what name;
    `prev_err` is the stored status.err from before this report.

    The board's two durable latches are levels, on every report while they
    stand: a repeat is not a new fault (the caller keeps the stamp), and a
    standing latch is renamed only when the level named goes while the other
    stays. The reset is an edge instead, because err= is sticky and the
    console never clears it — as a level, `contra` would re-latch a resumed
    board for ever. That edge is consulted on every report, level or no
    level: `dry off` typed before the first post-reset report leaves it
    saying ch211=0 err=resetmid, and read as "not dry" the reset would hide
    for good. It is seen this once, the upsert storing resetmid either way.

    Precedence contra, dry, resetmid: the contra's step comes first, and the
    reset behind a dry level is told once `clear contra` is typed. The float
    going empty is deliberately not here — the rules already refuse on
    float=0, and a refill is the human event for it.
    """
    if r.channels.get(constants.CONTRA_CHANNEL) == 1:
        return "contra"
    if r.channels.get(constants.DRY_CHANNEL) == 1:
        return "dry"
    if r.err == "resetmid" and prev_err != "resetmid":
        return "resetmid"
    return None


def latch_of(con: sqlite3.Connection, controller: int) -> tuple[int, str] | None:
    row = con.execute(
        "SELECT latched_ts, latch_reason FROM status WHERE controller = ?",
        (controller,),
    ).fetchone()
    return (row[0], row[1]) if row and row[0] is not None else None


def latest_refill(
    con: sqlite3.Connection, controller: int
) -> tuple[int, int | None] | None:
    """The latest tap, (ts, what the float said at it), or None if never tapped.

    A tap means "full to the top". The stuck-at-empty rule reads this one,
    snapshot and all; the counter starts at counter_origin's answer instead,
    which is not always a tap.
    """
    row = con.execute(
        "SELECT ts, float_ok FROM refills WHERE controller = ? "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (controller,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def base_tap(
    con: sqlite3.Connection, controller: int, fell: int | None = None
) -> tuple[int, int, int | None] | None:
    """The latest tap that saw the float, (ts, rowid, drop_ts), or None: the
    base the counter starts from.

    With `fell` — the second the word went 1 -> 0 — the latest such tap the
    fall came after, or in whose second it came with the tap having seen full:
    the row that drop stamps and the run it closes. Not simply the latest,
    because the drop is confirmed a report after the word fell: a person who
    filled and tapped between the two sightings made a tap the run did not
    start from, so the sample is the earlier tap's and the new tap keeps NULL.
    A tap whose snapshot is NULL never meant "full to the top" and is no base
    for anything — a month of untapped top-ups behind it would become a 12 L
    sample and a threshold no stuck float ever reaches.
    """
    row = con.execute(
        "SELECT ts, rowid, drop_ts FROM refills "
        "WHERE controller = ? AND float_ok IS NOT NULL "
        "AND (? IS NULL OR ts < ? OR (ts = ? AND float_ok = 1)) "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (controller, fell, fell, fell),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def counter_origin(
    con: sqlite3.Connection, controller: int
) -> tuple[int, str] | None:
    """Where this board's counter starts, (ts, "tap" | "rise"), or None.

    The latest tap that saw the float, or the float's latest rise once the
    word has gone 1 -> 0 since that tap and risen strictly later. A float
    that went 1 -> 0 -> 1 since the tap is a tank that ran down and was
    refilled by someone who forgot to tap: it demonstrably moved, so the
    counter restarts at the rise rather than calling it stuck twenty
    millilitres later. A rise with no drop since the tap is the tap's own
    water reaching the float, and the tap stands, or the ordinary "empty,
    fill, tap" run would lose its sample. Sticky: a second drain keeps the
    rise rather than falling back to the tap and resurrecting water already
    sampled. A drop and a rise in one second are a float bouncing, and the
    second is the tap's.

    Both edges are the firm word's, so one report's glitch to 0 neither drops
    nor, on its recovery, rises; and a rise out of a forced 0 is `clear
    contra` typed rather than a refill, is never stamped, and so is never the
    origin. The word's own clocks, not float_ok's: a report that omits float=
    blanks that column, and a float that said nothing once neither rose nor
    fell. With no tap the counter is 0 and nothing arms.
    """
    tap = base_tap(con, controller)
    if tap is None:
        return None
    ts, _rowid, drop_ts = tap
    row = con.execute(
        "SELECT float_rise FROM status WHERE controller = ?", (controller,)
    ).fetchone()
    rise = row[0] if row else None
    if drop_ts is not None and rise is not None and rise > drop_ts:
        return (rise, "rise")
    return (ts, "tap")


def over_stands(con: sqlite3.Connection, controller: int) -> bool:
    """Whether over:<c> is raised and not cleared.

    The page is the fact until a tap answers it: the rules stay dry on this
    rather than on the live predicate, which a float bouncing 0 -> 1 with
    nobody tapping lets go of — that is a rise, a fresh origin and a counter
    at 0, where the page was raised on a float presumed stuck at full. The
    tap clears the row in its own transaction, as /resume lifts the latch.
    """
    return (
        con.execute(
            "SELECT 1 FROM alerts WHERE key = ? AND cleared_ts IS NULL",
            (f"over:{controller}",),
        ).fetchone()
        is not None
    )


def pumped_since(con: sqlite3.Connection, controller: int, since_ts: int) -> int:
    """Millilitres of acked water handed to this board at or after `since_ts`.

    The meter's count, or the dose when the ack carried none — the daily
    cap's own expression, so the counter and the cap agree. At, not after: a
    dose pumps after it is handed and a tank is filled before its tap, so one
    handed in the origin's own second left the full tank, and the report that
    raises the float hands its queued command with that same `now`. A dose
    lost without an ack pumped something uncounted and whatever reads this
    fires late for it; the contra latch stands behind that.
    """
    (total,) = con.execute(
        "SELECT COALESCE(SUM(COALESCE(flow_ml, ml)), 0) FROM commands "
        "WHERE controller = ? AND kind = 'water' AND acked_ts IS NOT NULL "
        "AND sent_ts >= ?",
        (controller, since_ts),
    ).fetchone()
    return total


def tank_median(samples: list[int]) -> int | None:
    """The size in ml a run of samples says, None under TANK_SAMPLES_TO_ARM.

    `samples` is oldest first, as tank_history hands them, and the median is
    of the newest TANK_MEDIAN_OF. A median, not a mean, so one tap that was
    not a fill moves the number little.
    """
    newest = sorted(samples[-constants.TANK_MEDIAN_OF:])
    if len(newest) < constants.TANK_SAMPLES_TO_ARM:
        return None
    mid = len(newest) // 2
    return newest[mid] if len(newest) % 2 else (newest[mid - 1] + newest[mid]) // 2


def tank_history(
    con: sqlite3.Connection,
    controller: int,
    upto: tuple[int, int] | None = None,
    n: int = constants.TANK_MEDIAN_OF,
) -> list[int]:
    """The last `n` of this board's samples in ml, oldest first; `upto`, a
    sample's (ts, rowid), ends the run at that one included — what the tank
    knew as it closed. Bounded in SQLite, walking the index back from the
    newest rather than in Python after the fetch: every report, tick and
    /health reads this, and a board's life of samples must not be their cost.
    """
    ts, rowid = upto if upto is not None else (None, None)
    rows = con.execute(
        "SELECT ml FROM tank_samples WHERE controller = ? "
        "AND (? IS NULL OR ts < ? OR (ts = ? AND rowid <= ?)) "
        "ORDER BY ts DESC, rowid DESC LIMIT ?",
        (controller, ts, ts, ts, rowid, n),
    ).fetchall()
    return [ml for (ml,) in reversed(rows)]


def tank_ml(con: sqlite3.Connection, controller: int) -> int | None:
    """The tank's size in ml as it stands, or None until it is measured."""
    return tank_median(tank_history(con, controller))


def unannounced_samples(
    con: sqlite3.Connection, controller: int
) -> list[tuple[int, int, int, int]]:
    """This board's samples with no tank:<c>:<refill_ts> page yet, oldest
    first, as (ts, rowid, refill_ts, ml).

    The tick announces them in that order and stops at its first failed send,
    so the announced ones are always the oldest: walking the index back to the
    first announced one and then forward costs the pending few plus one, where
    every tick reads this and a board's life of samples must not be its cost.
    """
    last = con.execute(
        "SELECT ts, rowid FROM tank_samples WHERE controller = ? "
        "AND EXISTS (SELECT 1 FROM alerts WHERE key = "
        "'tank:' || tank_samples.controller || ':' || tank_samples.refill_ts) "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (controller,),
    ).fetchone()
    ts, rowid = last if last is not None else (None, None)
    # A bare `ts >= ?`, which the index bounds; the rowid tie only sorts
    # within the second. A `? IS NULL OR` would make SQLite walk the board's
    # whole run and filter.
    return con.execute(
        "SELECT ts, rowid, refill_ts, ml FROM tank_samples WHERE controller = ? "
        "AND ts >= COALESCE(?, 0) AND (? IS NULL OR ts > ? OR rowid > ?) "
        "ORDER BY ts, rowid",
        (controller, ts, ts, ts, rowid),
    ).fetchall()


def is_over(
    tank: int | None, pumped: int, word: int | None, firm: int | None
) -> bool:
    """More than the tank holds plus TANK_TOLERANCE_PCT pumped since the
    origin, while the float still says full in BOTH its words.

    The firm word is the one the origin waits for: between a 0 -> 1 sighting
    and its confirmation the raw word says full while the origin is still the
    tap whose run just closed. The raw word is the one the tank's end arrives
    in: at the first sighting of empty the firm word still says full over the
    run that just drained. On either alone, any run a tenth over the median
    was "presumed stuck" for one beat — and the page stands until a tap, so
    one beat is enough. A float stuck at full says full in both, always, and
    nothing is over while the size is unknown. The one expression of it, so
    the ticker, the rules and /health cannot disagree.
    """
    return (
        tank is not None
        and pumped > tank * (100 + constants.TANK_TOLERANCE_PCT) // 100
        and word == 1
        and firm == 1
    )


def tank_state(
    con: sqlite3.Connection,
    controller: int,
    origin: tuple[int, str] | None,
) -> str | tuple[str, int, int, int]:
    """The float judged against the tank's size: "unknown" while the size is
    or there is no origin, ("over", pumped, tank, origin_ts) for a float
    presumed stuck at full — the dangerous way — and "ok" otherwise, since a
    float that firmly reads empty is a float that works and the rules refuse
    on the raw word already. `origin` is counter_origin's, read by the caller.
    """
    if origin is None:
        return "unknown"
    tank = tank_ml(con, controller)
    if tank is None:
        return "unknown"
    pumped = pumped_since(con, controller, origin[0])
    row = con.execute(
        "SELECT float_ok, float_firm FROM status WHERE controller = ?",
        (controller,),
    ).fetchone()
    if is_over(tank, pumped, row[0] if row else None, row[1] if row else None):
        return ("over", pumped, tank, origin[0])
    return "ok"


def flap_tap(
    con: sqlite3.Connection, controller: int, float_ok: int | None, flap: int | None
) -> int | None:
    """The tap made after the board's flap tripped, or None.

    All three: the flap stands in the report at hand, the board's word is the
    0 the flap forces (a report that omits float= says nothing a tap can
    answer), and the latest tap that saw the float is later than flap_since.
    The tie goes dry — the tap and the report that tripped the flap are
    stamped from one clock, so within a second the order is unknowable and a
    tap made before the flap reached the wire answers nothing, which costs the
    human one more tap a minute on.
    """
    if flap != 1 or float_ok != 0:
        return None
    row = con.execute(
        "SELECT flap_since FROM status WHERE controller = ?", (controller,)
    ).fetchone()
    if not row or row[0] is None:
        return None
    tap = base_tap(con, controller)
    if tap is None or tap[0] <= row[0]:
        return None
    return tap[0]


def tap_answers_flap(
    con: sqlite3.Connection, controller: int, float_ok: int | None, flap: int | None
) -> int | None:
    """The tap the rules may water on over the board's word of 0, or None:
    flap_tap's, with no water handed to the board at or after it.

    The tap is the human saying full; the rules queue their next dose as they
    would and the board re-checks its float at dose time. Granted, the flap
    resets and float=1 returns; refused, the flap stands and the tap is spent,
    so the rules are dry again until the next one. One try per tap, whoever
    asked for it: the flap does not move on the wire for a refusal — the
    board's counter only grows — so without the last clause a refused try was
    queued again at cooldown pace, which is the loop the flap exists to stop.
    """
    tap = flap_tap(con, controller, float_ok, flap)
    if tap is None:
        return None
    handed = con.execute(
        "SELECT 1 FROM commands WHERE controller = ? AND kind = 'water' "
        "AND sent_ts >= ? LIMIT 1",
        (controller, tap),
    ).fetchone()
    return None if handed else tap


def flap_try_pending(
    con: sqlite3.Connection, controller: int, float_ok: int | None, flap: int | None
) -> bool:
    """Whether the try a tap bought is still to come.

    The tap answers the flap, and the try is either queued or with the board
    (handed since the tap, neither acked nor expired) or the rules can still
    make it: a live auto pot on the board with what the ladder needs, watered
    when it next dries. The stale page waits on this, since it would tell the
    person to refill and tap — which they just did — and promise a dose that
    is on its way. Not on a try nobody will make: "nothing handed since the
    tap" is also a board with no such pot, where the try is a human's, so the
    page comes at PERSIST_S instead of waiting for one it cannot promise.
    """
    tap = flap_tap(con, controller, float_ok, flap)
    if tap is None:
        return False
    ended = con.execute(
        "SELECT 1 FROM commands WHERE controller = ? AND kind = 'water' "
        "AND sent_ts >= ? AND state != 'sent' LIMIT 1",
        (controller, tap),
    ).fetchone()
    if ended:
        return False
    coming = con.execute(
        "SELECT 1 FROM commands WHERE controller = ? AND kind = 'water' "
        "AND (state = 'queued' OR (state = 'sent' AND sent_ts >= ?)) LIMIT 1",
        (controller, tap),
    ).fetchone()
    if coming:
        return True
    can_try = con.execute(
        f"SELECT 1 FROM pots_now WHERE {pots.RULES_POT_SQL} AND mode = 'auto' LIMIT 1",
        (controller,),
    ).fetchone()
    return can_try is not None


def float_dead(
    con: sqlite3.Connection,
    controller: int,
    tapped: tuple[int, int | None] | None,
) -> int | None:
    """The tap a float is presumed stuck at empty since, or None.

    `tapped` is latest_refill's answer, read by the caller: the tap saw the
    float saying empty, the board has said float= at least PERSIST_S after it,
    and it still says empty since before the tap. A reading, not the wall
    clock — a board on a five-minute beat or behind a WiFi drop has said
    nothing yet and is judged on nothing. The last clause keeps out a float
    that rose after the tap and days later fell again for real, and reads
    float_word_since rather than float_since because a report that omits
    float= moves that one and a float that said nothing has not moved.
    Harmless unlike its twin: the rules are dry on empty already, so this is
    a page and nothing else.
    """
    if tapped is None or tapped[1] != 0:
        return None
    row = con.execute(
        "SELECT float_ok, float_word_since, float_seen FROM status "
        "WHERE controller = ?",
        (controller,),
    ).fetchone()
    if (
        row
        and row[2] is not None
        and row[2] >= tapped[0] + constants.PERSIST_S
        and row[0] == 0
        and row[1] is not None
        and row[1] <= tapped[0]
    ):
        return tapped[0]
    return None
