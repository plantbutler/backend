"""The tank has a size: what the float said at the tap, the counter, the
samples the float closes, the median, the page each sample earns, and what
/health says about them (spec D1-D5, D8 and D9's three read-only fields)."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import butler
from butler import (
    FLAP_WINDOW_S,
    MAX_DOSE_ML,
    TANK_MEDIAN_OF,
    TANK_SAMPLES_TO_ARM,
    UP_AFTER_S,
    create_app,
)

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def sent():
    return []


@pytest.fixture
def app(db, sent):
    return create_app(
        db_path=str(db),
        token=TOKEN,
        next_s=60,
        cmd_ttl_s=900,
        quiet="0-0",
        send=lambda alert: sent.append(alert) or True,
        ping=lambda: True,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def post(client, path, body):
    return client.post(path, content=body, headers={"X-Token": TOKEN})


def report(client, body):
    answer = post(client, "/report", body)
    assert answer.status_code == 200, answer.text
    return answer


def health(client, controller=0):
    entries = client.get("/health").json()["controllers"]
    return next(c for c in entries if c["controller"] == controller)


def tick(app, now=None):
    return app.state.tick(now)


def keys(sent):
    return [a.key for a in sent if a.message is not None]


def run_sql(db, sql, *params):
    with sqlite3.connect(db) as con:
        return con.execute(sql, params).fetchall()


def age(db, seconds):
    """Everything so far happened `seconds` earlier, so what comes next is
    later than all of it: the tests run inside one second, a dose handed
    in an origin's own second is counted as after it, and two float=0
    sightings inside the flap window are one float flapping, not two runs
    of the tank."""
    with sqlite3.connect(db) as con:
        con.execute("UPDATE refills SET ts = ts - ?", (seconds,))
        con.execute(
            "UPDATE status SET float_since = float_since - ?, "
            "float_word_since = float_word_since - ?, "
            "float_seen = float_seen - ?, "
            "float_bad = float_bad - ?, float_bad_prev = float_bad_prev - ?",
            (seconds, seconds, seconds, seconds, seconds),
        )
        con.execute(
            "UPDATE commands SET created_ts = created_ts - ?, "
            "sent_ts = sent_ts - ?, acked_ts = acked_ts - ?",
            (seconds, seconds, seconds),
        )
        con.execute(
            "UPDATE tank_samples SET ts = ts - ?, refill_ts = refill_ts - ?",
            (seconds, seconds),
        )
        # The pages too, the ticker's own bookkeeping rows excepted (they
        # are its clock): a tap clears `over:` only when it is later than
        # the raise, and a raise from this same second must be able to be.
        con.execute(
            "UPDATE alerts SET raised_ts = raised_ts - ?, cleared_ts = cleared_ts - ? "
            "WHERE key NOT LIKE 'meta:%'",
            (seconds, seconds),
        )
        # The page a sample earned is keyed on its tap, so it moves with it:
        # left behind, the sample would look unannounced and page again.
        for (key,) in con.execute(
            "SELECT key FROM alerts WHERE key LIKE 'tank:%'"
        ).fetchall():
            head, refill_ts = key.rsplit(":", 1)
            con.execute(
                "UPDATE alerts SET key = ? WHERE key = ?",
                (f"{head}:{int(refill_ts) - seconds}", key),
            )


def tap(client, db):
    """The human says the tank is full, a minute ago. Returns the tap's
    ts as it stands now; a later `age` moves it again, so a test that taps
    twice reads the taps back with `taps`."""
    answer = post(client, "/refill", "c=0")
    assert answer.status_code == 200, answer.text
    ts = int(answer.text.removeprefix("refill=").strip())
    age(db, 60)
    return ts - 60


def hand(client, ml):
    """A manual dose, handed to the board on its next report."""
    answer = post(client, "/command", f"c=0 water=3 ml={ml}")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    handed = report(client, "c=0 ch0=1 float=1 pos=ok").text
    assert f"cmd={cmd_id} water=3 ml={ml}" in handed
    return cmd_id


def ack(client, cmd_id, flow=None, float_ok=1):
    count = "" if flow is None else f" flow_ml={flow}"
    report(client, f"c=0 ch0=1 float={float_ok} pos=ok ack={cmd_id}{count}")


def dose(client, ml, flow=None):
    ack(client, hand(client, ml), flow)


def samples(db):
    return run_sql(db, "SELECT refill_ts, ml FROM tank_samples ORDER BY ts, rowid")


def refills(db):
    return run_sql(db, "SELECT float_ok FROM refills ORDER BY ts, rowid")


def taps(db):
    return [ts for (ts,) in run_sql(db, "SELECT ts FROM refills ORDER BY ts, rowid")]


def origin(db):
    with sqlite3.connect(db) as con:
        return butler.counter_origin(con, 0)


def word_since(db):
    """When the float's word last changed: its latest rise while it is 1."""
    return run_sql(db, "SELECT float_word_since FROM status WHERE controller = 0")[0][0]


