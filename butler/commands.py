"""One report in, at most one command out, and the knobs beside them.

The report path is the only place two writers could collide, so it is one
BEGIN IMMEDIATE from the heartbeat to the hand-off: a command is handed to
exactly one board exactly once, and a dose the rules queue rides out on the
very response whose safety fields it was judged on.

Everything here errs dry. An expired command is gone rather than re-handed —
re-handing a dose the board may still be executing is how a plant drowns —
and a report the firmware retried is recognised and not stored twice.

`now` is passed in rather than read here: the facade owns the clock, which is
what lets a test step it backwards over one write and nobody else's.
"""

import sqlite3
from pathlib import Path

from . import constants, rules, store, tank, wire


def handle_report(
    db: Path,
    r: wire.Report,
    now: int,
    interval: int,
    cmd_ttl: int,
    quiet_window: tuple[int, int],
) -> tuple[int, tuple | None]:
    """One report, one transaction: heartbeat, ack, expiries, dedup, the
    readings, the watering rules, and at most one command handed out —
    atomically, so two writers cannot hand the same command twice. A
    command the rules queue here rides out on this very response: the
    safety fields it was judged on are from this same report."""
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")  # writers serialize up front
        con.execute(
            "INSERT INTO controllers (controller, last_seen) VALUES (?, ?) "
            "ON CONFLICT(controller) DO UPDATE SET last_seen = excluded.last_seen",
            (r.controller, now),
        )
        # The firmware retries a report with the body kept when the
        # response is lost, so an identical (controller, t) inside the
        # window is one report heard twice, not two — and t is board
        # uptime, which recurs after every reboot, so the window is what
        # keeps a permanent uniqueness rule from dropping real readings.
        # Judged here, before the status upsert and the float's edge and
        # not only before the readings: a one-glitch sighting delivered
        # twice would otherwise agree with itself into the firm word. The
        # heartbeat above still counts and the hand-off below still runs,
        # since the board never saw the response the retry stands in for.
        duplicate = (
            r.t is not None
            and con.execute(
                "SELECT 1 FROM readings "
                "WHERE controller = ? AND t = ? AND ts >= ? LIMIT 1",
                (r.controller, r.t, now - constants.RETRY_WINDOW_S),
            ).fetchone()
        )
        if not duplicate:
            # The board's error and float before this report: the resetmid
            # latch is an edge on the one and a tank sample on the other,
            # and the upsert below overwrites both. The float is its last
            # word rather than float_ok, because a report that omits
            # float= blanks that column — its vanishing being its own
            # alarm — and must not hide the edge.
            prev = con.execute(
                "SELECT err, float_word, float_firm, float_word_since "
                "FROM status WHERE controller = ?",
                (r.controller,),
            ).fetchone()
            prev_err, prev_word, prev_firm, prev_fell = prev or (None,) * 4
            # The firm word going 1 -> 0: firmly full, the last report
            # that carried float= said empty, and so does this one. One
            # sighting is a glitch by the board's own design — any of its
            # three samples failing fails the word — and a slosh at report
            # time must not close a sample early and hand the origin to
            # its recovery; a firm word of NULL was never firmly full and
            # is no edge. The drop belongs to the tap the word fell after,
            # since a person who filled and tapped between the two
            # sightings made a tap this run did not start from. Not on a
            # report carrying ch207=1: the contra latch is what forces the
            # word to 0 — "float OK, zero pulses" is a fault, not a drop —
            # and the upsert remembers the forced 0 so the word coming
            # back out of it is no rise. Under any other latch the float's
            # word is its own and a drain is an ordinary drop.
            edge = prev_firm == 1 and prev_word == 0 and r.float_ok == 0
            forced = r.channels.get(constants.CONTRA_CHANNEL) == 1
            flap = int(r.channels.get(constants.FLAP_CHANNEL) == 1)
            tap = None
            if edge and not forced:
                tap = tank.base_tap(con, r.controller, prev_fell)
            # The latest safety fields, with enough history for the
            # alert rules: when each value last changed (`since`), when
            # each was last sent at all (`seen` — its vanishing is an
            # alarm), and the last two bad sightings (`bad`, `bad_prev`
            # — a float flapping at the waterline must still page).
            # Plus the board's last error (`err`, left alone by a
            # report without one, and `err_ts`, when it last changed
            # value — the board repeats its last error on every report,
            # so "last seen" would just be the report clock) and the
            # last pos=ok ever seen (`pos_ok_seen`).
            # All SET expressions read the pre-update row, so the
            # ordering below is safe. latched_ts/latch_reason are
            # deliberately not here: the latch is set and cleared on
            # its own, never through this upsert, so no report can
            # overwrite it.
            con.execute(
                "INSERT INTO status (controller, ts, float_ok, float_since, "
                "pos, pos_since, float_seen, pos_seen, float_bad, "
                "float_bad_prev, pos_bad, pos_bad_prev, err, err_ts, pos_ok_seen, "
                "float_word, float_word_since, float_rise, contra, float_firm, "
                "flap, flap_since, dry) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, "
                "NULL, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(controller) DO UPDATE SET ts = excluded.ts, "
                "float_ok = excluded.float_ok, pos = excluded.pos, "
                "float_since = CASE WHEN status.float_ok IS excluded.float_ok "
                "THEN status.float_since ELSE excluded.ts END, "
                "pos_since = CASE WHEN status.pos IS excluded.pos "
                "THEN status.pos_since ELSE excluded.ts END, "
                "float_seen = CASE WHEN excluded.float_ok IS NOT NULL "
                "THEN excluded.ts ELSE status.float_seen END, "
                "pos_seen = CASE WHEN excluded.pos IS NOT NULL "
                "THEN excluded.ts ELSE status.pos_seen END, "
                "float_bad_prev = CASE WHEN excluded.float_ok = 0 "
                "THEN status.float_bad ELSE status.float_bad_prev END, "
                "float_bad = CASE WHEN excluded.float_ok = 0 "
                "THEN excluded.ts ELSE status.float_bad END, "
                "pos_bad_prev = CASE WHEN excluded.pos = 'unknown' "
                "THEN status.pos_bad ELSE status.pos_bad_prev END, "
                "pos_bad = CASE WHEN excluded.pos = 'unknown' "
                "THEN excluded.ts ELSE status.pos_bad END, "
                "err = COALESCE(excluded.err, status.err), "
                "err_ts = CASE WHEN excluded.err IS NOT NULL "
                "AND status.err IS NOT excluded.err "
                "THEN excluded.ts ELSE status.err_ts END, "
                "pos_ok_seen = CASE WHEN excluded.pos = 'ok' "
                "THEN excluded.ts ELSE status.pos_ok_seen END, "
                "float_word = COALESCE(excluded.float_word, status.float_word), "
                # float_word_since is the word's own clock, unlike
                # float_since (float_ok's, which a report omitting float=
                # moves and the fields: rule counts from): it moves on
                # 1 -> 0 and 0 -> 1 and nothing else, so a report that
                # said nothing leaves the float's last move where it was.
                # The firm word is the word once this report and the last
                # that carried float= agree — one sighting is a glitch by
                # the board's own design, and a report that says nothing
                # agrees with nothing (NULL IS 1 is false). It starts
                # NULL, and NULL is never an edge: out of it, becoming 1
                # is no rise and becoming 0 no drop.
                #
                # Its two clocks are the only edges the tank is measured
                # on: the rise here, and the drop the report path stamps
                # on the tap below, each where the word MOVED rather than
                # where it was confirmed — the report that raises the word
                # hands its queued dose with that clock, and the counter
                # must not lose it. A firm drop arriving with ch207=1 is
                # the contra latch forcing the word, remembered as
                # float_forced, so the word coming back out of a forced 0
                # is `clear contra` typed rather than a refill and stamps
                # no rise: stamped, it would become the origin and launder
                # the counter of an untapped refill's run. Every SET reads
                # the row before this update. ch207, ch210 and ch211 are
                # this report's, absent being 0; flap_since is when the
                # flap last tripped, the clock a tap must be later than to
                # answer it, left where it was when the flap lets go.
                "float_word_since = CASE WHEN excluded.float_word IS NULL "
                "OR status.float_word IS excluded.float_word "
                "THEN status.float_word_since ELSE excluded.ts END, "
                "float_firm = CASE WHEN status.float_word IS excluded.float_word "
                "THEN excluded.float_word ELSE status.float_firm END, "
                "float_rise = CASE WHEN excluded.float_word = 1 "
                "AND status.float_word = 1 AND status.float_firm = 0 "
                "AND status.float_forced = 0 "
                "THEN status.float_word_since ELSE status.float_rise END, "
                "float_forced = CASE WHEN excluded.float_word = 0 "
                "AND status.float_word = 0 AND status.float_firm = 1 "
                "THEN excluded.contra ELSE status.float_forced END, "
                "contra = excluded.contra, "
                "flap = excluded.flap, "
                "flap_since = CASE WHEN excluded.flap = 1 AND status.flap = 0 "
                "THEN excluded.ts ELSE status.flap_since END, "
                "dry = excluded.dry",
                (
                    r.controller,
                    now,
                    r.float_ok,
                    now,
                    r.pos,
                    now,
                    now if r.float_ok is not None else None,
                    now if r.pos is not None else None,
                    now if r.float_ok == 0 else None,
                    now if r.pos == "unknown" else None,
                    r.err,
                    now if r.err is not None else None,
                    now if r.pos == "ok" else None,
                    r.float_ok,
                    now if r.float_ok is not None else None,
                    int(forced),
                    flap,
                    now if flap else None,
                    int(r.channels.get(constants.DRY_CHANNEL) == 1),
                ),
            )
            reason = tank.latch_reason(r, prev_err)
            if reason is not None:
                # `since` is set once and never refreshed while the latch
                # stands: it is when the trouble began. The reason is the
                # newest fault's — the one to fix — and a fault the board
                # merely repeats is not a newer one. A dose still waiting
                # would pour into a tank nobody has looked at, so it is
                # expired; a 'sent' one is with the board, whose own latch
                # holds it.
                con.execute(
                    "UPDATE status SET latched_ts = COALESCE(latched_ts, ?), "
                    "latch_reason = ? WHERE controller = ?",
                    (now, reason, r.controller),
                )
                con.execute(
                    "UPDATE commands SET state = 'expired' WHERE controller = ? "
                    "AND kind = 'water' AND state IN ('proposed', 'queued')",
                    (r.controller,),
                )
            if r.ack is not None:
                con.execute(
                    "UPDATE commands SET state = 'acked', acked_ts = ?, flow_ml = ? "
                    "WHERE id = ? AND controller = ? AND state = 'sent'",
                    (now, r.flow_ml, r.ack, r.controller),
                )
            if tap is not None and tap[2] is None:
                # The float went empty for the first time since the tap
                # and this report confirms it: stamped on the tap where
                # the word fell, so a rise from here is a refill nobody
                # said was full and a later drop is a second drain of it.
                # The water the meter counted since the tap is then one
                # measurement of the tank — after the ack step, because
                # the dose that drained it acks on the sighting or on this
                # one. One per tap, and nothing on zero: a tank drained by
                # something the meter never saw is not a measurement. No
                # sample while the board's latch stands, a fault having
                # stood over the run, nor for a retired board; the drop is
                # a fact either way.
                since, rowid, _ = tap
                con.execute(
                    "UPDATE refills SET drop_ts = ? WHERE rowid = ?",
                    (prev_fell, rowid),
                )
                pumped = tank.pumped_since(con, r.controller, since)
                if (
                    pumped > 0
                    and tank.latch_of(con, r.controller) is None
                    and not tank.is_retired(con, r.controller)
                ):
                    con.execute(
                        "INSERT OR IGNORE INTO tank_samples "
                        "(ts, controller, refill_ts, ml) VALUES (?, ?, ?, ?)",
                        (now, r.controller, since, pumped),
                    )
        # A command still 'sent' after the ack step was handed on an
        # earlier response and this report did not ack it, so either that
        # response was lost or the board dropped it — both mean the board
        # does not have it. Expired, never re-handed: re-handing a
        # watering command the board might still execute is how a plant
        # drowns. Whoever wants water asks again.
        con.execute(
            "UPDATE commands SET state = 'expired' "
            "WHERE controller = ? AND state = 'sent'",
            (r.controller,),
        )
        con.execute(
            "UPDATE commands SET state = 'expired' "
            "WHERE controller = ? AND state = 'queued' AND created_ts < ?",
            (r.controller, now - cmd_ttl),
        )
        if not duplicate:
            # Whose reading each channel is, resolved ONCE per report:
            # pot_mappings is not written inside this transaction, so it
            # cannot go stale between two channels of one report. A
            # channel nobody is mapped to stamps NULL — an environment
            # sensor, or a socket no plant has claimed — and that is a
            # real answer, not a gap to fill in later. upsert_pot keeps
            # at most one open window per (controller, channel), which
            # is what makes this dict unambiguous.
            owners = dict(
                con.execute(
                    "SELECT channel, pot_id FROM pot_mappings "
                    "WHERE controller = ? AND to_ts IS NULL "
                    "AND channel IS NOT NULL",
                    (r.controller,),
                )
            )
            con.executemany(
                "INSERT INTO readings (ts, controller, channel, raw, t, pot_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (now, r.controller, ch, raw, r.t, owners.get(ch))
                    for ch, raw in sorted(r.channels.items())
                ],
            )
            rules.water_rules(con, r, now, interval, quiet_window)
        handed = con.execute(
            "SELECT id, kind, outlet, ml, cap_s FROM commands "
            "WHERE controller = ? AND state = 'queued' ORDER BY id LIMIT 1",
            (r.controller,),
        ).fetchone()
        if handed:
            # The stamp is re-read HERE, not kept from when the command
            # was written. The board is handed an outlet, and the water
            # goes to whoever is on that outlet at this moment — which
            # is not always who was on it when the command was made:
            # a manual dose queued before the pot was registered was
            # stamped NULL, and a hose rearranged while a command waited
            # would file the water under the pot that no longer holds it.
            #
            # Both cost more than a wrong name. A dose the pot half of
            # the cooldown cannot see is one the HOSE floor stops covering
            # the moment that pot is rewired, so both watering floors go
            # at once and the plant is watered twice.
            owner = con.execute(
                "SELECT pot_id FROM pot_mappings "
                "WHERE controller = ? AND outlet IS ? AND to_ts IS NULL "
                "ORDER BY rowid LIMIT 1",
                (r.controller, handed[2]),
            ).fetchone()
            con.execute(
                "UPDATE commands SET state = 'sent', sent_ts = ?, pot_id = ? "
                "WHERE id = ?",
                (now, owner and owner[0], handed[0]),
            )
        (override,) = con.execute(
            "SELECT next_s FROM controllers WHERE controller = ?",
            (r.controller,),
        ).fetchone()
        return (override or interval), handed

