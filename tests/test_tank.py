"""Trust the tank: err=, the durable latch, refills, the stuck-float rule,
retirement, and the rules corrections that came with them."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import butler
from butler import PERSIST_S, create_app, parse_report

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
    assert entry["retired"] == 0


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


# --------------------------------------------------------------------------- #
# The durable latch (spec D2-D4)
# --------------------------------------------------------------------------- #


def test_the_board_latches_the_backend_through_ch207_or_err(client):
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    latched = health(client)["latched"]
    assert latched["reason"] == "contra" and latched["since"] > 0
    report(client, "c=1 ch0=1 float=1 pos=ok err=contra")
    assert health(client, 1)["latched"]["reason"] == "contra"
    report(client, "c=2 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client, 2)["latched"]["reason"] == "resetmid"
    # An empty tank is not a latch (D2's deviation): the rules refuse on it.
    report(client, "c=3 ch0=1 float=1 pos=ok")
    report(client, "c=3 ch0=1 float=0 pos=ok")
    assert health(client, 3)["latched"] is None


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
    # next one is a new one, with its own onset and its own reason.
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
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
    report(client, "c=0 ch0=1 float=1 pos=ok err=contra")
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