def vm_steps(con, fetch):
    """SQLite's own count of the virtual-machine steps `fetch` costs on
    `con`, without a clock."""
    fetch()  # warm: a statement's first run pays for its plan
    n = 0

    def count():
        nonlocal n
        n += 1
        return 0

    con.set_progress_handler(count, 1)
    try:
        fetch()
    finally:
        con.set_progress_handler(None, 0)
    return n


# --------------------------------------------------------------------------- #
# The tap snapshots the float (spec D2)
# --------------------------------------------------------------------------- #


def test_a_tap_remembers_what_the_float_said(client, db):
    assert post(client, "/refill", "c=0").status_code == 200  # never reported
    report(client, "c=0 ch0=1 float=1")
    assert post(client, "/refill", "c=0").status_code == 200
    report(client, "c=0 ch0=1 float=0")
    assert post(client, "/refill", "c=0").status_code == 200
    assert refills(db) == [(None,), (1,), (0,)]


# --------------------------------------------------------------------------- #
# The counter (spec D3)
# --------------------------------------------------------------------------- #


def test_the_counter_is_acked_water_sent_after_the_tap(client, db):
    report(client, "c=0 ch0=1 float=1")
    assert health(client)["pumped_ml"] == 0  # no tap: nothing to count from
    dose(client, 100, flow=100)  # before the tap
    age(db, 60)
    since = tap(client, db)
    dose(client, 150, flow=140)  # the meter's count wins over the dose
    dose(client, 50)  # an ack without a count is charged the dose
    hand(client, 70)  # never acked: expired on the next report, uncounted
    report(client, "c=0 ch0=1 float=1 pos=ok")
    with sqlite3.connect(db) as con:
        # Another board's water is that board's.
        con.execute(
            "INSERT INTO commands (created_ts, controller, kind, outlet, ml, "
            "cap_s, state, source, sent_ts, acked_ts, flow_ml) "
            "VALUES (?, 1, 'water', 3, 500, 30, 'acked', 'manual', ?, ?, 500)",
            (since + 1, since + 1, since + 2),
        )
        assert butler.pumped_since(con, 0, since) == 190
        assert butler.pumped_since(con, 0, since - 1000) == 290
        assert butler.pumped_since(con, 1, since) == 500
    assert health(client)["pumped_ml"] == 190


def test_the_counter_starts_at_the_latest_tap_that_saw_the_float_or_the_rise(
    client, db
):
    """The origin is the later of the latest tap whose snapshot is not NULL
    and the float's latest rise; the tap on a tie. A tap the board never
    saw is no origin — a month of untapped top-ups behind it is not a
    measurement — and a float that went 1 -> 0 -> 1 since the tap is a
    tank refilled by someone who forgot to tap: the counter restarts at
    the rise instead of calling the float stuck (spec D3, amended)."""
    assert origin(db) is None  # nothing said, nothing tapped
    assert post(client, "/refill", "c=0").status_code == 200  # never sent float=
    age(db, 60)
    assert origin(db) is None  # a tap that saw nothing is no origin
    report(client, "c=0 ch0=1 float=1")  # the float's first word is a rise
    assert origin(db) == (word_since(db), "rise")
    age(db, 60)
    dose(client, 100, flow=100)
    assert health(client)["pumped_ml"] == 100  # counted from the rise, tap or no tap
    age(db, 60)
    since = tap(client, db)  # later than the rise and the dose, and it saw the float
    assert origin(db) == (since, "tap")
    assert health(client)["pumped_ml"] == 0
    run_sql(db, "UPDATE refills SET float_ok = NULL WHERE ts = ?", since)
    assert origin(db) == (word_since(db), "rise")  # blind after all: no origin
    assert health(client)["pumped_ml"] == 100
    run_sql(db, "UPDATE refills SET float_ok = 1 WHERE ts = ?", since)
    # The float goes empty and full again with nobody tapping.
    dose(client, 100, flow=100)
    report(client, "c=0 ch0=1 float=0")
    assert origin(db) == (since, "tap")  # empty: its last rise is behind the tap
    assert health(client)["pumped_ml"] == 100
    age(db, 60)
    report(client, "c=0 ch0=1 float=1")
    rise = word_since(db)
    assert rise > since and origin(db) == (rise, "rise")
    assert health(client)["pumped_ml"] == 0  # that water went before the rise
    age(db, 60)
    dose(client, 50, flow=50)
    assert health(client)["pumped_ml"] == 50
    # A rise in the tap's own second is the tap's: the human's word wins.
    report(client, "c=0 ch0=1 float=0")
    report(client, "c=0 ch0=1 float=1")
    since = tap(client, db)
    assert word_since(db) == since and origin(db) == (since, "tap")


