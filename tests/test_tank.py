"""Trust the tank: err=, the durable latch, refills, the float judged
against the tank's size, retirement, and the rules corrections that came
with them."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import butler
from butler import (
    FLAP_WINDOW_S,
    PERSIST_S,
    REALERT_FLOOR_S,
    TANK_SAMPLES_TO_ARM,
    TANK_TOLERANCE_PCT,
    create_app,
    parse_report,
)

TOKEN = "test-token"
DRY = 11000  # pct 12 with make_pot's calibration
WET = 8000  # pct 50


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


# --------------------------------------------------------------------------- #
# err= and pos_ok_seen (spec D8, D11's column)
# --------------------------------------------------------------------------- #


def test_err_is_parsed_once_and_as_a_short_token():
    assert parse_report("c=0 ch0=1 err=contra").err == "contra"
    assert parse_report("c=0 ch0=1").err is None
    with pytest.raises(ValueError, match="err= given twice"):
        parse_report("c=0 ch0=1 err=range err=heap")
    with pytest.raises(ValueError, match="err="):
        parse_report("c=0 ch0=1 err=Contra")
    with pytest.raises(ValueError, match="err="):
        parse_report("c=0 ch0=1 err=" + "x" * 17)


def test_the_last_err_is_kept_until_the_board_sends_another(client, db):
    report(client, "c=0 ch0=1 err=heap")
    first = health(client)
    assert first["err"] == "heap" and first["err_ts"] > 0
    # The reports below land in the same second as the first, so a stamp
    # rewritten on every report would equal the one that should have stayed
    # put. Push it back a minute first, so kept and rewritten can differ.
    run_sql(db, "UPDATE status SET err_ts = err_ts - 60")
    stamp = first["err_ts"] - 60
    report(client, "c=0 ch0=1")
    kept = health(client)
    assert kept["err"] == "heap" and kept["err_ts"] == stamp
    report(client, "c=0 ch0=1 err=range")
    replaced = health(client)
    assert replaced["err"] == "range" and replaced["err_ts"] > stamp


def test_pos_ok_seen_remembers_that_the_board_once_knew_its_position(client, db):
    report(client, "c=0 ch0=1 pos=unknown")
    assert health(client)["pos_ok_seen"] is None
    report(client, "c=0 ch0=1 pos=ok")
    assert health(client)["pos_ok_seen"] > 0
    # Same second as the report above: backdate the stamp so "frozen at the
    # last pos=ok" and "refreshed by any pos=" give different numbers.
    run_sql(db, "UPDATE status SET pos_ok_seen = pos_ok_seen - 60")
    stamp = health(client)["pos_ok_seen"]
    report(client, "c=0 ch0=1 pos=unknown")
    assert health(client)["pos_ok_seen"] == stamp
    report(client, "c=0 ch0=1 pos=ok")
    assert health(client)["pos_ok_seen"] > stamp


def test_health_carries_the_new_fields_with_their_defaults(client):
    report(client, "c=0 ch0=1")
    entry = health(client)
    for key in ("err", "err_ts", "pos_ok_seen", "latched", "last_refill"):
        assert entry[key] is None, key
    assert entry["retired"] == 0 and entry["over"] == 0


def test_an_old_database_grows_the_columns_at_startup(db):
    # The shape 0.17.0 left behind for the two tables that change.
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE controllers (
              controller INTEGER PRIMARY KEY, last_seen INTEGER NOT NULL, next_s INTEGER);
            CREATE TABLE status (
              controller INTEGER PRIMARY KEY, ts INTEGER NOT NULL, float_ok INTEGER,
              float_since INTEGER, pos TEXT, pos_since INTEGER, float_seen INTEGER,
              pos_seen INTEGER, float_bad INTEGER, float_bad_prev INTEGER,
              pos_bad INTEGER, pos_bad_prev INTEGER);
            INSERT INTO controllers VALUES (0, 5, NULL);
            INSERT INTO status VALUES (0, 5, 1, 5, 'ok', 5, 5, 5, NULL, NULL, NULL, NULL);
            """
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    entry = health(client)
    assert entry["retired"] == 0 and entry["latched"] is None and entry["err"] is None
    report(client, "c=0 ch0=1 err=noflow")
    assert health(client)["err"] == "noflow"


# --------------------------------------------------------------------------- #
# The dose ceiling is the board's (spec D10)
# --------------------------------------------------------------------------- #


def test_a_dose_above_the_rig_ceiling_is_refused_before_it_is_queued(client):
    assert butler.MAX_DOSE_ML == 250
    assert post(client, "/command", "c=0 water=3 ml=250").status_code == 200
    answer = post(client, "/command", "c=1 water=3 ml=251")
    assert answer.status_code == 400 and "ml=" in answer.text


def test_a_pot_cannot_be_saved_with_a_dose_the_board_would_refuse(client):
    answer = post(client, "/pot", "name=basil dose_ml=251")
    assert answer.status_code == 400 and "dose_ml" in answer.text
    assert post(client, "/pot", "name=basil dose_ml=250").status_code == 200


# --------------------------------------------------------------------------- #
# The daily cap counts water the board acknowledged (spec D9)
# --------------------------------------------------------------------------- #


def make_pot(client, **over):
    fields = {
        "name": "basil",
        "controller": 0,
        "channel": 0,
        "outlet": 3,
        "dry_raw": 12000,
        "wet_raw": 4000,
        "target_low_pct": 30,
        "target_high_pct": 60,
        "dose_ml": 100,
        "mode": "auto",
    } | over
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    answer = post(client, "/pot", body)
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def dry_reports(client, n=5, extra=""):
    """n dry reports with the safety fields the rules need; no t=, so none
    is a retry of another. Returns the last response text."""
    text = ""
    for _ in range(n):
        text = report(client, f"c=0 ch0={DRY} float=1 pos=ok {extra}".strip()).text
    return text


def commands(db):
    return run_sql(db, "SELECT id, state, flow_ml FROM commands ORDER BY id")