def enqueue(
    db: Path, c: wire.Command, now: int, cmd_ttl: int
) -> tuple[int, tuple | None]:
    """Fill the slot or report who holds it. The TTL backstop runs here
    too, so a dead board's abandoned command cannot wedge the slot."""
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE commands SET state = 'expired' "
            "WHERE controller = ? AND state IN ('queued', 'sent') "
            "AND COALESCE(sent_ts, created_ts) < ?",
            (c.controller, now - cmd_ttl),
        )
        # Water for a retired board is refused here, the only door the
        # rules do not guard. A stop still goes: it is the safe
        # direction, and the board may be mid-dose from before.
        if c.kind == "water" and tank.is_retired(con, c.controller):
            raise tank.Retired(c.controller)
        if c.kind == "water":
            standing = tank.latch_of(con, c.controller)
            if standing is not None:
                raise tank.Latched(c.controller, *standing)
        busy = con.execute(
            "SELECT id, state FROM commands "
            "WHERE controller = ? AND state IN ('queued', 'sent') LIMIT 1",
            (c.controller,),
        ).fetchone()
        if busy:
            return 0, busy
        # Whom this dose is for. A manual command names a hose, not a
        # pot, so the pot is whoever is on that hose right now, decided
        # once here. A stop has no outlet and stamps NULL, and so does a
        # hose nobody is on.
        owner = (
            con.execute(
                "SELECT pot_id FROM pot_mappings "
                "WHERE controller = ? AND outlet = ? AND to_ts IS NULL "
                "ORDER BY rowid LIMIT 1",
                (c.controller, c.outlet),
            ).fetchone()
            if c.outlet is not None
            else None
        )
        (cmd_id,) = con.execute(
            "INSERT INTO commands (created_ts, controller, kind, outlet, "
            "ml, cap_s, state, source, pot_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', 'manual', ?) RETURNING id",
            (now, c.controller, c.kind, c.outlet, c.ml, c.cap_s,
             owner and owner[0]),
        ).fetchone()
        return cmd_id, None

