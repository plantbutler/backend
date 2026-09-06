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
    later than all of it: the tests run inside one second, the counter is
    strict about which side of the tap a dose was sent on, and two float=0
    sightings inside the flap window are one float flapping, not two runs
    of the tank."""
    with sqlite3.connect(db) as con:
        con.execute("UPDATE refills SET ts = ts - ?", (seconds,))
        con.execute(
            "UPDATE status SET float_since = float_since - ?, "
            "float_bad = float_bad - ?, float_bad_prev = float_bad_prev - ?",
            (seconds, seconds, seconds),
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
    report(client, "c=0 ch0=1 float=1")
    tap(client, db)
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[0], 170)]
    # Then water flows and the float goes empty again on the same tap.
    report(client, "c=0 ch0=1 float=1")
    dose(client, 200, flow=210)
    report(client, "c=0 ch0=1 float=0")
    first, second = taps(db)
    assert samples(db) == [(first, 170), (second, 210)]


def test_a_first_report_has_no_previous_float_and_closes_nothing(client, db):
    since = tap(client, db)
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
    report(client, "c=0 ch0=1 float=1")
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == []
    # Back in service, the same tap and the same water close the sample.
    assert post(client, "/controller", "c=0 retired=0").status_code == 200
    report(client, "c=0 ch0=1 float=1")
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[0], 90)]


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
    tap(client, db)
    dose(client, 200, flow=200)
    dose(client, 50, flow=40)
    report(client, "c=0 ch0=1 float=0")
    entry = health(client)
    assert (entry["tank_ml"], entry["tank_samples"]) == (210, 2)
    assert entry["pumped_ml"] == 240
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
    # at full through the upgrade closes its sample on the first empty.
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
    assert run_sql(db, "SELECT float_word FROM status") == [(1,)]
    run_sql(
        db,
        "INSERT INTO commands (created_ts, controller, kind, outlet, ml, cap_s, "
        "state, source, sent_ts, acked_ts, flow_ml) "
        "VALUES (11, 0, 'water', 3, 100, 30, 'acked', 'manual', 11, 12, 100)",
    )
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(10, 100)]


# --------------------------------------------------------------------------- #
# Every sample is announced (spec D8)
# --------------------------------------------------------------------------- #


def run_the_tank_down(app, client, db, ml):
    """One run, a flap window after the last: a tap, `ml` through the meter
    in doses the board accepts (each handed on a report that says full),
    the float going empty, and a tick. Returns the tap's ts as it stands."""
    age(db, FLAP_WINDOW_S + 1)
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
    many the board has closed in its life (spec D5, D8)."""
    report(client, "c=0 ch0=1 float=1")
    for ml in (1000, 200, 200, 200, 200):
        run_the_tank_down(app, client, db, ml)
    assert sent[-1].message.endswith("(tank 200 ml over 5 samples)")
    since = run_the_tank_down(app, client, db, 200)  # the first is out
    assert health(client)["tank_samples"] == 6
    assert sent[-1].message == (
        f"board 0 ran its tank down: 200 ml since the refill at "
        f"{butler.hhmm(since)} (tank 200 ml over 5 samples)"
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