def test_a_report_without_float_moves_no_rise(client, db):
    """A report that says nothing about the float neither rises nor
    falls: the rise the float had stands through it, and none is invented
    when it says the same word again — the word's own clock, not
    float_since, which such a report restarts (spec D3, amended)."""
    report(client, "c=0 ch0=1 float=1")
    since = tap(client, db)
    dose(client, 150, flow=150)
    report(client, "c=0 ch0=1 float=0")  # ran down...
    age(db, 60)
    report(client, "c=0 ch0=1 float=1")  # ...and refilled, untapped
    rise = word_since(db)
    assert rise > since and origin(db) == (rise, "rise")
    assert health(client)["pumped_ml"] == 0  # that water went before the rise
    report(client, "c=0 ch0=1")  # says nothing about the float
    assert origin(db) == (rise, "rise") and health(client)["pumped_ml"] == 0
    report(client, "c=0 ch0=1 float=1")  # the same word again is no rise
    assert word_since(db) == rise and origin(db) == (rise, "rise")
    # Nor is full, nothing, full since a tap: the counter keeps its water.
    age(db, 120)
    since = tap(client, db)
    dose(client, 100, flow=100)
    report(client, "c=0 ch0=1")
    report(client, "c=0 ch0=1 float=1")
    assert origin(db) == (since, "tap") and health(client)["pumped_ml"] == 100


def test_a_dose_typed_before_the_tap_and_handed_after_it_counts(client, db):
    """The counter's side of the tap is the board's, sent_ts — when it was
    handed the dose — not the phone's created_ts: water typed before the
    tap and pumped after it left the full tank (spec D3)."""
    report(client, "c=0 ch0=1 float=1")
    answer = post(client, "/command", "c=0 water=3 ml=70")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    age(db, 60)  # typed a minute before...
    since = tap(client, db)  # ...the tap, itself a minute ago
    ((created,),) = run_sql(db, "SELECT created_ts FROM commands WHERE id = ?", cmd_id)
    assert created < since
    handed = report(client, "c=0 ch0=1 float=1 pos=ok").text  # after the tap
    assert f"cmd={cmd_id} water=3 ml=70" in handed
    ack(client, cmd_id, flow=70)
    assert health(client)["pumped_ml"] == 70


def test_a_dose_handed_on_the_report_that_raises_the_float_counts(client, db):
    """The report that first says full after empty is the rise, and it
    hands whatever was queued with the same clock: that dose pumps after
    it was handed, from the refilled tank, and a counter strict about the
    origin's second lost it for ever (spec D3, amended)."""
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    report(client, "c=0 ch0=1 float=0")  # ran down
    age(db, 60)
    answer = post(client, "/command", "c=0 water=3 ml=80")  # typed while empty
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    # Refilled untapped: this one report is the rise and hands the dose.
    handed = report(client, "c=0 ch0=1 float=1 pos=ok").text
    assert f"cmd={cmd_id} water=3 ml=80" in handed
    rise = word_since(db)
    assert origin(db) == (rise, "rise")
    assert run_sql(db, "SELECT sent_ts FROM commands WHERE id = ?", cmd_id) == [(rise,)]
    ack(client, cmd_id, flow=80)
    assert health(client)["pumped_ml"] == 80


def test_a_dose_handed_in_the_taps_own_second_counts(client, db):
    """The tank was filled before the human tapped, and the dose pumps
    after it was handed: a hand-off in the tap's second left the full
    tank (spec D3, amended)."""
    report(client, "c=0 ch0=1 float=1")
    age(db, 60)
    cmd_id = hand(client, 60)
    since = tap(client, db)
    assert run_sql(db, "SELECT sent_ts FROM commands WHERE id = ?", cmd_id) == [(since,)]
    ack(client, cmd_id, flow=60)
    assert health(client)["pumped_ml"] == 60