def set_interval(db: Path, controller: str, value: int, interval: int) -> int:
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO controllers (controller, last_seen, next_s) "
            "VALUES (?, 0, ?) "
            "ON CONFLICT(controller) DO UPDATE SET next_s = excluded.next_s",
            (controller, value or None),
        )
    return value or interval

def set_retired(db: Path, controller: int, flag: int, now: int) -> None:
    """A retired board is a quiet one, not a rejected one: its reports
    still land, but nothing pages for it and nothing waters from it.
    Retiring drops the water still waiting for the board — a proposal
    or a queued dose would otherwise be handed out on its next report,
    so it goes the way burial sends one; a 'sent' one is with the board
    and expires on that report. It also clears whatever page stands for
    the board — silence, a sensor, the float, the position, a field that
    vanished, the latch, a float stuck either way: every rule skips the
    board from now on, so nobody else would ever clear them. The latch
    row itself stays: nobody checked that tank, and the board comes back
    with it."""
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO controllers (controller, last_seen, retired) "
            "VALUES (?, 0, ?) "
            "ON CONFLICT(controller) DO UPDATE SET retired = excluded.retired",
            (controller, flag),
        )
        if flag:
            con.execute(
                "UPDATE commands SET state = 'expired' WHERE controller = ? "
                "AND kind = 'water' AND state IN ('proposed', 'queued')",
                (controller,),
            )
            con.execute(
                "UPDATE alerts SET cleared_ts = ? WHERE cleared_ts IS NULL "
                "AND (key IN (?, ?, ?, ?, ?, ?, ?, ?) OR key LIKE ?)",
                (
                    now,
                    f"silent:{controller}",
                    f"float:{controller}",
                    f"pos:{controller}",
                    f"fields:float:{controller}",
                    f"fields:pos:{controller}",
                    f"latch:{controller}",
                    f"over:{controller}",
                    f"stale:{controller}",
                    f"sensor:{controller}:%",
                ),
            )

