"""The watering ladder: every gate, in the order they refuse in.

Stateless, and run inside the report's own transaction on the report's own
numbers, so a dose the rules queue rides out on the very response whose
safety fields it was judged on. Every gate errs dry, and a pot skipped for
any reason is retried on the next report for free.

The order is load-bearing in three places, each marked where it happens: the
proposal expiry runs before the open-command check that would otherwise see
the stale proposal and before the quiet gate that would return past it; the
float gate produces the refusal count the two cooldown queries read; and the
daily cap's default is a number of doses, so it needs the candidate's own.
"""

import sqlite3
import time

from . import constants, pots, tank, wire


def water_rules(
    con: sqlite3.Connection,
    r: wire.Report,
    now: int,
    interval: int,
    quiet_window: tuple[int, int],
) -> None:
    """The watering ladder, statelessly, inside the report's own
    transaction. The median over the last RULES_WINDOW readings is both
    the smoothing and the consecutive-dry test: a dry median of five means
    most of the window was dry. Every gate errs dry, and a skipped pot is
    retried on the next report for free.
    """
    # First, and before every gate that can return: the open-command check
    # below counts a stale proposal as the plumbing being busy, and the quiet
    # gate returns without ever reaching it. Sweeping it here is what stops a
    # board that only reports at night from carrying one proposal for ever.
    con.execute(
        "UPDATE commands SET state = 'expired' "
        "WHERE controller = ? AND state = 'proposed' AND created_ts < ?",
        (r.controller, now - constants.PROPOSAL_TTL_S),
    )
    if tank.is_retired(con, r.controller):
        return  # a retired board keeps its readings and never waters
    if tank.latch_of(con, r.controller) is not None:
        return  # the durable half of the board's latch: dry until a human resumes
    origin = tank.counter_origin(con, r.controller)
    if isinstance(tank.tank_state(con, r.controller, origin), tuple) or tank.over_stands(
        con, r.controller
    ):
        return  # over: past the tank with the float at full, or paged so until a tap
    if r.pos != "ok":
        return  # no known position, no report field: dry
    answering = tank.tap_answers_flap(
        con, r.controller, r.float_ok, r.channels.get(constants.FLAP_CHANNEL)
    )
    if r.float_ok != 1 and answering is None:
        return  # no reservoir, no report field: dry — unless a tap answers the flap
    # While the tap answers the flap, a refusal acked before it — the
    # board's float check failing with the word still up, the thing the
    # flap counts — is not water to the cooldown below: the try the tap
    # bought must not wait out the refusal's six hours, when the person
    # told to refill and tap has just done both. Any other time a refusal
    # cools the pot as a dose does, or a pot the board refuses for ever
    # would be asked at report pace.
    refusals_before = 0 if answering is None else answering
    # This board's own beat, so "recent" below means the same number of
    # reports whether it speaks every minute or every hour.
    beat = con.execute(
        "SELECT next_s FROM controllers WHERE controller = ?", (r.controller,)
    ).fetchone()
    cadence = (beat and beat[0]) or interval
    if wire.in_quiet(time.localtime(now).tm_hour, *quiet_window):
        return
    candidates = con.execute(
        "SELECT id, channel, outlet, dry_raw, wet_raw, target_low_pct, "
        "dose_ml, mode, cooldown_h, daily_cap_ml FROM pots_now "
        f"WHERE {pots.RULES_POT_SQL} AND mode IN ('learning', 'auto') "
        "ORDER BY name",
        (r.controller,),
    ).fetchall()
    for (
        pot_id,
        channel,
        outlet,
        dry,
        wet,
        low,
        dose,
        mode,
        cool_h,
        cap_ml,
    ) in candidates:
        if channel not in r.channels:
            # A sensor that went silent errs dry, exactly like a missing
            # float=: without it the window would freeze on stale values
            # and water the pot at cooldown pace forever.
            continue
        # THIS pot's readings, not this channel's. A socket that has
        # just changed hands still holds the last plant's rows, and four
        # of a dead plant's drought readings under one fresh one make a
        # median that opens a valve on a pot nobody has measured.
        #
        # And only recent ones, which the channel key gave for free and
        # the pot key does not: a pot rewired after a month would
        # otherwise decide on four month-old rows plus today's. Fewer
        # than RULES_WINDOW inside the window means it waits — dry.
        fresh = now - constants.RULES_WINDOW * 3 * cadence
        window = [
            raw
            for (raw,) in con.execute(
                "SELECT raw FROM readings "
                "WHERE pot_id = ? AND ts >= ? "
                "ORDER BY ts DESC, rowid DESC LIMIT ?",
                (pot_id, fresh, constants.RULES_WINDOW),
            )
        ]
        if len(window) < constants.RULES_WINDOW:
            continue
        window.sort()  # median of raw == median of pct: the map is monotonic
        median_pct = pots.moisture_pct(window[constants.RULES_WINDOW // 2], dry, wet)
        if median_pct is None or median_pct >= low:
            continue
        # Keyed on the hose, and rightly so: this one asks whether the
        # plumbing is busy, not what this pot has had. The two gates
        # below ask about the pot, through its mapping windows — and
        # then about the hose anyway, as the floor no attribution
        # failure can dig under.
        open_cmd = con.execute(
            "SELECT 1 FROM commands WHERE controller = ? AND outlet = ? "
            "AND state IN ('proposed', 'queued', 'sent') LIMIT 1",
            (r.controller, outlet),
        ).fetchone()
        if open_cmd:
            continue
        # Cooldown counts from the last command the board ever HELD
        # (sent_ts set): an expired-unacked command may still have
        # watered, so it cools the pot just like an acked one. It
        # follows the pot when its hose moves — the six hours belong to
        # the plant, and a remap that reset them would water it twice.
        hours = cool_h if cool_h is not None else constants.DEFAULT_COOLDOWN_H
        cooldown_s = hours * 3600
        watered = con.execute(
            "SELECT 1 FROM commands WHERE pot_id = ? AND sent_ts IS NOT NULL "
            "AND COALESCE(acked_ts, sent_ts) > ? "
            "AND (flow_ml IS NOT 0 OR acked_ts >= ?) LIMIT 1",
            (pot_id, now - cooldown_s, refusals_before),
        ).fetchone() or con.execute(
            # ...and the hose underneath it. Attribution is a lookup, and
            # a lookup comes back empty for reasons that say nothing about
            # the plant: a dose handed before the pot was registered, a
            # clock that stepped while the wiring was saved. Water went
            # down this hose either way, so the hose-keyed gate is the
            # floor: an unknown state waters LESS, never more.
            "SELECT 1 FROM commands WHERE controller = ? AND outlet = ? "
            "AND sent_ts IS NOT NULL AND COALESCE(acked_ts, sent_ts) > ? "
            "AND (flow_ml IS NOT 0 OR acked_ts >= ?) LIMIT 1",
            (r.controller, outlet, now - cooldown_s, refusals_before),
        ).fetchone()
        if watered:
            continue
        cap = cap_ml if cap_ml is not None else constants.DEFAULT_DAILY_CAP_DOSES * dose
        # Acked water only. A handed command the board never acked is far
        # likelier a response that never arrived than a lost ack — the
        # firmware never retries once any response bytes came back — and
        # charging its full dose would starve the pot for the day on
        # nothing. The cooldown above still counts it: spacing errs dry,
        # the cap counts water. One row, one owner, one SUM: the stamp
        # means no dose can fall inside two mapping windows at once.
        (spent,) = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN acked_ts IS NOT NULL "
            "THEN COALESCE(flow_ml, ml) ELSE 0 END), 0) FROM commands "
            "WHERE pot_id = ? AND sent_ts > ?",
            (pot_id, now - 86400),
        ).fetchone()
        # The same floor as the cooldown's, for the same reason: what
        # this HOSE poured in the last day, whoever it was attributed
        # to. MAX rather than a sum, because an attributed dose is
        # counted by both queries and must be spent once.
        (hose_spent,) = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN acked_ts IS NOT NULL "
            "THEN COALESCE(flow_ml, ml) ELSE 0 END), 0) FROM commands "
            "WHERE controller = ? AND outlet = ? AND sent_ts > ?",
            (r.controller, outlet, now - 86400),
        ).fetchone()
        spent = max(spent, hose_spent)
        if spent + dose > cap:
            continue
        state = "proposed"
        if mode == "auto":
            slot_busy = con.execute(
                "SELECT 1 FROM commands WHERE controller = ? "
                "AND state IN ('queued', 'sent') LIMIT 1",
                (r.controller,),
            ).fetchone()
            if slot_busy:
                continue  # the next report retries; dry beats flooded
            state = "queued"
        cap_s = wire.cap_for(dose)
        con.execute(
            "INSERT INTO commands (created_ts, controller, kind, outlet, "
            "ml, cap_s, state, source, pot_id) "
            "VALUES (?, ?, 'water', ?, ?, ?, ?, 'rules', ?)",
            (now, r.controller, outlet, dose, cap_s, state, pot_id),
        )