def test_a_stop_acked_with_a_count_is_not_water(client, db):
    """A stop's ack may carry flow_ml=: what flowed before the board
    stopped, which the dose's own ack already counted. The ack step stamps
    it on the stop row like any other, so only the counter's kind = 'water'
    keeps it out of pumped_ml and out of the sample (spec D3)."""
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 100, flow=90)
    answer = post(client, "/command", "c=0 stop=1")
    assert answer.status_code == 200, answer.text
    stop_id = int(answer.text.strip().removeprefix("cmd="))
    assert f"cmd={stop_id} stop=1" in report(client, "c=0 ch0=1 float=1 pos=ok").text
    ack(client, stop_id, flow=60, float_ok=0)  # and the float drops on that report
    assert run_sql(
        db, "SELECT kind, ml, flow_ml FROM commands WHERE id = ?", stop_id
    ) == [("stop", None, 60)]
    assert health(client)["pumped_ml"] == 90
    assert samples(db) == [(taps(db)[0], 90)]


# --------------------------------------------------------------------------- #
# Learning a sample (spec D4)
# --------------------------------------------------------------------------- #


def test_the_float_going_empty_closes_one_sample_per_tap(client, db):
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 100, flow=90)
    # The dose that drains the tank acks on the very report that says
    # empty, and that count is part of the run.
    ack(client, hand(client, 100), flow=80, float_ok=0)
    assert samples(db) == [(taps(db)[0], 170)]
    report(client, "c=0 ch0=1 float=0")  # still empty: nothing new
    report(client, "c=0 ch0=1 float=1")  # bouncing at the line, no tap
    dose(client, 100, flow=100)  # and watered while it says full
    report(client, "c=0 ch0=1 float=0")  # the first crossing stands, not 270
    assert samples(db) == [(taps(db)[0], 170)]
    # A tap, nothing pumped, and the float goes empty: not a measurement.
    age(db, 60)
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[0], 170)]
    # Water flows and the float goes empty again on the same tap — but it
    # rose since the tap, so the tank was refilled by someone who did not
    # say so, and how full it was is anybody's guess (spec D4, amended).
    report(client, "c=0 ch0=1 float=1")
    assert origin(db)[1] == "rise"
    dose(client, 200, flow=210)
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[0], 170)]
    # Said so: the next tap's run is a sample.
    age(db, 60)
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 200, flow=210)
    report(client, "c=0 ch0=1 float=0")
    first, _second, third = taps(db)
    assert samples(db) == [(first, 170), (third, 210)]


def test_a_contra_report_or_a_standing_latch_closes_no_sample(client, db):
    """ch207=1 is "float OK, zero pulses", and zero pulses is a dead meter,
    a kinked tube, a dead pump or 12 V absent as often as anything about
    the tank: a fault, not a measurement (spec §1, D4 amended)."""
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    cmd_id = hand(client, 100)
    report(client, f"c=0 ch0=1 float=0 pos=ok ack={cmd_id} flow_ml=90 ch207=1")
    assert health(client)["latched"]["reason"] == "contra"
    assert health(client)["pumped_ml"] == 90  # acked water is a fact
    assert samples(db) == []
    # A latch standing from before the edge — here the reset with the pump
    # running, the float saying full throughout — closes none either.
    assert post(client, "/resume", "c=0").status_code == 200
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 100, flow=90)
    report(client, "c=0 ch0=1 float=1 err=resetmid")
    report(client, "c=0 ch0=1 float=0")
    assert health(client)["latched"]["reason"] == "resetmid"
    assert samples(db) == []
    # Resumed, a tap and a run: learning again.
    assert post(client, "/resume", "c=0").status_code == 200
    age(db, 60)
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 100, flow=90)
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[-1], 90)]


def test_a_first_report_has_no_previous_float_and_closes_nothing(client, db):
    since = tap(client, db)
    run_sql(db, "UPDATE refills SET float_ok = 1")  # a tap that saw the float
    run_sql(
        db,
        "INSERT INTO commands (created_ts, controller, kind, outlet, ml, cap_s, "
        "state, source, sent_ts, acked_ts, flow_ml) "
        "VALUES (?, 0, 'water', 3, 100, 30, 'acked', 'manual', ?, ?, 100)",
        since + 1, since + 1, since + 2,
    )
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == []
    # And a board that never tapped stores nothing however the float moves.
    run_sql(db, "DELETE FROM refills")
    report(client, "c=0 ch0=1 float=1")
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == []


