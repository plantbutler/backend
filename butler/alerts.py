"""Every alert rule, the pass that sends them, and the loop that runs it.

A rule reads database state and nothing else — bare SELECTs on an autocommit
connection, never a write — and answers a list of Alerts, each carrying the
write that remembers it went out. The tick applies that write only after the
message was accepted, so a failed send leaves no row and the next pass tries
again: at-least-once, because a repeated message beats a lost one.

Eight rules, thirteen key families. They are independent except in two places,
both of them real and both marked where they happen: `sensors` reads the
heartbeat `silent` fills in, and two rules — `latches` and the `over:` half of
`float_judged` — raise here but are cleared somewhere else entirely, in
/resume, in retiring a board, and in a refill tap. `latches` is also the one
that pages again while it still stands, when the reason it named changes.
"""

import asyncio
import contextlib
import sqlite3
import sys
import time
from collections.abc import Callable
from typing import NamedTuple

from starlette.concurrency import run_in_threadpool

from . import config, constants, notify, pots, store

# By name, and only here. `float_judged` unpacks a local called `tank` — the
# millilitres the tank measured — which would shadow the module for the whole
# of that function; the rest of the file follows it rather than reading two
# ways. Nothing patches these, so the second binding costs nothing.
from .tank import (
    counter_origin,
    flap_try_pending,
    float_dead,
    latch_steps,
    latest_refill,
    tank_history,
    tank_median,
    tank_state,
    unannounced_samples,
)


def mark(now: int, key: str, detail: str | None = None) -> Callable:
    def write(con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT OR REPLACE INTO alerts "
            "(key, raised_ts, cleared_ts, detail) VALUES (?, ?, NULL, ?)",
            (key, now, detail),
        )

    return write


def clear(now: int, key: str) -> Callable:
    def write(con: sqlite3.Connection) -> None:
        con.execute(
            "UPDATE alerts SET cleared_ts = ? WHERE key = ?", (now, key)
        )

    return write


class Context(NamedTuple):
    """What one pass reads once and every rule then asks.

    `heartbeat` is the one thing a rule writes: `silent` fills it in and
    `sensors` reads it, which is the only value here that travels from one
    rule to another. The tuple is frozen; that dictionary is the blackboard
    the two of them share, and it is empty until `silent` has run.
    """

    standing: dict[str, tuple[int, int | None, str | None]]
    boards: list[tuple[int, int]]
    retired: set[int]
    heartbeat: dict[int, tuple[int, int | None]]
    since: int
    interval: int
    silent_after: int

    def raised(self, key: str) -> bool:
        row = self.standing.get(key)
        return row is not None and row[1] is None

    def floor_ok(self, now: int, key: str) -> bool:
        # A cleared condition may sound again only after the floor: a
        # float bouncing at the waterline is one pair an hour, not a
        # pair per report.
        row = self.standing.get(key)
        return (
            row is None
            or row[1] is None
            or now - row[1] >= constants.REALERT_FLOOR_S
        )


def read_state(
    con: sqlite3.Connection, since: int, interval: int, silent_after: int
) -> Context:
    """The two queries every rule below would otherwise repeat."""
    standing = {
        key: (raised_ts, cleared_ts, detail)
        for key, raised_ts, cleared_ts, detail in con.execute(
            "SELECT key, raised_ts, cleared_ts, detail FROM alerts"
        )
    }
    # A retired board is a quiet one, not a rejected one: its reports
    # land, and every rule below that speaks for a board's own condition
    # leaves it out. Nothing waters from it, so "watering is on hold" has
    # nothing to tell; and the pages that stood when it was retired were
    # cleared by set_retired, since nobody here would ever clear them.
    # Read once, for every rule below: the boards, and which of them
    # are retired.
    boards = con.execute(
        "SELECT controller, retired FROM controllers ORDER BY controller"
    ).fetchall()
    retired = {controller for controller, flag in boards if flag}
    return Context(
        standing=standing,
        boards=boards,
        retired=retired,
        heartbeat={},
        since=since,
        interval=interval,
        silent_after=silent_after,
    )