def test_a_dose_the_board_never_acknowledged_is_not_charged_to_the_day(client, db):
    make_pot(client, cooldown_h=0, daily_cap_ml=150)
    assert "cmd=1 water=3 ml=100" in dry_reports(client)  # handed on the fifth
    # No ack: cmd 1 expires, never acked. Before this change its phantom
    # 100 ml counted, 100 + 100 > 150, and the pot went thirsty for the day
    # on a response that never arrived. The window is already five dry
    # readings deep, so the rules take the freed slot on this very report.
    unacked = report(client, f"c=0 ch0={DRY} float=1 pos=ok")
    assert "cmd=2 water=3 ml=100" in unacked.text
    assert commands(db) == [(1, "expired", None), (2, "sent", None)]
    report(client, f"c=0 ch0={DRY} float=1 pos=ok ack=2 flow_ml=100")
    dry_reports(client)
    assert [row[0] for row in commands(db)] == [1, 2]  # 100 acked + 100 > 150: the cap holds


# --------------------------------------------------------------------------- #
# pos: waits for a board that has ever known its position (spec D11)
# --------------------------------------------------------------------------- #


def test_no_pos_page_before_a_board_has_ever_said_pos_ok(app, client, sent):
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    tick(app)
    assert "pos:0" not in keys(sent)  # PB_REPORT_POS_UNKNOWN=1 ships this way
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    tick(app)
    assert "pos:0" in keys(sent)


# --------------------------------------------------------------------------- #
# Retirement (spec D7)
# --------------------------------------------------------------------------- #