def test_a_report_without_float_hides_no_edge(client, db):
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 100, flow=90)
    report(client, "c=0 ch0=1")  # says nothing about the float
    assert run_sql(db, "SELECT float_ok, float_word FROM status") == [(None, 1)]
    report(client, "c=0 ch0=1 float=0")  # full, silent, empty: an edge
    assert samples(db) == [(taps(db)[0], 90)]
    # Empty, silent, empty is not one, whatever the counter says.
    since = tap(client, db)
    run_sql(
        db,
        "INSERT INTO commands (created_ts, controller, kind, outlet, ml, cap_s, "
        "state, source, sent_ts, acked_ts, flow_ml) "
        "VALUES (?, 0, 'water', 3, 100, 30, 'acked', 'manual', ?, ?, 100)",
        since + 1, since + 1, since + 2,
    )
    report(client, "c=0 ch0=1")
    report(client, "c=0 ch0=1 float=0")
    first, _second = taps(db)
    assert samples(db) == [(first, 90)]


def test_a_retired_board_learns_nothing(client, db):
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    cmd_id = hand(client, 100)  # with the board when it is retired
    assert post(client, "/controller", "c=0 retired=1").status_code == 200
    ack(client, cmd_id, flow=90, float_ok=0)  # the ack lands, the float drops
    assert health(client)["pumped_ml"] == 90  # acked water is a fact
    assert samples(db) == []  # a measurement is learning
    # Back in service, the float has risen since that tap; a new tap and a
    # new run are what it learns from.
    assert post(client, "/controller", "c=0 retired=0").status_code == 200
    age(db, 60)
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 100, flow=90)
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[-1], 90)]


# --------------------------------------------------------------------------- #
# The size (spec D5)
# --------------------------------------------------------------------------- #


def test_tank_ml_is_the_median_of_the_last_five_and_none_under_two(db, app):
    assert TANK_SAMPLES_TO_ARM == 2 and TANK_MEDIAN_OF == 5
    with sqlite3.connect(db) as con:
        size = lambda controller=0: butler.tank_ml(con, controller)  # noqa: E731

        def sample(ts, ml, controller=0):
            con.execute(
                "INSERT INTO tank_samples (ts, controller, refill_ts, ml) "
                "VALUES (?, ?, ?, ?)",
                (ts, controller, ts, ml),
            )

        assert size() is None
        sample(1, 4000)
        assert size() is None  # one is not a size
        sample(2, 4201)
        assert size() == 4100  # of two, their mean
        sample(3, 9000)  # a tap that was not a fill
        assert size() == 4201
        sample(4, 4100)
        sample(5, 4300)
        assert size() == 4201
        sample(6, 4400)  # the sixth pushes the first out of the window
        sample(7, 4500)
        assert size() == 4400  # median of 9000, 4100, 4300, 4400, 4500
        sample(8, 1, controller=1)
        sample(9, 2, controller=1)
        assert size() == 4400 and size(1) == 1


def test_the_median_is_of_the_newest_five_not_the_five_largest():
    """tank_ml is handed five at most (tank_history's LIMIT); the ticker
    hands tank_median a sample and the five before it, and there the
    window is the last five by time — the oldest leaves, however large
    (spec D5)."""
    assert butler.tank_median([5000, 100, 100, 100, 3000, 3000]) == 100