def median_window_pct(
    con: sqlite3.Connection,
    controller: str,
    channel: int,
    dry: int,
    wet: int,
    where: str,
    params: tuple,
) -> int | None:
    """Median % of the last up-to-RULES_WINDOW readings matching `where`;
    None when no reading does (or the calibration is missing). With a
    full window and `ts <= sent_ts` this is exactly the window the rules
    judged: same ordering, same tie-break."""
    window = [
        raw
        for (raw,) in con.execute(
            "SELECT raw FROM readings "
            f"WHERE controller = ? AND channel = ? AND {where} "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (controller, channel, *params, constants.RULES_WINDOW),
        )
    ]
    if not window:
        return None
    window.sort()
    return pots.moisture_pct(window[len(window) // 2], dry, wet)


def silent(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`silent:<c>`: a board that stopped reporting.

    Fills the heartbeat the sensor rule below reads: the two thresholds
    have to be the same number, and a board this rule leaves out — a
    retired one, one never heard from — is one that rule leaves out too.
    """
    found: list[notify.Alert] = []
    # A controller that stopped reporting. Silence is measured against
    # the butler's own observation window too: after a redeploy or a NAS
    # reboot, last_seen is stale because the BUTLER was away — that is
    # the dead-man's news, not this rule's. A retired board is left out
    # here, so it gets no `heartbeat` entry either, and the sensor rule
    # below skips its pots on its own.
    for controller, last_seen, override in con.execute(
        "SELECT controller, last_seen, next_s FROM controllers "
        "WHERE last_seen > 0 AND retired = 0"
    ):
        ctx.heartbeat[controller] = (last_seen, override)
        threshold = max(ctx.silent_after, 3 * (override or ctx.interval))
        key = f"silent:{controller}"
        if now - max(last_seen, ctx.since) > threshold:
            if not ctx.raised(key) and ctx.floor_ok(now, key):
                found.append(
                    notify.Alert(
                        key,
                        "high",
                        "warning",
                        f"{controller} has been silent for "
                        f"{(now - last_seen) // 60} min "
                        f"(last report {notify.hhmm(last_seen)})",
                        mark(now, key),
                    )
                )
        elif now - last_seen <= threshold and ctx.raised(key):
            found.append(
                notify.Alert(
                    key,
                    "default",
                    "white_check_mark",
                    f"{controller} is reporting again",
                    clear(now, key),
                )
            )
    return found


def sensors(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`sensor:<c>:<ch>`: one channel gone quiet under a board that is fine.

    Reads the heartbeat `silent` filled in, which is the one value one
    rule here computes and another needs.
    """
    found: list[notify.Alert] = []
    # A sensor whose channel stopped arriving while its controller stays
    # healthy: water_rules errs dry and skips the pot forever, which
    # without this rule is a plant quietly dying behind a green
    # dead-man. Same threshold and observation window as the silent
    # rule; a controller that is itself silent already pages there.
    for name, controller, channel in con.execute(
        f"SELECT name, controller, channel FROM pots_now WHERE {pots.live_sql()} "
        "AND controller IS NOT NULL AND channel IS NOT NULL ORDER BY name"
    ):
        pulse = ctx.heartbeat.get(controller)
        if pulse is None:
            continue  # never-heard controller: nothing to compare against
        last_seen, override = pulse
        threshold = max(ctx.silent_after, 3 * (override or ctx.interval))
        if now - last_seen > threshold:
            continue  # the whole controller is silent: that rule pages
        (latest,) = con.execute(
            "SELECT MAX(ts) FROM readings WHERE controller = ? AND channel = ?",
            (controller, channel),
        ).fetchone()
        key = f"sensor:{controller}:{channel}"
        if now - max(latest or 0, ctx.since) > threshold:
            if not ctx.raised(key) and ctx.floor_ok(now, key):
                found.append(
                    notify.Alert(
                        key,
                        "high",
                        "warning",
                        f"the sensor for {name} ({controller} ch{channel}) "
                        "stopped reporting: the rules cannot water it",
                        mark(now, key),
                    )
                )
        elif latest is not None and now - latest <= threshold and ctx.raised(key):
            found.append(
                notify.Alert(
                    key,
                    "default",
                    "white_check_mark",
                    f"the sensor for {name} is back",
                    clear(now, key),
                )
            )
    return found


def safety_fields(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`float:<c>`, `pos:<c>` and the two `fields:` pages: the board's own
    words gone bad twice, and the board's own words gone missing.
    """
    found: list[notify.Alert] = []
    # The board said so itself: reservoir empty, or the manifold no
    # longer knows where it is — both mean the rules refuse to water,
    # silently. Two bad sightings inside FLAP_WINDOW_S raise (a single
    # blip at the waterline is slosh; a flap or a steady bad is an
    # empty tank — a sub-persistence debounce would sleep through the
    # flap); the clear needs the value good with no bad sighting for a
    # full window. NULL never counts as bad, but a field VANISHING
    # after the board had been sending it is its own alarm: absence
    # disables all watering with every tick looking clean.
    for (
        controller,
        _ts,
        float_ok,
        float_since,
        pos,
        pos_since,
        float_seen,
        pos_seen,
        float_bad,
        float_bad_prev,
        pos_bad,
        pos_bad_prev,
        pos_ok_seen,
    ) in con.execute(
        "SELECT controller, ts, float_ok, float_since, pos, pos_since, "
        "float_seen, pos_seen, float_bad, float_bad_prev, pos_bad, "
        "pos_bad_prev, pos_ok_seen FROM status"
    ):
        if controller in ctx.retired:
            continue
        pos_value = {"ok": 1, "unknown": 0}.get(pos)
        # A board that has never said pos=ok is one shipped with the flag
        # that forces pos=unknown; paging on it would raise once, two
        # minutes after first boot, and then stand deaf for the whole
        # bench programme.
        for (
            kind,
            value,
            value_since,
            seen,
            bad,
            bad_prev,
            trouble,
            relief,
            armed,
        ) in (
            (
                "float",
                float_ok,
                float_since,
                float_seen,
                float_bad,
                float_bad_prev,
                (
                    f"the reservoir on {controller} is empty or at the "
                    "waterline: watering is on hold"
                ),
                f"the reservoir on {controller} is full again",
                True,
            ),
            (
                "pos",
                pos_value,
                pos_since,
                pos_seen,
                pos_bad,
                pos_bad_prev,
                (
                    f"{controller} lost track of its manifold position: "
                    "watering is on hold"
                ),
                f"{controller} knows its manifold position again",
                pos_ok_seen is not None,
            ),
        ):
            key = f"{kind}:{controller}"
            flapped = (
                bad is not None
                and bad_prev is not None
                and now - bad <= constants.FLAP_WINDOW_S
                and now - bad_prev <= constants.FLAP_WINDOW_S
            )
            if flapped and armed:
                if not ctx.raised(key) and ctx.floor_ok(now, key):
                    found.append(
                        notify.Alert(key, "high", "warning", trouble, mark(now, key))
                    )
            elif (
                value == 1
                and (bad is None or now - bad > constants.FLAP_WINDOW_S)
                and ctx.raised(key)
            ):
                found.append(
                    notify.Alert(
                        key, "default", "white_check_mark", relief, clear(now, key)
                    )
                )
            vkey = f"fields:{kind}:{controller}"
            if (
                value is None
                and seen is not None
                and now - value_since >= constants.PERSIST_S
            ):
                if not ctx.raised(vkey) and ctx.floor_ok(now, vkey):
                    found.append(
                        notify.Alert(
                            vkey,
                            "high",
                            "warning",
                            f"{controller} stopped sending {kind}=: "
                            "watering is on hold",
                            mark(now, vkey),
                        )
                    )
            elif (
                value is not None
                and now - value_since >= constants.PERSIST_S
                and ctx.raised(vkey)
            ):
                found.append(
                    notify.Alert(
                        vkey,
                        "default",
                        "white_check_mark",
                        f"{controller} sends {kind}= again",
                        clear(now, vkey),
                    )
                )
    return found


def latches(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`latch:<c>`: raised here, cleared in /resume and in retiring a board.

    One of the two rules whose clear lives elsewhere, and the one that
    pages again while it still stands: the row's detail is the reason its
    page named, so a latch that turns from contra to dry says the new
    words rather than leaving a person doing the wrong thing.
    """
    found: list[notify.Alert] = []
    # The durable latch. High, and without the re-alert floor: a board
    # that latches again ten minutes after a human resumed it is the
    # repeat that must not wait an hour to be heard. The row's detail is
    # the reason the page named, and a standing latch whose reason changed
    # pages again with its new words, floor or no floor — a person told
    # "clear contra" must also be told "dry off".
    for controller, latched_ts, reason in con.execute(
        "SELECT controller, latched_ts, latch_reason FROM status "
        "WHERE latched_ts IS NOT NULL"
    ):
        if controller in ctx.retired:
            continue  # the row stands and comes back with the board
        key = f"latch:{controller}"
        if not ctx.raised(key) or ctx.standing[key][2] != reason:
            found.append(
                notify.Alert(
                    key,
                    "high",
                    "warning",
                    f"board {controller} stopped watering: "
                    f"{constants.LATCH_TEXT.get(reason, reason)} — "
                    f"{latch_steps(reason)} "
                    "in the app",
                    mark(now, key, reason),
                )
            )
    return found


def float_judged(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`over:<c>` and `stale:<c>`: the float against the tank's measured size.

    `over:` is the other rule raised here and cleared elsewhere — only a
    refill tap clears it, in `record_refill`, because the word dropping to
    0 is a contra, a flap or an omitted float= as often as an empty tank.
    """
    found: list[notify.Alert] = []
    # The float judged against the tank's size, never a clock.
    #
    # Stuck at full is the dangerous one: past what the tank holds since
    # the origin with the float's firm word still saying full. The rules
    # stay dry on it, and a tap is the only clear — made in its own
    # transaction with no page, the person having done the thing — since
    # the word dropping to 0 is a contra, a flap or an omitted float= as
    # often as an empty tank. Raised here and never cleared here.
    #
    # Stuck at empty is harmless — the rules are dry on empty already, so
    # it is a page and nothing else, cleared when the float says full —
    # but the page must say which of the two it is: the board's own float
    # check tripped, which a tap answers for one dose, or a float presumed
    # stuck. While the try that tap bought is still to come the page would
    # tell the person to do what they just did, so it waits for the
    # refusal; on a board where nobody will make that try it does not
    # wait.
    #
    # Neither is raised while the board's latch stands, nor while its
    # latest report carried ch207=1 or ch211=1 — a /resume lifts the
    # backend's latch and not the board's own. A contra forces the word to
    # 0, and a board held dry is the latch page's business.
    for controller, float_ok, latched_ts, contra, flap, dry in con.execute(
        "SELECT controller, float_ok, latched_ts, contra, flap, dry FROM status"
    ):
        if controller in ctx.retired:
            continue
        quiet = latched_ts is not None or contra or dry
        origin = counter_origin(con, controller)
        tapped = latest_refill(con, controller)
        key = f"over:{controller}"
        if not ctx.raised(key) and not quiet and ctx.floor_ok(now, key):
            state = tank_state(con, controller, origin)
            if isinstance(state, tuple):
                _, pumped, tank, origin_ts = state
                found.append(
                    notify.Alert(
                        key,
                        "high",
                        "warning",
                        f"board {controller} pumped {pumped} ml since "
                        f"{notify.hhmm(origin_ts)}, more than its tank holds "
                        f"({tank} ml), and the float still says full: "
                        "presumed stuck, the rules will not water until the "
                        "next refill",
                        mark(now, key),
                    )
                )
        key = f"stale:{controller}"
        dead = float_dead(con, controller, tapped)
        if dead is not None and flap_try_pending(con, controller, float_ok, flap):
            dead = None  # the tap's try is pending: nothing to ask of anyone yet
        if dead is not None:
            if not quiet and not ctx.raised(key) and ctx.floor_ok(now, key):
                why = (
                    "the board's own float check tripped — refill to the "
                    "top and tap refilled, and the butler will try one dose"
                    if flap
                    else "presumed stuck at empty, look at the magnet"
                )
                found.append(
                    notify.Alert(
                        key,
                        "high",
                        "warning",
                        f"the float on board {controller} still says empty "
                        f"{(now - dead) // 60} min after the refill at "
                        f"{notify.hhmm(dead)}: {why}",
                        mark(now, key),
                    )
                )
        elif float_ok == 1 and ctx.raised(key):
            found.append(
                notify.Alert(
                    key,
                    "default",
                    "white_check_mark",
                    f"the float on board {controller} moved",
                    clear(now, key),
                )
            )
    return found


def tank_runs(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`tank:<c>:<refill_ts>`: one announcement per sample the float closed.
    """
    found: list[notify.Alert] = []
    # Every sample the float closes is announced once, keyed on its tap
    # and marked like a dose judgement: a one-shot, never cleared, so
    # /health and the up-probe leave `tank:` out as they leave `dose:`.
    # Against the size the tank knew before it — the median of the
    # earlier last few, once there are enough of them — a sample off
    # by more than TANK_DRIFT_PCT is a warning rather than news: the
    # tank was swapped, the meter is clogging, or the tap was not a
    # fill. A retired board's waits, like its doses. Board by board,
    # each bounded to its pending few: the samples a board has closed
    # in its life are not a tick's cost.
    for controller, _flag in ctx.boards:
        if controller in ctx.retired:
            continue
        for ts, rowid, refill_ts, ml in unannounced_samples(con, controller):
            key = f"tank:{controller}:{refill_ts}"
            # The sample and the five before it: the size it is judged
            # against is the median of those five, the size it joins the
            # median of the five ending at it.
            history = tank_history(
                    con, controller, (ts, rowid), constants.TANK_MEDIAN_OF + 1
                )
            known = tank_median(history[:-1])
            drift = known is not None and (
                abs(ml - known) > known * constants.TANK_DRIFT_PCT // 100
            )
            if drift:
                found.append(
                    notify.Alert(
                        key,
                        "default",
                        "warning",
                        f"board {controller}'s tank measured {ml} ml this run, "
                        f"not the {known} ml it knew: a different tank, a "
                        "clogging meter, or a tap that was not a fill",
                        mark(now, key),
                    )
                )
                continue
            size = tank_median(history)
            # The count beside the size is the samples it rests on: the
            # median's window, not every run the board has closed in its
            # life, or the sixth run would claim a first that has left
            # the number. Learning, under two, they are the same count.
            behind = min(len(history), constants.TANK_MEDIAN_OF)
            found.append(
                notify.Alert(
                    key,
                    "default",
                    "droplet",
                    f"board {controller} ran its tank down: {ml} ml since the "
                    f"refill at {notify.hhmm(refill_ts)} "
                    + (
                        f"(tank {size} ml over {behind} samples)"
                        if size is not None
                        else f"(tank size learning, {behind} of "
                        f"{constants.TANK_SAMPLES_TO_ARM})"
                    ),
                    mark(now, key),
                )
            )
    return found


def doses(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`dose:<id>`, and `dosefail:<c>` as its once-an-hour floor.

    Every dose the board was handed is judged exactly once. The floor is
    this rule's own state, not the context's: it is spent inside one pass.
    """
    found: list[notify.Alert] = []
    paged_hoses: set[str] = set()
    # Every dose the board was handed gets judged exactly once: never
    # acked (judged immediately — the loss is proven the moment the next
    # report failed to ack), short on the meter, or no moisture rise
    # (both a soak later). A dose that worked is recorded silently —
    # this is "tell me when it's wrong", not a watering feed. The 24 h
    # lookback bounds the first-deploy burst; its cost is that a
    # judgement ntfy could not take for a full day is dropped, and by
    # then the dead-man has been quiet for most of it.
    for (
        cmd_id,
        controller,
        outlet,
        ml,
        flow_ml,
        _,
        sent_ts,
        acked_ts,
        owner,
    ) in con.execute(
        "SELECT id, controller, outlet, ml, flow_ml, state, sent_ts, "
        "acked_ts, pot_id FROM commands "
        "WHERE kind = 'water' AND sent_ts IS NOT NULL "
        "AND state IN ('acked', 'expired') "
        "AND COALESCE(acked_ts, sent_ts) >= ? "
        "AND NOT EXISTS "
        "(SELECT 1 FROM alerts WHERE key = 'dose:' || commands.id)",
        (now - constants.DOSE_LOOKBACK_S,),
    ).fetchall():
        if controller in ctx.retired:
            continue  # judged once the board is back, inside the lookback
        row = con.execute(
            "SELECT next_s FROM controllers WHERE controller = ?", (controller,)
        ).fetchone()
        # Slow reporters get a longer soak, or the after-window would
        # hold no readings at all and every dose would judge on nothing.
        soak = max(constants.SOAK_S, 3 * ((row and row[0]) or ctx.interval))
        judge_at = (acked_ts or sent_ts) + soak
        if acked_ts is not None and now < judge_at:
            continue  # still soaking in; an expiry needs no wait
        key = f"dose:{cmd_id}"
        # Two questions with different answers. WHOSE dose it was is the
        # stamp on the row, decided when the command was written. WHICH
        # SENSOR to judge it on is a window question: the pot may have
        # been rewired between the dose and the soak, and the rise belongs
        # to the probe that was in that soil at the time.
        #
        # No status filter: a dose that happened is a dose worth
        # judging and naming, even if the plant has since been buried.
        pot = (
            con.execute(
                "SELECT name, dry_raw, wet_raw FROM pots WHERE id = ?",
                (owner,),
            ).fetchone()
            if owner
            else None
        )
        channel_row = (
            con.execute(
                "SELECT channel FROM pot_mappings WHERE pot_id = ? "
                "AND ? >= from_ts AND (to_ts IS NULL OR ? < to_ts)",
                (owner, sent_ts, sent_ts),
            ).fetchone()
            if owner
            else None
        )
        channel = channel_row[0] if channel_row else None
        name = pot[0] if pot else f"outlet {outlet}"
        symptoms: list[str] = []
        priority = "default"
        evidence = False  # did anything actually vouch for this dose?
        if acked_ts is None:  # handed out, expired unacknowledged
            symptoms.append("it was handed to the board and never acknowledged")
            priority = "high"
            evidence = True
        elif flow_ml is not None and ml is not None:
            evidence = True
            if 2 * flow_ml < ml:
                symptoms.append(f"the meter counted {flow_ml} of {ml} ml")
                priority = "high"
        if (
            pot is not None
            and acked_ts is not None
            and channel is not None
            and pot[1] is not None
            and pot[2] is not None
        ):
            _, dry, wet = pot
            before = median_window_pct(
                con, controller, channel, dry, wet, "ts <= ?", (sent_ts,)
            )
            after = median_window_pct(
                con,
                controller,
                channel,
                dry,
                wet,
                "ts > ? AND ts <= ?",
                (acked_ts, judge_at),
            )
            # An already-wet pot has no headroom to rise: skip, or every
            # hose-priming test dose would page the phone. The rise-only
            # symptom stays at default priority until the bench rig says
            # what a dose actually does to a sensor.
            if before is not None and after is not None:
                evidence = True
                if (
                    before < 100 - constants.MIN_RISE_PCT
                    and after - before < constants.MIN_RISE_PCT
                ):
                    symptoms.append(f"moisture went {before}% to {after}%")
        if symptoms:
            # Failures correlate: a dead pump takes every pot down at
            # once, and a high page per dose per pot is how a phone ends
            # up muted — which is worse than an alert three minutes late.
            # One page per controller per floor; the rest are judged and
            # recorded silently.
            hose = f"dosefail:{controller}"
            floored = ctx.standing.get(hose)
            if hose in paged_hoses or (
                floored is not None and now - floored[0] < constants.REALERT_FLOOR_S
            ):
                found.append(
                    notify.Alert(key, "min", "droplet", None, mark(now, key, "failed"))
                )
            else:
                paged_hoses.add(hose)

                def record_failure(
                    con: sqlite3.Connection, _key=key, _hose=hose
                ) -> None:
                    mark(now, _key, "failed")(con)
                    mark(now, _hose)(con)

                found.append(
                    notify.Alert(
                        key,
                        priority,
                        "warning,droplet",
                        f"the {ml} ml dose on {name} did not work: "
                        + "; ".join(symptoms),
                        record_failure,
                    )
                )
        else:
            # No symptom, but 'ok' only when something vouched for it: a
            # dose with no meter number and no usable readings is
            # 'unverified', not quietly fine.
            found.append(
                notify.Alert(
                    key,
                    "min",
                    "droplet",
                    None,
                    mark(now, key, "ok" if evidence else "unverified"),
                )
            )
    return found


def proposals(
    con: sqlite3.Connection, now: int, ctx: Context
) -> list[notify.Alert]:
    """`proposal:<c>:<outlet>`: a learning proposal nobody has looked at.
    """
    found: list[notify.Alert] = []
    # A learning proposal nobody is polling /pots for. Keyed on the
    # hose, not the command: proposals expire and respawn with fresh ids
    # every PROPOSAL_TTL_S while the pot stays dry, and one nudge a day
    # is a reminder where one per respawn is a mute button.
    for (
        controller,
        outlet,
        cmd_id,
        ml,
        created_ts,
        name,
        channel,
        dry,
        wet,
        low,
    ) in con.execute(
        # The pot that is on the hose now AND was already on it when the
        # proposal was made: an offer to open a hose is not something
        # the next pot along inherits, and /pots stops showing it too.
        # Since it was on the HOSE, not since its wiring last changed —
        # correcting a sensor channel must not mute the nudge.
        "SELECT c.controller, c.outlet, c.id, c.ml, c.created_ts, p.name, "
        "m.channel, p.dry_raw, p.wet_raw, p.target_low_pct FROM commands c "
        "JOIN pot_mappings m ON m.controller = c.controller "
        "AND m.outlet = c.outlet AND m.to_ts IS NULL "
        f"AND c.created_ts >= {pots._hose_since('m.pot_id', 'm.controller', 'm.outlet')} "
        f"JOIN pots p ON p.id = m.pot_id AND {pots.live_sql('p.status')} "
        "WHERE c.state = 'proposed' AND c.created_ts >= ? ORDER BY c.id",
        (now - constants.PROPOSAL_TTL_S,),
    ):
        key = f"proposal:{controller}:{outlet}"
        row = ctx.standing.get(key)
        if row is not None and now - row[0] < constants.PROPOSAL_NUDGE_S:
            continue
        pct = median_window_pct(
            con, controller, channel, dry, wet, "ts <= ?", (now,)
        )
        found.append(
            notify.Alert(
                key,
                "default",
                "seedling",
                f"{name} looks dry ({pct}%, target {low}%): proposal "
                f"{cmd_id} for {ml} ml waits until "
                f"{notify.hhmm(created_ts + constants.PROPOSAL_TTL_S)} "
                "- approve it from /pots",
                mark(now, key),
            )
        )
    return found


# The order is the order the pages come out in, and `silent` must run before
# `sensors`: the heartbeat one fills in is the threshold the other judges a
# channel against.
RULES = (
    silent,
    sensors,
    safety_fields,
    latches,
    float_judged,
    tank_runs,
    doses,
    proposals,
)


def evaluate(
    con: sqlite3.Connection,
    now: int,
    since: int,
    interval: int,
    silent_after: int,
) -> list[notify.Alert]:
    """Every alert rule, from database state alone: bare SELECTs on an
    autocommit connection, never a write. Each Alert carries its own
    record step so the tick can apply it only once its message went out.
    """
    ctx = read_state(con, since, interval, silent_after)
    found: list[notify.Alert] = []
    for rule in RULES:
        found += rule(con, now, ctx)
    return found


class Ticker:
    """The periodic pass and the loop around it, and what they remember.

    Three things, and only three: when this process started, the window it
    has been watching the boards over, and whether the one "butler is up"
    probe has gone out. Everything else a pass needs is read from the
    database or from the configuration.
    """

    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.started = int(time.time())
        self.observed = {"since": self.started, "last_tick": None}
        self.up_sent = False
        with store.connect(cfg.db) as con:
            prior = con.execute(
                "SELECT raised_ts, detail FROM alerts WHERE key = 'meta:tick'"
            ).fetchone()
        if prior is not None and self.started - prior[0] <= constants.RESUME_GRACE_S:
            # A short restart (a redeploy, a crash loop) must not blind the
            # silent and sensor rules for another threshold on every
            # incarnation: inherit the observation window the previous process
            # earned.
            with contextlib.suppress(TypeError, ValueError):
                self.observed["since"] = min(self.started, int(prior[1]))

    def tick(self, now: int | None = None) -> bool:
        """One alert pass; True only when everything it tried succeeded.

        Reads run on an autocommit connection — bare SELECTs, so the report
        path's BEGIN IMMEDIATE is never blocked behind a network call. Each
        record step is its own short write transaction, applied only after
        its message went out: a failed send leaves no row and the next tick
        retries it (at-least-once — a crash between send and record repeats
        a message; loud beats lost). The first failed send stops the loop,
        since everything behind it would fail the same way, and any unclean
        tick withholds the dead-man ping: an unreachable ntfy must trip the
        dead man, not feed it.
        """
        cfg = self.cfg
        now = int(time.time()) if now is None else now
        if (
            self.observed["last_tick"] is not None
            and now - self.observed["last_tick"] > 3 * cfg.beat
        ):
            self.observed["since"] = now  # the butler was away, not the boards
        self.observed["last_tick"] = now
        with store.connect(cfg.db) as con:
            con.execute("BEGIN IMMEDIATE")
            # The observation window survives short restarts through this
            # row (read back in __init__): a crash-looping butler must not
            # blind the silent and sensor rules on every incarnation.
            con.execute(
                "INSERT OR REPLACE INTO alerts "
                "(key, raised_ts, cleared_ts, detail) "
                "VALUES ('meta:tick', ?, NULL, ?)",
                (now, str(self.observed["since"])),
            )
        with store.connect(cfg.db) as con:
            pending = evaluate(
                con, now, self.observed["since"], cfg.interval, cfg.silent_after
            )
        ok = True
        attempted = False
        for alert in pending:
            if alert.message is not None:
                attempted = True
                if not cfg.send(alert):
                    ok = False
                    break
            with store.connect(cfg.db) as con:
                con.execute("BEGIN IMMEDIATE")
                alert.record(con)
        if ok and not self.up_sent and now - self.started >= constants.UP_AFTER_S:
            # One end-to-end probe of the topic: uptime-gated so a fast
            # crash loop never sends it, floored at one a day across
            # restarts so a slow loop cannot spam either. Without it, a
            # typo'd topic is a permanent, undetectable alert blackout:
            # ntfy answers 200 on any topic, and a healthy garden is also
            # silent.
            with store.connect(cfg.db) as con:
                (raised_count,) = con.execute(
                    f"SELECT COUNT(*) FROM alerts WHERE {pots.RAISED_SQL}"
                ).fetchone()
                last_probe = con.execute(
                    "SELECT raised_ts FROM alerts WHERE key = 'meta:up'"
                ).fetchone()
            if (
                last_probe is not None
                and now - last_probe[0] < constants.UP_PROBE_FLOOR_S
            ):
                self.up_sent = True  # probed recently enough, across restarts
            else:
                probe_alert = notify.Alert(
                    None,
                    "min",
                    "robot",
                    f"the butler is up; {raised_count} condition(s) raised",
                )
                attempted = True
                if cfg.send(probe_alert):
                    self.up_sent = True
                    with store.connect(cfg.db) as con:
                        con.execute("BEGIN IMMEDIATE")
                        con.execute(
                            "INSERT OR REPLACE INTO alerts "
                            "(key, raised_ts, cleared_ts, detail) "
                            "VALUES ('meta:up', ?, NULL, NULL)",
                            (now,),
                        )
                else:
                    ok = False
        if ok and not attempted and cfg.check is not None and not cfg.check():
            # A pass that sent nothing proved nothing: a healthy garden is
            # quiet, and the dead-man must still stop when ntfy has been
            # unreachable for days.
            ok = False
        if ok and cfg.ping is not None:
            ok = cfg.ping()
        return ok

    async def run(self) -> None:
        # The first tick comes a full beat after startup, never at t=0: a
        # crash-looping container must not reach the dead-man ping.
        while True:
            await asyncio.sleep(self.cfg.beat)
            try:
                await run_in_threadpool(self.tick)
            except Exception as why:  # noqa: BLE001 - the ticker survives anything
                print(f"alert tick failed: {why!r}", file=sys.stderr)