def age_controller(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE controllers SET last_seen = last_seen - ?", (seconds,))


def test_parse_controller_wants_both_fields_once():
    assert butler.parse_controller("c=0 retired=1") == (0, 1)
    assert butler.parse_controller("retired=0 c=9 later=1") == (9, 0)
    for body in ("retired=1", "c=0", "c=0 retired=2", "c=0 c=0 retired=1"):
        with pytest.raises(ValueError):
            butler.parse_controller(body)


def test_a_retired_board_is_quiet_and_never_waters(app, client, db, sent):
    make_pot(client, cooldown_h=0)
    report(client, "c=0 ch0=1")
    answer = post(client, "/controller", "c=0 retired=1")
    assert answer.status_code == 200 and answer.text == "controller=0 retired=1\n"
    assert health(client)["retired"] == 1
    age_controller(db, 7200)
    app.state.observed["since"] = 0
    tick(app)
    assert "silent:0" not in keys(sent)
    dry_reports(client)
    assert commands(db) == []  # readings landed, nothing queued
    assert post(client, "/controller", "c=0 retired=0").text == "controller=0 retired=0\n"
    age_controller(db, 7200)
    tick(app)
    assert "silent:0" in keys(sent)


def test_retiring_a_paged_board_clears_its_silence_page(app, client, db, sent):
    report(client, "c=0 ch0=1")
    age_controller(db, 7200)
    app.state.observed["since"] = 0
    tick(app)
    assert keys(sent) == ["silent:0"]
    post(client, "/controller", "c=0 retired=1")
    assert run_sql(db, "SELECT cleared_ts IS NOT NULL FROM alerts WHERE key = 'silent:0'") == [(1,)]
    assert client.get("/health").json()["alerts"] == []


def test_retiring_a_board_clears_its_sensor_page_too(app, client, db, sent):
    make_pot(client, mode="manual")
    report(client, "c=0 ch0=1")
    app.state.observed["since"] = 0
    run_sql(db, "UPDATE readings SET ts = ts - 700")  # the wire comes loose
    tick(app)
    assert keys(sent) == ["sensor:0:0"]
    post(client, "/controller", "c=0 retired=1")
    assert run_sql(db, "SELECT cleared_ts IS NOT NULL FROM alerts WHERE key = 'sensor:0:0'") == [(1,)]
    assert client.get("/health").json()["alerts"] == []
    tick(app)
    assert keys(sent) == ["sensor:0:0"]  # neither cleared aloud nor raised again


def raise_every_page_a_board_can_earn(app, client, db, sent):
    """The six pages the ticker raises for a board's own condition: an
    empty tank and a lost manifold twice inside the flap window, a float
    still saying empty in a report its minutes after a refill, then the
    contradiction latch and both safety fields vanishing for PERSIST_S.
    Two ticks, because the stuck float waits behind the latch and the last
    two want float= gone. (`over:` is the one page not here: it wants the
    float saying full.)"""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=unknown")
    tap(client, db)  # with the float saying empty
    age(db, PERSIST_S)
    report(client, "c=0 ch0=1 float=0 pos=unknown")  # its minutes on, still empty
    tick(app)
    assert sorted(keys(sent)) == ["float:0", "pos:0", "stale:0"]
    report(client, "c=0 ch0=1 float=0 pos=unknown ch207=1")
    report(client, "c=0 ch0=1")
    run_sql(
        db,
        "UPDATE status SET float_since = float_since - ?, pos_since = pos_since - ?",
        PERSIST_S,
        PERSIST_S,
    )
    tick(app)
    assert sorted(keys(sent)) == [
        "fields:float:0",
        "fields:pos:0",
        "float:0",
        "latch:0",
        "pos:0",
        "stale:0",
    ]


def test_a_retired_board_pages_nothing_whatever_its_reports_say(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1 pos=ok")
    post(client, "/controller", "c=0 retired=1")
    # Everything a live board would page for: the tank empty twice, the
    # manifold lost twice, a float still saying empty its minutes after a
    # refill, and the contradiction latch.
    report(client, "c=0 ch0=1 float=0 pos=unknown")
    tap(client, db)
    age(db, PERSIST_S)
    report(client, "c=0 ch0=1 float=0 pos=unknown ch207=1")
    tick(app)
    assert keys(sent) == []
    assert health(client)["latched"]["reason"] == "contra"  # the report landed
    assert health(client)["float"] == 0
    # Back in service, the board's standing trouble is heard at once — the
    # stuck float excepted, which waits behind the latch and behind the
    # board's own ch207 (spec D7).
    post(client, "/controller", "c=0 retired=0")
    tick(app)
    assert sorted(keys(sent)) == ["float:0", "latch:0", "pos:0"]
    assert post(client, "/resume", "c=0").status_code == 200
    tick(app)
    assert sorted(keys(sent)) == ["float:0", "latch:0", "pos:0"]
    report(client, "c=0 ch0=1 float=0 pos=unknown")  # clear contra was typed
    tick(app)
    assert sorted(keys(sent)) == ["float:0", "latch:0", "pos:0", "stale:0"]


def test_retiring_a_board_clears_every_page_that_stood_for_it(app, client, db, sent):
    raise_every_page_a_board_can_earn(app, client, db, sent)
    post(client, "/controller", "c=0 retired=1")
    assert client.get("/health").json()["alerts"] == []
    assert run_sql(db, "SELECT COUNT(*) FROM alerts WHERE cleared_ts IS NULL AND key NOT LIKE 'meta:%'") == [(0,)]
    assert health(client)["latched"]["reason"] == "contra"  # the page went, the row stays
    tick(app)
    assert len(sent) == 6  # neither cleared aloud nor raised again


def test_a_retired_board_is_handed_no_water(client, db):
    make_pot(client, mode="learning", cooldown_h=0)
    dry_reports(client)  # cmd 1: the rules' proposal, waiting on a human
    assert post(client, "/command", "c=0 water=3 ml=50").text == "cmd=2\n"
    post(client, "/controller", "c=0 retired=1")
    # Both were waiting to pour from a board nobody wants water from.
    assert commands(db) == [(1, "expired", None), (2, "expired", None)]
    assert post(client, "/approve", "cmd=1").status_code == 400
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text == "refused: board 0 is retired: un-retire it first\n"
    assert dry_reports(client) == "next=60\n"  # handed nothing, proposed nothing
    assert post(client, "/command", "c=0 stop=1").text == "cmd=3\n"  # the safe direction
    assert "cmd=3 stop=1" in report(client, "c=0 ch0=1").text
    report(client, "c=0 ch0=1 ack=3")
    post(client, "/controller", "c=0 retired=0")
    assert post(client, "/command", "c=0 water=3 ml=50").text == "cmd=4\n"


def test_a_retired_boards_lost_dose_is_not_judged_while_it_is_retired(app, client, db, sent):
    assert post(client, "/command", "c=0 water=3 ml=50").text == "cmd=1\n"
    assert "cmd=1 water=3 ml=50" in report(client, "c=0 ch0=1").text  # handed
    post(client, "/controller", "c=0 retired=1")
    report(client, "c=0 ch0=1")  # no ack: expired, never acknowledged
    assert commands(db) == [(1, "expired", None)]
    tick(app)
    assert keys(sent) == []
    # Skipped, not forgotten: back in service, the hand-off is judged.
    post(client, "/controller", "c=0 retired=0")
    tick(app)
    assert keys(sent) == ["dose:1"]


# --------------------------------------------------------------------------- #
# The durable latch (spec D2-D4)
# --------------------------------------------------------------------------- #


def test_the_board_latches_the_backend_through_ch207_or_the_resetmid_edge(client):
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    latched = health(client)["latched"]
    assert latched["reason"] == "contra" and latched["since"] > 0
    # err= is the board's sticky last error, sent on every report until a
    # later dose ends otherwise; `clear contra` on the console never touches
    # it. ch207 is the level the console clears, and the only contra trigger.
    report(client, "c=1 ch0=1 float=1 pos=ok err=contra")
    assert health(client, 1)["latched"] is None
    report(client, "c=2 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client, 2)["latched"]["reason"] == "resetmid"
    # An empty tank is not a latch (D2's deviation): the rules refuse on it.
    report(client, "c=3 ch0=1 float=1 pos=ok")
    report(client, "c=3 ch0=1 float=0 pos=ok")
    assert health(client, 3)["latched"] is None


def test_the_sticky_err_contra_cannot_relatch_a_resumed_board(client):
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 err=contra")
    assert health(client)["latched"]["reason"] == "contra"
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    # `clear contra` on the board dropped ch207; err= still says contra, and
    # will until the next dose ends with something else.
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 err=contra")
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 err=contra")
    assert health(client)["latched"] is None
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200


def test_resetmid_latches_on_its_edge_and_not_on_every_repeat(client):
    report(client, "c=0 ch0=1 float=1 pos=ok err=range")
    assert health(client)["latched"] is None
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client)["latched"]["reason"] == "resetmid"
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    # The board keeps saying so: the same error, not a new one.
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client)["latched"] is None
    # A later transition into it is a new reset with the pump running.
    report(client, "c=0 ch0=1 float=1 pos=ok err=none")
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client)["latched"]["reason"] == "resetmid"
    # A board's first report ever saying it latches: nothing stored before.
    report(client, "c=1 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client, 1)["latched"]["reason"] == "resetmid"


def test_err_ts_is_when_the_error_changed_not_when_it_was_last_seen(client, db):
    report(client, "c=0 ch0=1 err=heap")
    # Same second as the reports below: backdate so kept and rewritten differ.
    run_sql(db, "UPDATE status SET err_ts = err_ts - 60")
    stamp = health(client)["err_ts"]
    report(client, "c=0 ch0=1 err=heap")
    report(client, "c=0 ch0=1 err=heap")
    kept = health(client)
    assert kept["err"] == "heap" and kept["err_ts"] == stamp
    report(client, "c=0 ch0=1 err=none")
    changed = health(client)
    assert changed["err"] == "none" and changed["err_ts"] > stamp


def test_the_latch_outlives_the_board_forgetting_it(client, db):
    make_pot(client, cooldown_h=0)
    report(client, f"c=0 ch0={DRY} float=1 pos=ok ch207=1")
    since = health(client)["latched"]["since"]
    # A power cycle: the board comes back clean and keeps saying so.
    dry_reports(client, extra="ch207=0")
    assert health(client)["latched"] == {"since": since, "reason": "contra"}
    assert commands(db) == []  # the rules stayed dry for the whole window


def test_the_latch_keeps_its_first_stamp_and_reason_while_it_stands(client, db):
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    # The reports below land in the same second as the first, so a stamp
    # rewritten on every latching report would equal the one that should
    # have stayed put. Push it back a minute first, so kept and rewritten
    # can differ.
    run_sql(db, "UPDATE status SET latched_ts = latched_ts - 60")
    stamp = health(client)["latched"]["since"]
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")  # the board still says so
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")  # a second fault on top
    assert health(client)["latched"] == {"since": stamp, "reason": "contra"}
    # `since` is when the trouble began: resume ends this latch, and the
    # next one is a new one, with its own onset and its own reason — here
    # a fresh reset with the pump running, err= turning to resetmid again.
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    report(client, "c=0 ch0=1 float=1 pos=ok err=none")
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    fresh = health(client)["latched"]
    assert fresh["reason"] == "resetmid" and fresh["since"] > stamp


def test_a_latch_expires_what_was_waiting_and_refuses_new_water(client, db):
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ack=99")  # the queued one is NOT handed
    assert commands(db) == [(1, "expired", None)]
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.startswith("refused: board 0 stopped watering (contra since ")
    assert answer.text.rstrip().endswith("check the tank, type clear contra on the board, then resume")
    assert post(client, "/command", "c=0 stop=1").status_code == 200


def test_the_latch_pages_once_and_resume_clears_row_and_page(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 err=contra")
    tick(app)
    tick(app)
    assert keys(sent) == ["latch:0"]
    (alert,) = [a for a in sent if a.key == "latch:0"]
    assert alert.priority == "high" and "float said full and the meter saw nothing" in alert.message
    assert client.get("/health").json()["alerts"][0]["key"] == "latch:0"
    answer = post(client, "/resume", "c=0")
    assert answer.status_code == 200 and answer.text == "resumed=0\n"
    assert health(client)["latched"] is None
    assert client.get("/health").json()["alerts"] == []
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    # A re-latch inside the hour pages again: no re-alert floor on this one.
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    assert post(client, "/resume", "c=0").text == "resumed=0\n"  # idempotent on a clean board
    assert post(client, "/resume", "c=0").text == "resumed=0\n"


# --------------------------------------------------------------------------- #
# Refills, and the float judged against the tank's size (spec D6, D7)
# --------------------------------------------------------------------------- #


def refill(client):
    """The human taps "refilled" on board 0; the tap's ts."""
    answer = post(client, "/refill", "c=0")
    assert answer.status_code == 200, answer.text
    return int(answer.text.removeprefix("refill=").strip())


def age(db, seconds):
    """Everything so far happened `seconds` earlier, relations kept: the
    tests run inside one second, a dose handed in an origin's own second
    is counted as after it, and two float=0 sightings inside the flap
    window are one float flapping, not two runs of the tank."""
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE refills SET ts = ts - ?, drop_ts = drop_ts - ?", (seconds, seconds)
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
        con.execute(
            "UPDATE status SET float_since = float_since - ?, "
            "float_word_since = float_word_since - ?, float_rise = float_rise - ?, "
            "float_seen = float_seen - ?, "
            "float_bad = float_bad - ?, float_bad_prev = float_bad_prev - ?",
            (seconds, seconds, seconds, seconds, seconds, seconds),
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
    """The human says the tank is full, a minute ago; the tap's ts as it
    stands after that."""
    ts = refill(client)
    age(db, 60)
    return ts - 60


def dose(client, ml, float_ok=1):
    """A manual dose, handed on one report and acked with the meter's count
    on the next, whose float= says `float_ok`."""
    answer = post(client, "/command", f"c=0 water=3 ml={ml}")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.strip().removeprefix("cmd="))
    handed = report(client, "c=0 ch0=1 float=1 pos=ok").text
    assert f"cmd={cmd_id} water=3 ml={ml}" in handed
    report(client, f"c=0 ch0=1 float={float_ok} pos=ok ack={cmd_id} flow_ml={ml}")


def learn_the_tank(app, client, db, sent, size):
    """Two runs of `size` ml, each ended by the float and a flap window
    apart: the tank is known, and its two announcements are ticked away
    (they are test_tank_size's subject). Returns the line `over` starts
    past."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    for _ in range(TANK_SAMPLES_TO_ARM):
        tap(client, db)
        dose(client, size, float_ok=0)
        age(db, FLAP_WINDOW_S + 1)
        report(client, "c=0 ch0=1 float=1 pos=ok")
    assert health(client)["tank_ml"] == size
    tick(app)
    assert [k.split(":")[0] for k in keys(sent)] == ["tank"] * TANK_SAMPLES_TO_ARM
    # Both waited for this one tick, and each is judged as it closed: the
    # first knew nothing yet, the second knew the first.
    assert sent[0].message.endswith(f"(tank size learning, 1 of {TANK_SAMPLES_TO_ARM})")
    assert sent[1].message.endswith(f"(tank {size} ml over {TANK_SAMPLES_TO_ARM} samples)")
    sent.clear()
    return size * (100 + TANK_TOLERANCE_PCT) // 100


def rules_water(db):
    return run_sql(db, "SELECT id FROM commands WHERE source = 'rules'")


def taps(db):
    return [ts for (ts,) in run_sql(db, "SELECT ts FROM refills ORDER BY ts, rowid")]


def word_since(db):
    """When the float's word last changed, 1 -> 0 or 0 -> 1."""
    return run_sql(db, "SELECT float_word_since FROM status WHERE controller = 0")[0][0]


def rise(db):
    """When the float's word last went 0 -> 1."""
    return run_sql(db, "SELECT float_rise FROM status WHERE controller = 0")[0][0]


def alerts(client):
    return [a["key"] for a in client.get("/health").json()["alerts"]]


def test_a_refill_is_recorded_and_shown(client, db):
    before = int(time.time())
    answer = post(client, "/refill", "c=0")
    assert answer.status_code == 200
    ts = int(answer.text.removeprefix("refill=").strip())
    assert ts >= before
    assert run_sql(db, "SELECT controller FROM refills") == [(0,)]
    assert health(client)["last_refill"] == ts
    assert post(client, "/refill", "retired=1").status_code == 400


def test_over_fires_past_the_tolerance_and_only_while_the_float_says_full(
    app, client, db, sent
):
    assert learn_the_tank(app, client, db, sent, 200) == 220
    since = tap(client, db)
    dose(client, 100)
    dose(client, 120)  # 220: at the line, not past it
    tick(app)
    assert keys(sent) == [] and health(client)["over"] == 0
    dose(client, 1)  # 221, and the float still says full
    assert health(client)["over"] == 1
    tick(app)
    tick(app)
    assert keys(sent) == ["over:0"]  # once
    (alert,) = sent
    assert alert.priority == "high"
    assert alert.message == (
        f"board 0 pumped 221 ml since {butler.hhmm(since)}, more than its tank "
        "holds (200 ml), and the float still says full: presumed stuck, the "
        "rules will not water until the next refill"
    )
    assert alerts(client) == ["over:0"]


def test_over_counts_from_the_rise_when_the_float_moved_since_the_tap(
    app, client, db, sent
):
    """A float that went 1 -> 0 -> 1 since the tap is a tank run down and
    refilled by someone who forgot to tap: it demonstrably moved, so the
    counter restarts at the rise instead of calling it stuck twenty
    millilitres later, and the page says since when. A second drain keeps
    the rise as the origin and stores no second sample: nobody said that
    refill was full (spec D3, D4, D6, amended twice)."""
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 150)
    dose(client, 60, float_ok=0)  # 210: the tank ran down, as a tank does
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=1 pos=ok")  # refilled, untapped
    age(db, 60)
    risen = rise(db)
    assert risen > taps(db)[-1]
    dose(client, 20)  # 230 since the tap, 20 since the rise
    entry = health(client)
    assert entry["pumped_ml"] == 20 and entry["over"] == 0
    tick(app)
    assert [k.split(":")[0] for k in keys(sent)] == ["tank"]  # the run, no over
    dose(client, 180, float_ok=0)  # 200 since the rise: drained again
    assert health(client)["tank_samples"] == 3  # the tapped run, not this one
    with sqlite3.connect(db) as con:
        assert butler.counter_origin(con, 0) == (risen, "rise")  # sticky
    assert health(client)["pumped_ml"] == 200
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=1 pos=ok")  # refilled untapped again
    assert rise(db) > risen - FLAP_WINDOW_S - 1  # a new rise, and the counter restarts
    age(db, 60)
    risen = rise(db)
    assert health(client)["pumped_ml"] == 0
    dose(client, 221)  # 221 since this rise
    assert health(client)["over"] == 1
    tick(app)
    assert keys(sent)[-1] == "over:0"
    assert sent[-1].message.startswith(
        f"board 0 pumped 221 ml since {butler.hhmm(risen)}, more than its tank "
        "holds (200 ml), "
    )


def test_over_waits_behind_the_latch_and_the_boards_own_ch207(app, client, db, sent):
    """A report carrying ch207=1 is "float OK, zero pulses": a fault, and
    the latch page already says what to do. The board keeps sending it
    until `clear contra` is typed, so a /resume before that must not page
    the same fault as a stuck float (spec §1, D6 amended twice)."""
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    assert health(client)["over"] == 1  # the fact stands
    tick(app)
    tick(app)
    assert keys(sent) == ["latch:0"]  # the latch page already says what to do
    assert post(client, "/resume", "c=0").status_code == 200
    tick(app)
    assert keys(sent) == ["latch:0"]  # resumed, but the board still says ch207=1
    assert health(client)["over"] == 1
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0")  # clear contra was typed
    tick(app)
    assert keys(sent) == ["latch:0", "over:0"]


def test_a_dead_float_waits_behind_the_boards_own_ch207(app, client, db, sent):
    """The forced 0 of a contra is not a float that failed to rise: a
    /resume before `clear contra` pages nothing as dead until a report
    without ch207=1 says the word is the float's own again (spec D7,
    amended twice)."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")
    tick(app)
    assert keys(sent) == ["latch:0"]
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)  # with the forced 0 at the tap
    age(db, PERSIST_S - 60)
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")  # a word, its minutes on
    assert post(client, "/resume", "c=0").status_code == 200
    tick(app)
    assert keys(sent) == ["latch:0"]  # the board still says ch207=1
    age(db, FLAP_WINDOW_S + 1)  # one more sighting of empty is slosh, not a flap
    report(client, "c=0 ch0=1 float=0 pos=ok")  # cleared on the board, still empty
    tick(app)
    assert keys(sent) == ["latch:0", "stale:0"]


def test_over_clears_on_a_tap_and_on_nothing_else(app, client, db, sent):
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    tick(app)
    assert keys(sent) == ["over:0"]
    age(db, 60)
    # The float word dropping to 0 is a contra, a flap or an omitted float=
    # as often as an empty tank: the page stands, and so does /health's over.
    report(client, "c=0 ch0=1 pos=ok")  # says nothing about the float
    tick(app)
    assert keys(sent) == ["over:0"]
    assert health(client)["over"] == 1
    report(client, "c=0 ch0=1 float=0 pos=ok")  # empty: the run is a sample...
    tick(app)  # ...heard with the float at 0, which clears nothing...
    assert alerts(client) == ["over:0"] and health(client)["over"] == 1
    age(db, 60)
    report(client, "c=0 ch0=1 float=1 pos=ok")  # ...and full again, untapped
    tick(app)
    assert [k for k in keys(sent) if k.startswith("over:")] == ["over:0"]
    assert alerts(client) == ["over:0"]
    entry = health(client)
    assert entry["pumped_ml"] == 0  # the counter restarted at the rise
    assert entry["over"] == 1  # the page is the fact until a tap answers it
    # A tap the board never saw is no answer (a row from before 0.19.0).
    refill(client)
    run_sql(db, "UPDATE refills SET float_ok = NULL WHERE ts = ?", taps(db)[-1])
    tick(app)
    assert alerts(client) == ["over:0"]
    # A tap that saw the float, later than the raise: cleared, and that is
    # all it says — "watering resumes" was untrue on a float at 0.
    refill(client)
    tick(app)
    assert sent[-1].key == "over:0" and sent[-1].priority == "default"
    assert sent[-1].message == "the tank on board 0 was refilled"
    assert alerts(client) == [] and health(client)["over"] == 0


def test_a_float_that_goes_empty_past_the_size_is_a_float_that_works(
    app, client, db, sent
):
    learn_the_tank(app, client, db, sent, 200)
    since = tap(client, db)
    dose(client, 150)
    dose(client, 71, float_ok=0)  # 221, and the float said so
    assert health(client)["over"] == 0
    tick(app)
    assert keys(sent) == [f"tank:0:{since}"]  # a longer run, learned and told
    assert health(client)["tank_samples"] == 3


def test_over_holds_the_rules_not_the_phone_and_the_tap_is_the_clear(
    app, client, db, sent
):
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    tick(app)
    assert keys(sent) == ["over:0"]
    dry_reports(client)
    assert rules_water(db) == []
    # A dose typed at the phone still goes: a human is at the phone, and
    # the board's own float check runs before its pump does.
    dose(client, 50)
    age(db, 60)  # a minute on...
    tap(client, db)  # ...the tap, later than the page
    assert health(client)["over"] == 1  # until the ticker hears of it
    tick(app)
    assert keys(sent) == ["over:0", "over:0"]
    assert sent[-1].priority == "default"
    assert sent[-1].message == "the tank on board 0 was refilled"
    assert alerts(client) == [] and health(client)["over"] == 0
    # The window is already five dry readings deep: the next report waters.
    assert "cmd=" in dry_reports(client, n=1)
    assert len(rules_water(db)) == 1


def test_over_holds_the_rules_through_a_float_bounce_until_the_tap(
    app, client, db, sent
):
    """The float going 0 -> 1 with nobody tapping, after it went 1 -> 0
    since the tap, is a rise: a fresh origin, a counter at 0, and the live
    predicate lets go. The page still stands, and the page is the fact —
    it was raised on a pump presumed stuck at full — so the rules stay dry
    on it, as /health's over stays 1, until a tap answers it (spec D6)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    tick(app)
    assert keys(sent) == ["over:0"]
    age(db, 60)
    report(client, f"c=0 ch0={DRY} float=0 pos=ok")
    age(db, 60)
    report(client, f"c=0 ch0={DRY} float=1 pos=ok")  # a bounce, untapped
    entry = health(client)
    assert entry["pumped_ml"] == 0  # the counter let go...
    assert entry["over"] == 1  # ...the page did not
    dry_reports(client)
    assert rules_water(db) == []  # and the rules read the page
    age(db, 60)
    tap(client, db)
    tick(app)
    assert alerts(client) == [] and health(client)["over"] == 0
    assert "cmd=" in dry_reports(client, n=1)
    assert len(rules_water(db)) == 1


def test_a_report_that_omits_float_neither_hides_nor_makes_a_rise(
    app, client, db, sent
):
    """One report saying nothing about the float, then the same word again,
    is not the float moving: no rise is invented, the counter keeps what
    it had, and a float stuck at full is still caught (spec D3, D6)."""
    learn_the_tank(app, client, db, sent, 200)
    since = tap(client, db)
    dose(client, 150)
    report(client, "c=0 ch0=1 pos=ok")  # says nothing about the float...
    report(client, "c=0 ch0=1 float=1 pos=ok")  # ...then full, as before
    with sqlite3.connect(db) as con:
        assert butler.counter_origin(con, 0) == (since, "tap")
    assert health(client)["pumped_ml"] == 150
    dose(client, 71)  # 221 since the tap, the float at full throughout
    assert health(client)["over"] == 1
    tick(app)
    assert keys(sent) == ["over:0"]


def test_over_pages_once_per_floor(app, client, db, sent):
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    tick(app)
    age(db, 60)
    tap(client, db)
    tick(app)
    assert keys(sent) == ["over:0", "over:0"]
    dose(client, 250)  # over again inside the hour: the page waits its floor
    tick(app)
    assert keys(sent) == ["over:0", "over:0"]
    assert health(client)["over"] == 1  # the state is a fact all the same
    run_sql(
        db,
        "UPDATE alerts SET cleared_ts = cleared_ts - ? WHERE key = 'over:0'",
        REALERT_FLOOR_S,
    )
    tick(app)
    assert keys(sent) == ["over:0", "over:0", "over:0"]


def test_a_top_up_tapped_daily_never_fires(app, client, db, sent):
    learn_the_tank(app, client, db, sent, 200)
    for _ in range(4):
        age(db, 60)
        tap(client, db)
        dose(client, 150)  # the day's water, under the size...
        tick(app)
    assert health(client)["pumped_ml"] == 150  # ...counted from the last tap alone
    assert keys(sent) == [] and health(client)["over"] == 0


def test_retiring_a_board_clears_its_over_page(app, client, db, sent):
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    tick(app)
    assert keys(sent) == ["over:0"]
    post(client, "/controller", "c=0 retired=1")
    assert alerts(client) == []
    entry = health(client)
    assert entry["pumped_ml"] == 250 and entry["over"] == 0  # retired is the last word
    tick(app)
    assert keys(sent) == ["over:0"]  # neither cleared aloud nor raised again
    # Past the floor a cleared page may sound again; a retired board's does
    # not, however long it stays over. Back in service, it is heard at once.
    run_sql(
        db,
        "UPDATE alerts SET cleared_ts = cleared_ts - ? WHERE key = 'over:0'",
        REALERT_FLOOR_S,
    )
    tick(app)
    assert keys(sent) == ["over:0"]
    post(client, "/controller", "c=0 retired=0")
    assert health(client)["over"] == 1
    tick(app)
    assert keys(sent) == ["over:0", "over:0"]
    assert sent[-1].priority == "high"


def test_a_tank_still_learning_is_never_over(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1 pos=ok")
    tap(client, db)
    dose(client, 200, float_ok=0)  # one sample: the size is not known yet
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=1 pos=ok")
    tap(client, db)
    dose(client, 250)
    dose(client, 250)  # 500 since the tap, past any size, the float at full
    entry = health(client)
    assert entry["tank_samples"] == 1 and entry["tank_ml"] is None
    assert entry["pumped_ml"] == 500 and entry["over"] == 0
    tick(app)
    assert [k.split(":")[0] for k in keys(sent)] == ["tank"]  # the sample, no over


def test_a_float_still_empty_its_minutes_after_the_tap_pages(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")
    age(db, FLAP_WINDOW_S + 1)  # empty a while: one sighting is slosh, not a flap
    tap(client, db)  # with the float saying empty, a minute ago
    tick(app)
    assert keys(sent) == []  # its minutes to settle
    age(db, PERSIST_S - 60)  # its minutes are up on the wall clock...
    tick(app)
    assert keys(sent) == []  # ...but the board has said nothing since the tap
    report(client, "c=0 ch0=1 float=0 pos=ok")  # a word, its minutes on
    tick(app)
    assert keys(sent) == ["stale:0"]
    (alert,) = sent
    (tapped,) = taps(db)
    assert alert.priority == "high"
    assert alert.message == (
        f"the float on board 0 still says empty 3 min after the refill at "
        f"{butler.hhmm(tapped)}: a stuck float, or the board's own float check "
        "tripped — look at the magnet, or water once from the phone (a granted "
        "dose resets the board's check)"
    )
    tick(app)
    assert keys(sent) == ["stale:0"]  # once
    # A second tap while it still says empty is not the float moving.
    refill(client)
    tick(app)
    assert keys(sent) == ["stale:0"]
    assert alerts(client) == ["stale:0"]
    # Then the float moves: cleared.
    report(client, "c=0 ch0=1 float=1 pos=ok")
    tick(app)
    assert keys(sent) == ["stale:0", "stale:0"]
    assert sent[-1].priority == "default"
    assert sent[-1].message == "the float on board 0 moved"
    assert alerts(client) == []


def test_a_float_that_rose_after_the_tap_and_fell_later_is_not_dead(
    app, client, db, sent
):
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")
    tap(client, db)
    report(client, "c=0 ch0=1 float=1 pos=ok")  # rose as the water arrived
    age(db, PERSIST_S)
    report(client, "c=0 ch0=1 float=1 pos=ok")
    tick(app)
    assert keys(sent) == []
    age(db, FLAP_WINDOW_S + 1)  # days later...
    report(client, "c=0 ch0=1 float=0 pos=ok")  # ...legitimately empty again
    tick(app)
    assert keys(sent) == []


def test_a_float_still_empty_pages_through_a_report_that_omits_it(
    app, client, db, sent
):
    """A report that says nothing about the float, then the same word
    again, is not the float moving: its word last changed before the tap
    still, and the page comes (spec D7)."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)  # with the float saying empty, a minute ago
    report(client, "c=0 ch0=1 pos=ok")  # says nothing about the float...
    report(client, "c=0 ch0=1 float=0 pos=ok")  # ...then empty, as before
    assert word_since(db) < taps(db)[-1]  # it has not moved
    age(db, FLAP_WINDOW_S + 1)  # its minutes are up, and one sighting is no flap
    report(client, "c=0 ch0=1 float=0 pos=ok")  # a word, its minutes on
    tick(app)
    assert keys(sent) == ["stale:0"]


def test_a_dead_float_is_judged_on_a_word_of_empty_not_on_silence(
    app, client, db, sent
):
    """The report that says nothing about the float is not one that says
    empty: stale: waits for the word (spec D7)."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    age(db, PERSIST_S - 60)
    report(client, "c=0 ch0=1 float=0 pos=ok")  # a word, its minutes on...
    report(client, "c=0 ch0=1 pos=ok")  # ...then one that says nothing
    tick(app)
    assert keys(sent) == []  # judged on the word, and there was none
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=0 pos=ok")
    tick(app)
    assert keys(sent) == ["stale:0"]


def test_a_tap_that_never_saw_the_float_judges_nothing(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    run_sql(db, "UPDATE refills SET float_ok = NULL")  # a row from before 0.19.0
    age(db, PERSIST_S - 60)
    report(client, "c=0 ch0=1 float=0 pos=ok")
    tick(app)
    assert keys(sent) == []
    # A board that has never sent float= has nothing to judge either.
    assert post(client, "/refill", "c=1").status_code == 200
    age(db, PERSIST_S)
    tick(app)
    assert keys(sent) == []
    # The next tap looks at the float.
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    age(db, PERSIST_S - 60)
    report(client, "c=0 ch0=1 float=0 pos=ok")
    tick(app)
    assert keys(sent) == ["stale:0"]


def test_a_dead_float_waits_behind_the_latch(app, client, db, sent):
    """A contra forces the board's word to 0, and the latch page already
    says what to do (spec D7)."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1")
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    age(db, PERSIST_S - 60)
    report(client, "c=0 ch0=1 float=0 pos=ok")
    tick(app)
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert post(client, "/resume", "c=0").status_code == 200
    tick(app)
    assert keys(sent) == ["latch:0", "stale:0"]


def test_a_stale_page_from_the_clock_rule_clears_when_the_float_says_full(
    app, client, db, sent
):
    # 0.18.0 raised it off ch204 and a refill; neither says anything now,
    # and the key is kept so it still clears through the same path.
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")
    run_sql(
        db,
        "INSERT INTO alerts (key, raised_ts, cleared_ts) VALUES ('stale:0', ?, NULL)",
        int(time.time()) - 86400,
    )
    tick(app)
    assert keys(sent) == [] and alerts(client) == ["stale:0"]
    report(client, "c=0 ch0=1 float=1 pos=ok")
    tick(app)
    assert keys(sent) == ["stale:0"]
    assert sent[0].priority == "default" and sent[0].message == "the float on board 0 moved"
    assert alerts(client) == []


def test_the_helpers_read_the_origin_the_size_the_tap_and_the_float(app, db):
    with sqlite3.connect(db) as con:
        origin = lambda: butler.counter_origin(con, 0)  # noqa: E731
        state = lambda: butler.tank_state(con, 0, origin())  # noqa: E731
        tapped = lambda: butler.latest_refill(con, 0)  # noqa: E731
        dead = lambda: butler.float_dead(con, 0, tapped())  # noqa: E731

        def pumped(ml, sent_ts):
            con.execute(
                "INSERT INTO commands (created_ts, controller, kind, outlet, ml, "
                "cap_s, state, source, sent_ts, acked_ts, flow_ml) "
                "VALUES (?, 0, 'water', 3, ?, 30, 'acked', 'manual', ?, ?, ?)",
                (sent_ts, ml, sent_ts, sent_ts + 1, ml),
            )

        assert origin() is None and state() == "unknown" and dead() is None
        con.execute("INSERT INTO refills (ts, controller, float_ok) VALUES (1000, 0, NULL)")
        assert origin() is None  # a tap that saw nothing is no origin
        con.execute("INSERT INTO refills (ts, controller, float_ok) VALUES (1000, 0, 1)")
        assert origin() == (1000, "tap") and state() == "unknown"  # size unknown
        con.executemany(
            "INSERT INTO tank_samples (ts, controller, refill_ts, ml) VALUES (?, 0, ?, ?)",
            [(1, 1, 190), (2, 2, 210)],
        )
        con.execute(
            "INSERT INTO status (controller, ts, float_ok, float_word, "
            "float_word_since, float_rise, float_seen) "
            "VALUES (0, 1000, 1, 1, 900, 900, 1000)"
        )
        assert origin() == (1000, "tap") and state() == "ok"  # nothing pumped
        pumped(220, 1001)
        assert state() == "ok"  # 200 + 10 %: at the line
        pumped(1, 1002)
        assert state() == ("over", 221, 200, 1000)
        con.execute("UPDATE status SET float_rise = 1002")  # rose after the tap...
        assert origin() == (1000, "tap")  # ...with no drop since it: the tap's own fill
        con.execute("UPDATE refills SET drop_ts = 1002 WHERE float_ok = 1")
        assert origin() == (1000, "tap")  # a drop in the rise's second: a bounce
        con.execute("UPDATE refills SET drop_ts = 1001 WHERE float_ok = 1")
        # Drained after the tap, then risen: an untapped refill. 1 ml since:
        # the dose handed in the rise's own second is after it.
        assert origin() == (1002, "rise") and state() == "ok"
        pumped(220, 1003)
        assert state() == ("over", 221, 200, 1002)
        con.execute("UPDATE status SET float_ok = NULL")  # a report that said nothing
        assert origin() == (1002, "rise")  # the rise stands: the word did not move
        assert state() == "ok"  # and a float that says nothing refuses already
        con.execute("UPDATE status SET float_ok = 0, float_word = 0")  # drained again
        assert origin() == (1002, "rise")  # sticky: the rise is where it was
        assert state() == "ok"  # a float that reads empty works
        con.execute("UPDATE status SET float_ok = 1, float_word = 1, float_rise = 900")
        con.execute("INSERT INTO refills (ts, controller, float_ok) VALUES (2000, 0, 1)")
        assert origin() == (2000, "tap") and state() == "ok"  # restarts at the tap
        con.execute("UPDATE status SET float_rise = 2001")  # rose, with no drop since
        assert origin() == (2000, "tap")  # the human's word stands
        # Judged from the origin it is handed, never one it reads for itself.
        assert butler.tank_state(con, 0, (1000, "tap")) == ("over", 441, 200, 1000)
        assert butler.tank_state(con, 0, None) == "unknown"
        # Dead at empty reads the latest tap, whatever the origin, and waits
        # for a word from the board its minutes after it.
        assert dead() is None  # the float said full at the tap
        con.execute("UPDATE refills SET float_ok = 0 WHERE ts = 2000")
        con.execute(
            "UPDATE status SET float_ok = 0, float_word = 0, float_word_since = 2000, "
            "float_seen = ?",
            (2000 + PERSIST_S - 1,),
        )
        assert dead() is None  # nothing said since its minutes
        con.execute("UPDATE status SET float_seen = ?", (2000 + PERSIST_S,))
        assert dead() == 2000
        con.execute("UPDATE status SET float_word_since = 2001")
        assert dead() is None  # it moved after the tap
        con.execute(
            "UPDATE status SET float_word_since = 1999, float_ok = 1, float_word = 1"
        )
        assert dead() is None  # it says full
        con.execute("UPDATE status SET float_ok = NULL, float_word = 0")
        assert dead() is None  # it said nothing, which is not empty
        con.execute("UPDATE status SET float_ok = 0")
        assert dead() == 2000
        assert butler.float_dead(con, 0, (1999, 0)) == 1999  # the tap handed
        assert butler.float_dead(con, 0, (2001, 0)) is None  # no word since its minutes
        con.execute("UPDATE refills SET float_ok = NULL WHERE ts = 2000")
        assert dead() is None  # a tap that never saw the float