def test_tank_history_is_the_last_few_up_to_a_sample(db, app):
    """Five by default, oldest first, and up to a sample when one is
    named: two closed at the same second are told apart by rowid, so the
    later one is not in the run of the earlier."""
    with sqlite3.connect(db) as con:
        history = butler.tank_history

        def sample(ts, ml, controller=0):
            cur = con.execute(
                "INSERT INTO tank_samples (ts, controller, refill_ts, ml) "
                "VALUES (?, ?, ?, ?)",
                (ts, controller, ml, ml),
            )
            return (ts, cur.lastrowid)

        assert history(con, 0) == []
        at = {ml: sample(ml // 100, ml) for ml in range(100, 800, 100)}
        sample(7, 750)  # the same second as 700, a later row
        sample(4, 9, controller=1)
        assert history(con, 0) == [400, 500, 600, 700, 750]
        assert history(con, 0, at[600]) == [200, 300, 400, 500, 600]
        assert history(con, 0, at[600], TANK_MEDIAN_OF + 1) == [
            100, 200, 300, 400, 500, 600
        ]
        assert history(con, 0, at[700]) == [300, 400, 500, 600, 700]
        assert history(con, 1) == [9]


def test_tank_history_costs_the_same_however_many_runs_a_board_has_closed(db, app):
    """Read on every report, tick and /health, so the fetch is bounded in
    SQLite, not in Python after it: walking the index back from the newest
    takes the same steps over six hundred samples as over six. SQLite's
    own step counter says so, without a clock."""
    with sqlite3.connect(db) as con:

        def fill(total):
            con.execute("DELETE FROM tank_samples")
            con.executemany(
                "INSERT INTO tank_samples (ts, controller, refill_ts, ml) "
                "VALUES (?, 0, ?, 100)",
                [(t, t) for t in range(1, total + 1)],
            )
            (newest,) = con.execute(
                "SELECT rowid FROM tank_samples ORDER BY ts DESC, rowid DESC"
            ).fetchone()
            return (
                vm_steps(con, lambda: butler.tank_ml(con, 0)),
                vm_steps(
                    con,
                    lambda: butler.tank_history(
                        con, 0, (total, newest), TANK_MEDIAN_OF + 1
                    ),
                ),
            )

        few, many = fill(6), fill(600)
        assert many[0] <= few[0], (few, many)  # the size
        assert many[1] <= few[1], (few, many)  # a sample's judgement


def test_finding_the_unannounced_samples_costs_the_pending_few(db, app):
    """Every tick looks for the samples with no page yet, so that walk is
    bounded in SQLite to the pending ones plus one: the same steps over
    six hundred announced runs as over six, and more only with more
    pending (spec D8)."""
    with sqlite3.connect(db) as con:

        def fill(total, pending):
            con.execute("DELETE FROM tank_samples")
            con.execute("DELETE FROM alerts")
            con.executemany(
                "INSERT INTO tank_samples (ts, controller, refill_ts, ml) "
                "VALUES (?, 0, ?, 100)",
                [(t, t) for t in range(1, total + 1)],
            )
            con.executemany(
                "INSERT INTO alerts (key, raised_ts, cleared_ts) VALUES (?, ?, NULL)",
                [(f"tank:0:{t}", t) for t in range(1, total + 1 - pending)],
            )
            found = butler.unannounced_samples(con, 0)
            assert [(ts, refill_ts) for ts, _rowid, refill_ts, _ml in found] == [
                (t, t) for t in range(total + 1 - pending, total + 1)
            ]
            return vm_steps(con, lambda: butler.unannounced_samples(con, 0))

        few, many = fill(6, 1), fill(600, 1)
        assert many <= few, (few, many)
        assert fill(600, 3) > many  # the pending ones are the cost
        assert butler.unannounced_samples(con, 1) == []  # another board's are its own


# --------------------------------------------------------------------------- #
# What the app sees (spec D9, the read-only fields)
# --------------------------------------------------------------------------- #


def test_health_carries_the_size_the_count_and_the_counter(client, db):
    report(client, "c=0 ch0=1 float=1")
    entry = health(client)
    assert entry["tank_ml"] is None
    assert entry["tank_samples"] == 0
    assert entry["pumped_ml"] == 0
    tap(client, db)
    dose(client, 200, flow=180)
    report(client, "c=0 ch0=1 float=0")
    report(client, "c=0 ch0=1 float=1")
    entry = health(client)
    assert (entry["tank_ml"], entry["tank_samples"]) == (None, 1)
    age(db, 60)
    tap(client, db)
    dose(client, 200, flow=200)
    dose(client, 50, flow=40)
    report(client, "c=0 ch0=1 float=0")
    entry = health(client)
    assert (entry["tank_ml"], entry["tank_samples"]) == (210, 2)
    assert entry["pumped_ml"] == 240
    age(db, 60)
    tap(client, db)
    assert health(client)["pumped_ml"] == 0


def test_an_existing_database_gains_the_snapshot_column_at_startup(db):
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE refills (ts INTEGER NOT NULL, controller INTEGER NOT NULL);
            INSERT INTO refills VALUES (5, 0);
            """
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    assert client.get("/health").status_code == 200
    report(client, "c=0 ch0=1 float=1")
    assert post(client, "/refill", "c=0").status_code == 200
    assert refills(db) == [(None,), (1,)]  # the old row judges nothing


def test_an_existing_database_carries_the_floats_last_word_at_startup(db):
    # The 0.18.0 shape of status: float_ok, no float_word. A tank sitting
    # at full through the upgrade closes its sample on the first empty —
    # from a tap made after it: the one from before saw nothing (it gets
    # NULL) and starts no counter (spec D3).
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE status (
              controller INTEGER PRIMARY KEY, ts INTEGER NOT NULL, float_ok INTEGER,
              float_since INTEGER, pos TEXT, pos_since INTEGER, float_seen INTEGER,
              pos_seen INTEGER, float_bad INTEGER, float_bad_prev INTEGER,
              pos_bad INTEGER, pos_bad_prev INTEGER, err TEXT, err_ts INTEGER,
              latched_ts INTEGER, latch_reason TEXT, pos_ok_seen INTEGER);
            INSERT INTO status (controller, ts, float_ok, float_since) VALUES (0, 5, 1, 5);
            CREATE TABLE refills (ts INTEGER NOT NULL, controller INTEGER NOT NULL);
            INSERT INTO refills VALUES (10, 0);
            """
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    # The word and its clock, carried from float_ok and float_since: the
    # rise the float had before the upgrade is where it was.
    assert run_sql(db, "SELECT float_word, float_word_since FROM status") == [(1, 5)]
    assert post(client, "/refill", "c=0").status_code == 200  # snapshots the carried 1
    run_sql(db, "UPDATE refills SET ts = ts - 60 WHERE float_ok IS NOT NULL")
    assert refills(db) == [(None,), (1,)]
    since = taps(db)[-1]
    run_sql(
        db,
        "INSERT INTO commands (created_ts, controller, kind, outlet, ml, cap_s, "
        "state, source, sent_ts, acked_ts, flow_ml) "
        "VALUES (?, 0, 'water', 3, 100, 30, 'acked', 'manual', ?, ?, 100)",
        since + 1, since + 1, since + 2,
    )
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(since, 100)]


# --------------------------------------------------------------------------- #
# Every sample is announced (spec D8)
# --------------------------------------------------------------------------- #


def run_the_tank_down(app, client, db, ml):
    """One run, a flap window after the last: the float saying full again
    (before the tap — a rise after it would be the origin, and no sample
    closes on a rise), a tap, `ml` through the meter in doses the board
    accepts, the float going empty, and a tick. Returns the tap's ts as it
    stands."""
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=1")
    since = tap(client, db)
    while ml:
        part = min(ml, MAX_DOSE_ML)
        dose(client, part, flow=part)
        ml -= part
    report(client, "c=0 ch0=1 float=0")
    tick(app)
    return since


def test_every_sample_is_announced_once(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1")
    first = tap(client, db)
    dose(client, 200, flow=190)
    report(client, "c=0 ch0=1 float=0")
    tick(app)
    tick(app)
    assert keys(sent) == [f"tank:0:{first}"]  # once
    (alert,) = sent
    assert (alert.priority, alert.tags) == ("default", "droplet")
    assert alert.message == (
        f"board 0 ran its tank down: 190 ml since the refill at "
        f"{butler.hhmm(first)} (tank size learning, 1 of 2)"
    )
    # With one sample behind it there is no size to drift from, however
    # far off the second lands: it is announced, and the two make a size.
    second = run_the_tank_down(app, client, db, 400)
    assert keys(sent) == [f"tank:0:{first}", f"tank:0:{second}"]
    assert sent[-1].tags == "droplet"
    assert sent[-1].message == (
        f"board 0 ran its tank down: 400 ml since the refill at "
        f"{butler.hhmm(second)} (tank 295 ml over 2 samples)"
    )
    # Marked like a dose judgement: a row that is never cleared.
    assert run_sql(
        db, "SELECT cleared_ts FROM alerts WHERE key LIKE 'tank:%' ORDER BY key"
    ) == [(None,), (None,)]
    # A tap, nothing pumped, the float going empty: no sample, no page.
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    report(client, "c=0 ch0=1 float=0")
    tick(app)
    assert len(sent) == 2


def test_two_runs_waiting_on_one_tick_are_each_judged_as_they_closed(
    app, client, db, sent
):
    """A tick that finds two samples waiting (the ticker was down, or a
    send failed) judges the first against what the tank knew when it
    closed — nothing — not against the second, which had not happened
    yet (spec D8)."""
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 200, flow=200)
    report(client, "c=0 ch0=1 float=0")
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    dose(client, 250, flow=250)
    dose(client, 150, flow=150)
    report(client, "c=0 ch0=1 float=0")
    tick(app)
    first, second = taps(db)
    assert keys(sent) == [f"tank:0:{first}", f"tank:0:{second}"]
    assert sent[0].message.endswith("(tank size learning, 1 of 2)")
    assert sent[1].message.endswith("(tank 300 ml over 2 samples)")


def test_a_sample_off_the_size_it_knew_is_a_warning(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1")
    run_the_tank_down(app, client, db, 200)
    run_the_tank_down(app, client, db, 200)
    since = run_the_tank_down(app, client, db, 250)  # 200 + 25 %: at the line
    assert sent[-1].tags == "droplet"
    assert sent[-1].message == (
        f"board 0 ran its tank down: 250 ml since the refill at "
        f"{butler.hhmm(since)} (tank 200 ml over 3 samples)"
    )
    run_the_tank_down(app, client, db, 251)  # past it, against the earlier three
    assert (sent[-1].priority, sent[-1].tags) == ("default", "warning")
    assert sent[-1].message == (
        "board 0's tank measured 251 ml this run, not the 200 ml it knew: a "
        "different tank, a clogging meter, or a tap that was not a fill"
    )
    run_the_tank_down(app, client, db, 100)  # short of it: the same warning
    assert sent[-1].tags == "warning"
    assert sent[-1].message.startswith(
        "board 0's tank measured 100 ml this run, not the 225 ml it knew"
    )
    assert health(client)["tank_samples"] == 5  # a warning is still a sample
    assert len(keys(sent)) == 5


def test_the_count_is_the_samples_the_size_rests_on(app, client, db, sent):
    """The size is the median of the last five, so the count beside it
    stops at five: past that, the oldest run has left the number, however
    many the board has closed in its life (spec D5, D8). Left by age, not
    by size: the sixth run's page is handed six samples where /health's
    tank_ml is handed five, and the two must name the same tank."""
    report(client, "c=0 ch0=1 float=1")
    for ml in (1000, 200, 200, 300, 300):
        run_the_tank_down(app, client, db, ml)
    assert sent[-1].message.endswith("(tank 300 ml over 5 samples)")
    since = run_the_tank_down(app, client, db, 250)  # the first, the largest, is out
    assert health(client)["tank_samples"] == 6
    assert sent[-1].message == (
        f"board 0 ran its tank down: 250 ml since the refill at "
        f"{butler.hhmm(since)} (tank 250 ml over 5 samples)"
    )
    assert health(client)["tank_ml"] == 250  # the app's number is the page's


def test_a_sample_is_judged_against_the_five_before_it(app, client, db, sent):
    """The size a sample is held to is the median of the five that came
    before it, not of the four: a run that fetched only the window ending
    at the sample would judge it against a different number."""
    report(client, "c=0 ch0=1 float=1")
    for ml in (1000, 1000, 1000, 200, 200):
        run_the_tank_down(app, client, db, ml)
    # Of the five before: 1000. Of the four before: 600, and 1000 would
    # be a warning against that.
    since = run_the_tank_down(app, client, db, 1000)
    assert sent[-1].tags == "droplet"
    assert sent[-1].message == (
        f"board 0 ran its tank down: 1000 ml since the refill at "
        f"{butler.hhmm(since)} (tank 1000 ml over 5 samples)"
    )


def test_the_announcements_never_reach_the_app_or_the_up_count(
    app, client, db, sent
):
    report(client, "c=0 ch0=1 float=1")
    run_the_tank_down(app, client, db, 200)
    assert len(keys(sent)) == 1
    assert client.get("/health").json()["alerts"] == []
    # The probe's tick is ten minutes on, when the board would be silent;
    # retired, it is quiet, and its announcement is the one row standing.
    assert post(client, "/controller", "c=0 retired=1").status_code == 200
    assert run_sql(
        db, "SELECT cleared_ts FROM alerts WHERE key LIKE 'tank:%'"
    ) == [(None,)]
    tick(app, int(time.time()) + UP_AFTER_S + 1)
    probe = sent[-1]
    assert probe.key is None
    assert probe.message == "the butler is up; 0 condition(s) raised"


def test_a_retired_boards_sample_waits_for_it(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1")
    since = tap(client, db)
    dose(client, 200, flow=200)
    report(client, "c=0 ch0=1 float=0")
    assert post(client, "/controller", "c=0 retired=1").status_code == 200
    tick(app)
    assert keys(sent) == []
    # Skipped, not forgotten: back in service, the run is announced.
    assert post(client, "/controller", "c=0 retired=0").status_code == 200
    tick(app)
    assert keys(sent) == [f"tank:0:{since}"]