def resume(db: Path, controller: int, now: int) -> None:
    """The human's half: the tank was checked. Clears the row and the
    page together, so /health and the phone agree the moment it answers;
    idempotent on a board that was not latched."""
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE status SET latched_ts = NULL, latch_reason = NULL "
            "WHERE controller = ?",
            (controller,),
        )
        con.execute(
            "UPDATE alerts SET cleared_ts = ? WHERE key = ? AND cleared_ts IS NULL",
            (now, f"latch:{controller}"),
        )

def record_refill(db: Path, controller: int, now: int) -> int:
    """A human refilled this board's tank. Returns the tap's timestamp.

    The tap means "full to the top", and the board's last real word on the
    float goes on the row: float_word rather than float_ok, which one
    report omitting float= blanks — a tap made under a row reading
    "float ?" must not be a tap that counts for nothing. NULL only for a
    board that has never sent float=, and then the tap judges nothing.
    Read under the write lock, so no report can slip between the look and
    the insert. The tap also answers over:<c>, cleared here in the tap's
    own transaction as /resume clears latch:<c>: the person did the thing,
    so the rules, /health and the phone let go the moment it lands.
    """
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT float_word FROM status WHERE controller = ?", (controller,)
        ).fetchone()
        con.execute(
            "INSERT INTO refills (ts, controller, float_ok) VALUES (?, ?, ?)",
            (now, controller, row[0] if row else None),
        )
        con.execute(
            "UPDATE alerts SET cleared_ts = ? WHERE key = ? AND cleared_ts IS NULL",
            (now, f"over:{controller}"),
        )
    return now

