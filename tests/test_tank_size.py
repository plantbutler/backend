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
        con.execute(
            "UPDATE refills SET ts = ts - ?, drop_ts = drop_ts - ?", (seconds, seconds)
        )
        con.execute(
            "UPDATE status SET float_since = float_since - ?, "
            "float_word_since = float_word_since - ?, float_rise = float_rise - ?, "
            "float_seen = float_seen - ?, float_bad = float_bad - ?, "
            "float_bad_prev = float_bad_prev - ?",
            (seconds,) * 6,
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


def full(client):
    """The float says full, twice. One sighting is not yet the word the
    tank is measured on: the firm word is what two consecutive reports
    that carry float= agree on, and its rise is where the word rose."""
    report(client, "c=0 ch0=1 float=1")
    report(client, "c=0 ch0=1 float=1")


def empty(client):
    """The float says empty, twice: one sighting is a glitch by the
    board's own design (any of its three samples failing fails the
    word), and the drop is the firm word's, confirmed by the second."""
    report(client, "c=0 ch0=1 float=0")
    report(client, "c=0 ch0=1 float=0")


def still_empty(client, db):
    """A flap window on, the float still says empty: the sighting that
    confirms an earlier one — a dose's ack, a first report of empty —
    far enough from it that the two are the tank's run and not a float
    flapping at the line, which is the float: rule's subject and would
    page here."""
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=0")


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
    """When the float's word last changed, 1 -> 0 or 0 -> 1."""
    return run_sql(db, "SELECT float_word_since FROM status WHERE controller = 0")[0][0]


def rise(db):
    """When the float's firm word last went 0 -> 1."""
    return run_sql(db, "SELECT float_rise FROM status WHERE controller = 0")[0][0]


def firm(db):
    """The float's firm word: what two consecutive reports agreed on. Its
    clocks are the edges the tank is measured on, `drops` and `rise`."""
    return run_sql(db, "SELECT float_firm FROM status WHERE controller = 0")[0][0]


def drops(db):
    """Each tap's drop_ts, the first time the word went 1 -> 0 after it."""
    return [ts for (ts,) in run_sql(db, "SELECT drop_ts FROM refills ORDER BY ts, rowid")]


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
    """The board's last real word, not the latest report's float_ok,
    which one report that omits float= blanks: a tap made under a row
    reading "float ?" is a tap all the same, and must not be one that
    counts for nothing. NULL only for a board that has never said
    float= (spec D2, thrice)."""
    assert post(client, "/refill", "c=0").status_code == 200  # never reported
    report(client, "c=0 ch0=1 float=1")
    assert post(client, "/refill", "c=0").status_code == 200
    report(client, "c=0 ch0=1 float=0")
    assert post(client, "/refill", "c=0").status_code == 200
    report(client, "c=0 ch0=1")  # says nothing about the float
    assert run_sql(db, "SELECT float_ok FROM status WHERE controller = 0") == [(None,)]
    assert post(client, "/refill", "c=0").status_code == 200  # the last real word
    assert refills(db) == [(None,), (1,), (0,), (0,)]
    report(client, "c=1 ch0=1")  # reported, but never a word on the float
    assert post(client, "/refill", "c=1").status_code == 200
    assert run_sql(db, "SELECT float_ok FROM refills WHERE controller = 1") == [(None,)]


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
    """The base is the latest tap whose snapshot is not NULL; the float's
    latest rise is the origin only once the word has gone 1 -> 0 after that
    tap (its drop_ts) and rose later. A tap the board never saw is no
    origin — a month of untapped top-ups behind it is not a measurement —
    and without one the float's rises count for nothing. A float that
    went 1 -> 0 -> 1 since the tap is a tank refilled by someone who
    forgot to tap: the counter restarts at the rise instead of calling the
    float stuck; the same second is the tap's (spec D3, amended twice)."""
    assert origin(db) is None  # nothing said, nothing tapped
    assert post(client, "/refill", "c=0").status_code == 200  # never sent float=
    age(db, 60)
    assert origin(db) is None  # a tap that saw nothing is no origin
    report(client, "c=0 ch0=1 float=1")  # the float's first word...
    assert rise(db) is None  # ...is one sighting, not yet a rise
    report(client, "c=0 ch0=1 float=1")  # the next agrees: the first firm full
    assert rise(db) is not None and origin(db) is None  # a rise with no tap is none
    age(db, 60)
    dose(client, 100, flow=100)
    assert health(client)["pumped_ml"] == 0  # nothing to count from
    age(db, 60)
    since = tap(client, db)  # later than the rise and the dose, and it saw the float
    assert origin(db) == (since, "tap")
    assert health(client)["pumped_ml"] == 0
    run_sql(db, "UPDATE refills SET float_ok = NULL WHERE ts = ?", since)
    assert origin(db) is None  # blind after all: no origin
    run_sql(db, "UPDATE refills SET float_ok = 1 WHERE ts = ?", since)
    # The float goes empty and full again with nobody tapping.
    dose(client, 100, flow=100)
    empty(client)
    assert drops(db) == [None, word_since(db)]  # the tap's first drop
    assert origin(db) == (since, "tap")  # empty: its last rise is behind the tap
    assert health(client)["pumped_ml"] == 100
    age(db, 60)
    full(client)
    assert rise(db) > since and origin(db) == (rise(db), "rise")
    assert health(client)["pumped_ml"] == 0  # that water went before the rise
    age(db, 60)
    dose(client, 50, flow=50)
    assert health(client)["pumped_ml"] == 50
    # Sticky: a second drain keeps the rise, and does not move the drop.
    risen, dropped = rise(db), drops(db)[-1]
    empty(client)
    assert origin(db) == (risen, "rise") and health(client)["pumped_ml"] == 50
    assert drops(db) == [None, dropped]
    age(db, 60)
    full(client)  # and a second untapped refill restarts it
    assert rise(db) > risen - 60 and origin(db) == (rise(db), "rise")
    assert health(client)["pumped_ml"] == 0
    # A rise in the tap's own second is the tap's: the human's word wins.
    empty(client)
    full(client)
    since = tap(client, db)
    assert rise(db) == since and origin(db) == (since, "tap")
    # A drop and a rise in one second are a float bouncing, not a refill:
    # the tap stays.
    age(db, 60)
    empty(client)
    full(client)
    assert rise(db) == drops(db)[-1] and origin(db) == (since - 60, "tap")


def test_a_rise_before_any_drop_after_the_tap_leaves_the_tap(client, db):
    """A 0 -> 1 with no drop after the tap is the tap's own refill arriving
    on the wire after a tap made at empty: the tap stays the origin, and
    the run it starts closes on the tap — the ordinary "tank empty, fill,
    tap" run, which the first wording lost (spec D3, D4, amended twice)."""
    full(client)
    empty(client)  # ran dry
    age(db, FLAP_WINDOW_S + 1)
    since = tap(client, db)  # the tap, with the float still saying empty
    assert refills(db) == [(0,)]
    full(client)  # the pour reaches the float
    assert rise(db) > since and drops(db) == [None]
    assert origin(db) == (since, "tap") and health(client)["pumped_ml"] == 0
    dose(client, 150, flow=140)
    assert health(client)["pumped_ml"] == 140
    ack(client, hand(client, 100), flow=90, float_ok=0)
    assert samples(db) == []  # one sighting of empty
    report(client, "c=0 ch0=1 float=0")  # the next agrees
    assert samples(db) == [(since, 230)]
    assert drops(db) == [word_since(db)]


def test_a_tap_between_the_two_sightings_of_empty_is_after_the_run(client, db):
    """The drop is confirmed a report after the word fell, and a person
    who saw the tank empty, filled it and tapped inside that beat made a
    tap the run did not start from: the drop and the sample are the
    earlier tap's, where the word fell, the new tap keeps NULL, and the
    pour reaching the float is not a rise past a drop — the new tap stays
    the origin, and its own run is the next sample (spec D2, D4, thrice)."""
    full(client)
    tap(client, db)
    dose(client, 200, flow=200)
    report(client, "c=0 ch0=1 float=0")  # the first sighting
    age(db, 30)
    tap(client, db)  # filled and tapped, the float still saying empty
    assert refills(db) == [(1,), (0,)]
    report(client, "c=0 ch0=1 float=0")  # the next agrees
    first, second = taps(db)
    assert drops(db) == [word_since(db), None] and word_since(db) < second
    assert samples(db) == [(first, 200)]
    full(client)  # the pour reaches the float
    assert rise(db) > second and origin(db) == (second, "tap")
    assert health(client)["pumped_ml"] == 0
    dose(client, 100, flow=100)
    empty(client)
    assert samples(db) == [(first, 200), (second, 100)]


def test_one_sighting_of_empty_between_two_of_full_moves_nothing(client, db):
    """The wire's word is one glitch to 0 by design (safety.cpp fails the
    word on any of three samples), so the tank is measured on the firm
    word — two consecutive reports that carry float= agreeing — and a
    slosh at report time neither closes the sample early nor hands the
    origin to its recovery: no drop, no sample, no rise, the counter
    where it was. Two sightings stamp the drop where the word fell and
    close the sample on the water counted at the second, the dose handed
    on the first included (spec D3, D4, thrice)."""
    full(client)
    since = tap(client, db)
    dose(client, 100, flow=90)
    risen = rise(db)
    assert firm(db) == 1
    report(client, "c=0 ch0=1 float=0")  # a slosh
    assert firm(db) == 1 and drops(db) == [None] and samples(db) == []
    report(client, "c=0 ch0=1")  # a report that says nothing sits between
    report(client, "c=0 ch0=1 float=1")  # and full again: no rise
    assert firm(db) == 1 and rise(db) == risen
    report(client, "c=0 ch0=1 float=1")  # the next agrees: no rise either
    assert firm(db) == 1 and rise(db) == risen
    assert origin(db) == (since, "tap") and health(client)["pumped_ml"] == 90
    dose(client, 50, flow=50)
    answer = post(client, "/command", "c=0 water=3 ml=40")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    handed = report(client, "c=0 ch0=1 float=0 pos=ok").text  # the first sighting
    assert f"cmd={cmd_id} water=3 ml=40" in handed
    assert firm(db) == 1 and drops(db) == [None] and samples(db) == []
    fell = word_since(db)
    # The next agrees, and acks the dose the first sighting handed.
    report(client, f"c=0 ch0=1 float=0 pos=ok ack={cmd_id} flow_ml=35")
    assert firm(db) == 0 and drops(db) == [fell]
    assert samples(db) == [(since, 90 + 50 + 35)]


def test_the_firm_words_clocks_are_where_the_word_moved(client, db):
    """The firm word's clocks — the drop on its tap, the rise — are the
    word's own: where it moved, not where the next report confirmed it.
    On a board the two agreeing reports are a beat apart, and the report
    that raises the word hands its queued dose with the move's clock — a
    clock set at the confirmation would put that dose before the rise and
    off the counter. The tests around this one confirm inside a second,
    where the two are one number; here a beat sits between, on the drop
    and on the rise. And one sighting moves neither clock: the rise the
    tap found stands until the next report agrees, and a rise stamped at
    the sighting would be the fall's clock, not the word's (spec D3, D4,
    thrice)."""
    full(client)
    tap(client, db)  # in the rise's second
    report(client, "c=0 ch0=1 float=0")  # the word fell here...
    age(db, 30)
    fell = word_since(db)
    assert firm(db) == 1 and drops(db) == [None] and rise(db) == taps(db)[0]
    report(client, "c=0 ch0=1 float=0")  # ...and is confirmed a beat later
    assert firm(db) == 0 and drops(db) == [fell] and rise(db) == taps(db)[0]
    report(client, "c=0 ch0=1 float=1")  # rose here...
    age(db, 30)
    rose = word_since(db)
    assert firm(db) == 0 and rise(db) == taps(db)[0]  # one sighting moves nothing
    assert origin(db) == (taps(db)[0], "tap")
    report(client, "c=0 ch0=1 float=1")  # ...and is confirmed a beat later
    assert firm(db) == 1 and rise(db) == rose and rose > drops(db)[0]
    assert origin(db) == (rose, "rise")


def test_a_slosh_after_an_untapped_refill_moves_neither_rise_nor_counter(client, db):
    """Once the origin is a rise — the tank ran down and someone refilled
    it without tapping — the raw word sloshing at the line is no edge of
    the firm word, and the counter must not move on it. Empty again, a
    0 -> 1 -> 0 is no rise: the origin stays the rise and the water since
    it stays on the counter, rather than restarting at the slosh with the
    run's water laundered. Full again, a 1 -> 0 -> 1 that the next report
    confirms is no rise either: the rise is where the firm word rose, not
    where a glitch recovered. The rise's guard is two conditions — the
    last word agreeing with this one, the firm word not already full —
    and each slosh gets past one of them alone (spec D3, thrice)."""
    full(client)
    tap(client, db)
    dose(client, 100, flow=100)
    empty(client)  # ran down: the tap's drop
    age(db, 60)
    full(client)  # refilled, untapped: the rise is the origin
    age(db, 60)
    dose(client, 80, flow=80)
    age(db, 60)
    risen = rise(db)
    assert origin(db) == (risen, "rise") and health(client)["pumped_ml"] == 80
    empty(client)  # a second drain keeps the rise
    assert rise(db) == risen and origin(db) == (risen, "rise")
    report(client, "c=0 ch0=1 float=1")  # a slosh at the line...
    report(client, "c=0 ch0=1 float=0")  # ...and back: the firm word never left 0
    assert firm(db) == 0 and rise(db) == risen and origin(db) == (risen, "rise")
    assert health(client)["pumped_ml"] == 80
    age(db, 60)
    full(client)  # a second untapped refill is a rise, and restarts the counter
    assert rise(db) > drops(db)[0] and origin(db) == (rise(db), "rise")
    assert health(client)["pumped_ml"] == 0
    age(db, 60)
    dose(client, 30, flow=30)
    age(db, 60)
    risen = rise(db)
    assert origin(db) == (risen, "rise") and health(client)["pumped_ml"] == 30
    report(client, "c=0 ch0=1 float=0")  # a slosh...
    report(client, "c=0 ch0=1 float=1")  # ...and back...
    report(client, "c=0 ch0=1 float=1")  # ...which the next agrees with: no rise
    assert firm(db) == 1 and rise(db) == risen and origin(db) == (risen, "rise")
    assert health(client)["pumped_ml"] == 30


def test_a_forced_zero_is_not_a_drop(client, db):
    """A contra forces the board's word to 0 on every report until
    `clear contra` is typed: "float OK, zero pulses", a fault and not the
    tank. Stamped as the tap's drop, the word coming back after `clear
    contra` read as a rise past it — an untapped refill — and the counter
    restarted with the run's water laundered and its sample lost. So
    neither a report carrying ch207=1 nor one under the standing latch
    stamps or closes anything: the tap stays the origin through the
    contra, the resume and the clear, and the run's later real drain
    closes its sample on all the water since the tap (spec §1, D3, D4,
    thrice)."""
    full(client)
    since = tap(client, db)
    dose(client, 100, flow=100)
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")  # the forced 0...
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")  # ...on every report
    assert health(client)["latched"]["reason"] == "contra"
    assert firm(db) == 0  # the word is what the board says, forced or not
    assert drops(db) == [None] and samples(db) == []
    assert post(client, "/resume", "c=0").status_code == 200
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0")  # clear contra was typed
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0")
    assert rise(db) > since and drops(db) == [None]
    assert origin(db) == (since, "tap") and health(client)["pumped_ml"] == 100
    dose(client, 120, flow=110)
    empty(client)  # the real drain
    assert drops(db) == [word_since(db)]
    assert samples(db) == [(since, 210)]


def test_clear_contra_after_a_tap_at_the_forced_zero_leaves_the_tap(client, db):
    """A contra forces the board's word to 0; the human taps, resumes and
    types `clear contra`, and the word comes back to 1. That rise had no
    drop after the tap before it — the forced 0 stamped none on the tap
    before either — so the tap stays the origin and the run it starts is
    a sample (spec §1, D3 amended twice, then thrice)."""
    full(client)
    tap(client, db)
    dose(client, 100, flow=100)
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")  # the forced 0
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")
    assert health(client)["latched"]["reason"] == "contra"
    assert samples(db) == [] and drops(db) == [None]
    age(db, 60)
    since = tap(client, db)  # a look at the tank, at the forced 0
    assert refills(db)[-1] == (0,)
    assert post(client, "/resume", "c=0").status_code == 200
    report(client, "c=0 ch0=1 float=1 pos=ok")  # clear contra on the board
    report(client, "c=0 ch0=1 float=1 pos=ok")
    assert rise(db) > since and drops(db) == [None, None]
    assert origin(db) == (since, "tap")
    dose(client, 120, flow=110)
    empty(client)
    assert samples(db) == [(since, 110)]


def test_a_report_without_float_moves_no_rise(client, db):
    """A report that says nothing about the float neither rises nor
    falls: the rise the float had stands through it, and none is invented
    when it says the same word again — the word's own clock, not
    float_since, which such a report restarts (spec D3, amended)."""
    full(client)
    since = tap(client, db)
    dose(client, 150, flow=150)
    empty(client)  # ran down...
    age(db, 60)
    full(client)  # ...and refilled, untapped
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
    """The report that first says full after empty is the rise, once the
    next agrees, and it hands whatever was queued with its own clock: that
    dose pumps after it was handed, from the refilled tank. A counter
    strict about the origin's second lost it for ever, and so would a
    rise stamped where it was confirmed, a beat later, rather than where
    the word rose (spec D3, amended, then thrice)."""
    full(client)
    tap(client, db)
    empty(client)  # ran down
    age(db, 60)
    answer = post(client, "/command", "c=0 water=3 ml=80")  # typed while empty
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    # Refilled untapped: this one report is the rise and hands the dose.
    handed = report(client, "c=0 ch0=1 float=1 pos=ok").text
    assert f"cmd={cmd_id} water=3 ml=80" in handed
    assert origin(db)[1] == "tap"  # one sighting of full is not yet the rise
    age(db, 60)  # a beat later...
    report(client, f"c=0 ch0=1 float=1 pos=ok ack={cmd_id} flow_ml=80")  # ...it agrees
    risen = rise(db)
    assert risen == word_since(db) and origin(db) == (risen, "rise")
    assert run_sql(db, "SELECT sent_ts FROM commands WHERE id = ?", cmd_id) == [
        (risen,)
    ]
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
    full(client)
    tap(client, db)
    dose(client, 100, flow=90)
    answer = post(client, "/command", "c=0 stop=1")
    assert answer.status_code == 200, answer.text
    stop_id = int(answer.text.strip().removeprefix("cmd="))
    assert f"cmd={stop_id} stop=1" in report(client, "c=0 ch0=1 float=1 pos=ok").text
    ack(client, stop_id, flow=60, float_ok=0)  # and the float drops on that report
    report(client, "c=0 ch0=1 float=0")  # and says so again
    assert run_sql(
        db, "SELECT kind, ml, flow_ml FROM commands WHERE id = ?", stop_id
    ) == [("stop", None, 60)]
    assert health(client)["pumped_ml"] == 90
    assert samples(db) == [(taps(db)[0], 90)]


# --------------------------------------------------------------------------- #
# Learning a sample (spec D4)
# --------------------------------------------------------------------------- #


def test_the_float_going_empty_closes_one_sample_per_tap(client, db):
    full(client)
    tap(client, db)
    dose(client, 100, flow=90)
    # The dose that drains the tank acks on the report that first says
    # empty, and that count is part of the run the next report closes.
    ack(client, hand(client, 100), flow=80, float_ok=0)
    assert samples(db) == []
    report(client, "c=0 ch0=1 float=0")
    assert samples(db) == [(taps(db)[0], 170)]
    report(client, "c=0 ch0=1 float=0")  # still empty: nothing new
    full(client)  # bouncing at the line, no tap
    dose(client, 100, flow=100)  # and watered while it says full
    empty(client)  # the first crossing stands, not 270
    assert samples(db) == [(taps(db)[0], 170)]
    # A tap, nothing pumped, and the float goes empty: not a measurement,
    # but the tap's first drop all the same.
    age(db, 60)
    full(client)
    tap(client, db)
    assert drops(db)[-1] is None
    empty(client)
    assert samples(db) == [(taps(db)[0], 170)]
    assert drops(db)[-1] == word_since(db)
    # Water flows and the float goes empty again on the same tap — but it
    # rose after that tap's drop, so the tank was refilled by someone who
    # did not say so, and how full it was is anybody's guess: a second
    # drain stores nothing (spec D4, amended twice).
    age(db, 60)
    full(client)
    assert origin(db)[1] == "rise"
    dose(client, 200, flow=210)
    empty(client)
    assert samples(db) == [(taps(db)[0], 170)]
    # Said so: the next tap's run is a sample.
    age(db, 60)
    full(client)
    tap(client, db)
    dose(client, 200, flow=210)
    empty(client)
    first, _second, third = taps(db)
    assert samples(db) == [(first, 170), (third, 210)]


def test_a_bounce_while_the_firm_word_is_empty_is_no_drop(client, db):
    """Once the firm word is at 0, the raw word bouncing 0 -> 1 -> 0 moves
    the word's clock and nothing else: the report that agrees with the
    bounce's fall is not the firm word going 1 -> 0 — it never left 0 —
    so it stamps no tap and closes nothing. The gate is the firm word,
    not the last report having said 0 as well: the bounce's fall is later
    than a tap made at empty, and that gate alone hands the tap a drop it
    never had and closes its run, still open, on the noise — the true
    drain then stores nothing, being a second drain (spec D4, thrice)."""
    full(client)
    tap(client, db)
    dose(client, 100, flow=100)
    empty(client)  # ran down: the firm word's drop, and the tap's sample
    assert firm(db) == 0 and drops(db) == [word_since(db)]
    assert samples(db) == [(taps(db)[0], 100)]
    report(client, "c=0 ch0=1 float=0")  # still empty: nothing moves
    assert drops(db) == [word_since(db)] and samples(db) == [(taps(db)[0], 100)]
    age(db, 60)
    tap(client, db)  # filled and tapped, the float still saying empty
    first, second = taps(db)
    dropped = drops(db)[0]
    assert refills(db) == [(1,), (0,)] and origin(db) == (second, "tap")
    answer = post(client, "/command", "c=0 water=3 ml=77")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    handed = report(client, "c=0 ch0=1 float=0 pos=ok").text  # the pour not up yet
    assert f"cmd={cmd_id} water=3 ml=77" in handed
    report(client, f"c=0 ch0=1 float=0 pos=ok ack={cmd_id} flow_ml=77")
    assert health(client)["pumped_ml"] == 77
    report(client, "c=0 ch0=1 float=1")  # a slosh...
    report(client, "c=0 ch0=1 float=0")  # ...and back: the word's clock passes the tap
    assert word_since(db) > second and firm(db) == 0
    report(client, "c=0 ch0=1 float=0")  # the next agrees with the bounce's fall
    assert drops(db) == [dropped, None] and samples(db) == [(first, 100)]
    assert origin(db) == (second, "tap") and health(client)["pumped_ml"] == 77
    # The pour reaches the float, more water goes, and the tank runs down:
    # the tap's first drop, and its run is all the water since the tap.
    age(db, 60)
    full(client)
    dose(client, 50, flow=50)
    empty(client)
    first, second = taps(db)
    assert drops(db)[1] == word_since(db)
    assert samples(db) == [(first, 100), (second, 127)]


def test_a_contra_report_or_a_standing_latch_closes_no_sample(client, db):
    """ch207=1 is "float OK, zero pulses", and zero pulses is a dead meter,
    a kinked tube, a dead pump or 12 V absent as often as anything about
    the tank: a fault, not a measurement (spec §1, D4 amended)."""
    full(client)
    tap(client, db)
    cmd_id = hand(client, 100)
    report(client, f"c=0 ch0=1 float=0 pos=ok ack={cmd_id} flow_ml=90 ch207=1")
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")  # and keeps saying so
    assert health(client)["latched"]["reason"] == "contra"
    assert health(client)["pumped_ml"] == 90  # acked water is a fact
    assert samples(db) == [] and drops(db) == [None]  # a forced 0 is no drop
    # A latch standing from before the edge — here the reset with the pump
    # running, the float saying full throughout — closes none either, and
    # stamps no drop: the word under a latch is not the tank's.
    assert post(client, "/resume", "c=0").status_code == 200
    full(client)
    tap(client, db)
    dose(client, 100, flow=90)
    report(client, "c=0 ch0=1 float=1 err=resetmid")
    empty(client)
    assert health(client)["latched"]["reason"] == "resetmid"
    assert samples(db) == [] and drops(db) == [None, None]
    # Resumed, a tap and a run: learning again.
    assert post(client, "/resume", "c=0").status_code == 200
    age(db, 60)
    full(client)
    tap(client, db)
    dose(client, 100, flow=90)
    empty(client)
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
    """The edge is read off the board's last real word: a report that omits
    float= neither hides the crossing after it nor is one itself. Were
    silence an edge, a heartbeat mid-dose would close the sample at the
    water so far, and the true crossing's count would be dropped on the
    tap's key (spec D4)."""
    full(client)
    tap(client, db)
    dose(client, 100, flow=90)
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    # A report that says nothing about the float hands the dose...
    handed = report(client, "c=0 ch0=1 pos=ok").text
    assert f"cmd={cmd_id} water=3 ml=50" in handed
    assert run_sql(db, "SELECT float_ok, float_word, float_firm FROM status") == [
        (None, 1, 1)
    ]
    assert samples(db) == []  # ...and closes nothing: silence is not empty
    # Full, silent, empty, silent, empty is an edge — the two sightings
    # are the last two reports that carried float=, whatever said nothing
    # between them — and the run it closes is the whole run: the dose in
    # flight through the silence acks on the first sighting.
    ack(client, cmd_id, flow=40, float_ok=0)
    report(client, "c=0 ch0=1 pos=ok")
    assert samples(db) == []
    report(client, "c=0 ch0=1 float=0 pos=ok")
    assert samples(db) == [(taps(db)[0], 130)]
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
    assert samples(db) == [(first, 130)]


def test_a_retired_board_learns_nothing(client, db):
    full(client)
    tap(client, db)
    cmd_id = hand(client, 100)  # with the board when it is retired
    assert post(client, "/controller", "c=0 retired=1").status_code == 200
    ack(client, cmd_id, flow=90, float_ok=0)  # the ack lands, the float drops
    report(client, "c=0 ch0=1 float=0")  # and says so again
    assert health(client)["pumped_ml"] == 90  # acked water is a fact
    assert samples(db) == []  # a measurement is learning
    assert drops(db) == [word_since(db)]  # the drop is a fact too
    # Back in service, the float has risen since that tap; a new tap and a
    # new run are what it learns from.
    assert post(client, "/controller", "c=0 retired=0").status_code == 200
    age(db, 60)
    full(client)
    tap(client, db)
    dose(client, 100, flow=90)
    empty(client)
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
    full(client)
    entry = health(client)
    assert entry["tank_ml"] is None
    assert entry["tank_samples"] == 0
    assert entry["pumped_ml"] == 0
    tap(client, db)
    dose(client, 200, flow=180)
    empty(client)
    full(client)
    entry = health(client)
    assert (entry["tank_ml"], entry["tank_samples"]) == (None, 1)
    age(db, 60)
    tap(client, db)
    dose(client, 200, flow=200)
    dose(client, 50, flow=40)
    empty(client)
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
    assert drops(db) == [None, None]  # and has had no drop


OLD_STATUS = """
CREATE TABLE status (
  controller INTEGER PRIMARY KEY, ts INTEGER NOT NULL, float_ok INTEGER,
  float_since INTEGER, pos TEXT, pos_since INTEGER, float_seen INTEGER,
  pos_seen INTEGER, float_bad INTEGER, float_bad_prev INTEGER,
  pos_bad INTEGER, pos_bad_prev INTEGER, err TEXT, err_ts INTEGER,
  latched_ts INTEGER, latch_reason TEXT, pos_ok_seen INTEGER);
"""


def test_an_existing_database_carries_the_floats_last_word_at_startup(db):
    # The 0.18.0 shape of status: float_ok, no float_word. A tank sitting
    # at full through the upgrade closes its sample on the first empty —
    # from a tap made after it: the one from before saw nothing (it gets
    # NULL) and starts no counter (spec D3).
    with sqlite3.connect(db) as con:
        con.executescript(
            OLD_STATUS
            + """
            INSERT INTO status (controller, ts, float_ok, float_since) VALUES (0, 5, 1, 5);
            CREATE TABLE refills (ts INTEGER NOT NULL, controller INTEGER NOT NULL);
            INSERT INTO refills VALUES (10, 0);
            """
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    # The word, its clock and its rise, carried from float_ok and
    # float_since, and the firm word from the word: the rise the float
    # had before the upgrade is where it was, the one report the upgrade
    # has to go on is taken at its word, and no report has carried ch207
    # yet.
    assert run_sql(
        db,
        "SELECT float_word, float_word_since, float_rise, contra, float_firm "
        "FROM status",
    ) == [(1, 5, 5, 0, 1)]
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
    empty(client)
    assert samples(db) == [(since, 100)]


def test_the_carried_clocks_come_only_with_the_word(db):
    """A last pre-upgrade report that omitted float= left float_ok NULL and
    float_since restarted at the omission: the word is not carried, so
    neither is a clock for it — a clock without a word would read as a
    float that has not moved since before any tap. A word of empty brings
    its clock and no rise (float_since is its fall); a word of full brings
    both. The firm word is the word, under the same gate (spec D3,
    amended, then thrice)."""
    with sqlite3.connect(db) as con:
        con.executescript(
            OLD_STATUS
            + """
            INSERT INTO status (controller, ts, float_ok, float_since)
            VALUES (0, 9, NULL, 7), (1, 9, 0, 6), (2, 9, 1, 5);
            """
        )
    TestClient(create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900))
    assert run_sql(
        db,
        "SELECT controller, float_word, float_word_since, float_rise, float_firm "
        "FROM status ORDER BY controller",
    ) == [
        (0, None, None, None, None),
        (1, 0, 6, None, 0),
        (2, 1, 5, 5, 1),
    ]


# The shape between the two amendments: the tap already snapshots the
# float and the word has its clock, but no tap has a drop yet.
MID_SHAPE = """
CREATE TABLE status (
  controller INTEGER PRIMARY KEY, ts INTEGER NOT NULL, float_ok INTEGER,
  float_since INTEGER, pos TEXT, pos_since INTEGER, float_seen INTEGER,
  pos_seen INTEGER, float_bad INTEGER, float_bad_prev INTEGER,
  pos_bad INTEGER, pos_bad_prev INTEGER, err TEXT, err_ts INTEGER,
  latched_ts INTEGER, latch_reason TEXT, pos_ok_seen INTEGER,
  float_word INTEGER, float_word_since INTEGER);
CREATE TABLE refills (ts INTEGER NOT NULL, controller INTEGER NOT NULL,
  float_ok INTEGER);
"""


def test_a_tank_already_empty_at_the_upgrade_carries_its_drop(db):
    """A tank that ran down before the upgrade is one that ran down: the
    latest tap that saw the float gets the word's fall as its drop_ts
    when the word is 0 and fell after the tap — or in its second, the tap
    having seen it full, since a tap after the fall snapshots the 0 — so
    the rise after the next untapped refill restarts the counter instead
    of the board being paged stuck at full on every dose since a tap it
    demonstrably ran down from. A word of full hides whatever fall
    preceded it: that tap keeps NULL, as with no fall (spec D2, amended)."""
    with sqlite3.connect(db) as con:
        con.executescript(MID_SHAPE)
        con.executemany(
            "INSERT INTO status (controller, ts, float_ok, float_since, "
            "float_word, float_word_since) VALUES (?, 50, ?, ?, ?, ?)",
            [
                (0, 0, 30, 0, 30),  # tapped at full, ran down: carried
                (1, 0, 5, 0, 5),  # tapped at empty, never moved: the fall came first
                (2, 0, 40, 0, 40),  # tapped at empty, rose and fell: carried
                (3, 1, 30, 1, 30),  # full now: whatever fell is hidden
                (4, 0, 10, 0, 10),  # fell in the tap's second, tap saw full: carried
                (5, 0, 10, 0, 10),  # fell in the tap's second, saw empty: fall first
                (6, 0, 30, 0, 30),  # latest tap saw nothing: the one before is the base
            ],
        )
        con.executemany(
            "INSERT INTO refills VALUES (?, ?, ?)",
            [
                (10, 0, 1),
                (10, 1, 0),
                (10, 2, 0),
                (10, 3, 1),
                (10, 4, 1),
                (10, 5, 0),
                (10, 6, 1),
                (20, 6, None),
            ],
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    assert run_sql(
        db, "SELECT controller, ts, drop_ts FROM refills ORDER BY controller, ts"
    ) == [
        (0, 10, 30),
        (1, 10, None),
        (2, 10, 40),
        (3, 10, None),
        (4, 10, 10),
        (5, 10, None),
        (6, 10, 30),
        (6, 20, None),
    ]
    # The firm word is the word the row had, this shape having no other.
    assert run_sql(
        db,
        "SELECT controller, float_firm FROM status "
        "WHERE controller IN (0, 3) ORDER BY controller",
    ) == [(0, 0), (3, 1)]
    # Board 0 pumped a tank's worth between its tap and the fall — the run
    # the old code closed — and is now refilled by someone who forgot to
    # tap: the rise is the origin, nothing is on the counter, and the
    # board is not presumed stuck.
    run_sql(
        db,
        "INSERT INTO tank_samples (ts, controller, refill_ts, ml) "
        "VALUES (2, 0, 1, 200), (30, 0, 10, 250)",
    )
    run_sql(
        db,
        "INSERT INTO commands (created_ts, controller, kind, outlet, ml, cap_s, "
        "state, source, sent_ts, acked_ts, flow_ml) "
        "VALUES (20, 0, 'water', 3, 250, 30, 'acked', 'manual', 20, 21, 250)",
    )
    full(client)
    assert origin(db) == (rise(db), "rise")
    entry = health(client)
    assert (entry["tank_ml"], entry["pumped_ml"], entry["over"]) == (225, 0, 0)
    # Its second drain is the untapped refill's, not the tap's: the tap
    # keeps the drop it was carried, and the rise stays the origin.
    empty(client)
    assert run_sql(db, "SELECT drop_ts FROM refills WHERE controller = 0") == [(30,)]
    assert origin(db) == (rise(db), "rise")
    assert samples(db) == [(1, 200), (10, 250)]


# --------------------------------------------------------------------------- #
# Every sample is announced (spec D8)
# --------------------------------------------------------------------------- #


def run_the_tank_down(app, client, db, ml):
    """One run, a flap window after the last: the float saying full again
    (before the tap — a rise after it would be the origin, and no sample
    closes on a rise), a tap, `ml` through the meter in doses the board
    accepts, the float going empty and still empty a flap window on, and
    a tick. Returns the tap's ts as it stood at the tick."""
    age(db, FLAP_WINDOW_S + 1)
    full(client)
    tap(client, db)
    while ml:
        part = min(ml, MAX_DOSE_ML)
        dose(client, part, flow=part)
        ml -= part
    report(client, "c=0 ch0=1 float=0")
    still_empty(client, db)
    tick(app)
    return taps(db)[-1]


def test_every_sample_is_announced_once(app, client, db, sent):
    full(client)
    tap(client, db)
    dose(client, 200, flow=190)
    report(client, "c=0 ch0=1 float=0")
    still_empty(client, db)
    first = taps(db)[-1]
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
    full(client)
    tap(client, db)
    report(client, "c=0 ch0=1 float=0")
    still_empty(client, db)
    tick(app)
    assert len(sent) == 2


def test_two_runs_waiting_on_one_tick_are_each_judged_as_they_closed(
    app, client, db, sent
):
    """A tick that finds two samples waiting (the ticker was down, or a
    send failed) judges the first against what the tank knew when it
    closed — nothing — not against the second, which had not happened
    yet (spec D8)."""
    full(client)
    tap(client, db)
    dose(client, 200, flow=200)
    report(client, "c=0 ch0=1 float=0")
    still_empty(client, db)
    full(client)
    tap(client, db)
    dose(client, 250, flow=250)
    dose(client, 150, flow=150)
    report(client, "c=0 ch0=1 float=0")
    still_empty(client, db)
    tick(app)
    first, second = taps(db)
    assert keys(sent) == [f"tank:0:{first}", f"tank:0:{second}"]
    assert sent[0].message.endswith("(tank size learning, 1 of 2)")
    assert sent[1].message.endswith("(tank 300 ml over 2 samples)")


def test_a_sample_off_the_size_it_knew_is_a_warning(app, client, db, sent):
    full(client)
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
    full(client)
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
    full(client)
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
    full(client)
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
    full(client)
    tap(client, db)
    dose(client, 200, flow=200)
    report(client, "c=0 ch0=1 float=0")
    still_empty(client, db)
    since = taps(db)[-1]
    assert post(client, "/controller", "c=0 retired=1").status_code == 200
    tick(app)
    assert keys(sent) == []
    # Skipped, not forgotten: back in service, the run is announced.
    assert post(client, "/controller", "c=0 retired=0").status_code == 200
    tick(app)
    assert keys(sent) == [f"tank:0:{since}"]