def approve(db: Path, cmd_id: int, now: int, cmd_ttl: int) -> tuple | None:
    """proposed -> queued, slot permitting; returns the blocker if busy.

    created_ts restarts on approval: the queued-TTL clock should time
    the wait for the board, not the hours the proposal sat waiting for
    a human.
    """
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        # The proposal-TTL sweep normally runs on the controller's own
        # reports; a board gone dark never sweeps, so enforce the TTL
        # here too — a days-old proposal must not water on the stale
        # evidence it was made from.
        con.execute(
            "UPDATE commands SET state = 'expired' "
            "WHERE id = ? AND state = 'proposed' AND created_ts < ?",
            (cmd_id, now - constants.PROPOSAL_TTL_S),
        )
        row = con.execute(
            "SELECT controller FROM commands WHERE id = ? AND state = 'proposed'",
            (cmd_id,),
        ).fetchone()
        if not row:
            # Keep the expiry sweep even though we refuse: the with-block
            # would roll it back along with the raise, and /pots and the
            # database should agree the proposal is gone.
            con.commit()
            raise ValueError(f"no proposed command {cmd_id}")
        # The same TTL backstop enqueue runs: a dead board's abandoned
        # command must not wedge approval behind a 409 forever.
        con.execute(
            "UPDATE commands SET state = 'expired' "
            "WHERE controller = ? AND state IN ('queued', 'sent') "
            "AND COALESCE(sent_ts, created_ts) < ?",
            (row[0], now - cmd_ttl),
        )
        busy = con.execute(
            "SELECT id, state FROM commands WHERE controller = ? "
            "AND state IN ('queued', 'sent') LIMIT 1",
            (row[0],),
        ).fetchone()
        if busy:
            return busy
        con.execute(
            "UPDATE commands SET state = 'queued', created_ts = ? WHERE id = ?",
            (now, cmd_id),
        )
        return None

def record_verdict(db: Path, cmd_id: int, verdict: str, now: int) -> None:
    """One human judgement per executed dose; a re-verdict replaces."""
    with store.connect(db) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT sent_ts FROM commands WHERE id = ?", (cmd_id,)
        ).fetchone()
        if not row or row[0] is None:
            raise ValueError(f"command {cmd_id} was never handed to the board")
        con.execute(
            "INSERT OR REPLACE INTO verdicts (command_id, ts, verdict) "
            "VALUES (?, ?, ?)",
            (cmd_id, now, verdict),
        )
