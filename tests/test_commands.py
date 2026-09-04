"""The command hand-off: one slot, handed once, acked or expired."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import butler
from butler import cap_for, create_app, parse_command, parse_report

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    return TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )


def report(client, body, token=TOKEN):
    return client.post("/report", content=body, headers={"X-Token": token})


def command(client, body, token=TOKEN):
    return client.post("/command", content=body, headers={"X-Token": token})


def interval(client, body, token=TOKEN):
    return client.post("/interval", content=body, headers={"X-Token": token})


def states(db):
    with sqlite3.connect(db) as con:
        return dict(con.execute("SELECT id, state FROM commands").fetchall())


# --------------------------------------------------------------------------- #
# The happy path: queued -> sent -> acked
# --------------------------------------------------------------------------- #


def test_a_water_command_rides_the_next_report_response(client, db):
    answer = command(client, "c=0 water=3 ml=50 cap_s=30")
    assert answer.status_code == 200
    assert answer.text == "cmd=1\n"

    answer = report(client, "c=0 t=1000 ch0=8000")
    assert answer.text == "next=60\ncmd=1 water=3 ml=50 cap_s=30\n"
    assert states(db) == {1: "sent"}


def test_the_following_report_acks_it_and_the_flow_count_is_kept(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")

    answer = report(client, "c=0 t=61000 ch0=8000 ack=1 flow_ml=48")
    assert answer.text == "next=60\n"
    with sqlite3.connect(db) as con:
        state, flow = con.execute(
            "SELECT state, flow_ml FROM commands WHERE id = 1"
        ).fetchone()
    assert (state, flow) == ("acked", 48)


def test_a_stop_command_is_handed_as_stop(client, db):
    command(client, "c=0 stop=1")

    answer = report(client, "c=0 t=1000 ch0=8000")
    assert answer.text == "next=60\ncmd=1 stop=1\n"


def test_a_command_waits_for_its_own_controller(client, db):
    command(client, "c=2 water=1 ml=50 cap_s=30")

    answer = report(client, "c=0 t=1000 ch0=8000")
    assert answer.text == "next=60\n"
    assert states(db) == {1: "queued"}


# --------------------------------------------------------------------------- #
# Expiry: the board does not have it means it is gone
# --------------------------------------------------------------------------- #


def test_a_report_without_the_ack_expires_the_sent_command(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")  # handed here

    answer = report(client, "c=0 t=61000 ch0=8000")  # no ack
    assert "cmd=" not in answer.text
    assert states(db) == {1: "expired"}


def test_a_lost_response_expires_the_command_it_carried(client, db):
    # The board retries the same report when the response is lost; the retry
    # cannot carry an ack, so the command the lost response carried is gone —
    # never re-handed, because the board might still be executing a copy.
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")

    retry = report(client, "c=0 t=1000 ch0=8000")
    assert "cmd=" not in retry.text
    assert states(db) == {1: "expired"}


def test_a_late_ack_for_an_expired_command_changes_nothing(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")
    report(client, "c=0 t=61000 ch0=8000")  # expires it

    report(client, "c=0 t=121000 ch0=8000 ack=1 flow_ml=48")
    assert states(db) == {1: "expired"}


def test_a_queued_command_nobody_collects_expires(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    with sqlite3.connect(db) as con:  # age it past the TTL
        con.execute("UPDATE commands SET created_ts = created_ts - 3600")

    answer = report(client, "c=0 t=1000 ch0=8000")
    assert "cmd=" not in answer.text
    assert states(db) == {1: "expired"}


# --------------------------------------------------------------------------- #
# One slot per controller, never deeper
# --------------------------------------------------------------------------- #


def test_the_slot_holds_one_command_while_queued(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")

    answer = command(client, "c=0 stop=1")
    assert answer.status_code == 409
    assert answer.text == "busy: cmd=1 state=queued\n"


def test_the_slot_holds_one_command_while_sent(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")

    answer = command(client, "c=0 stop=1")
    assert answer.status_code == 409
    assert "state=sent" in answer.text


def test_controllers_have_their_own_slots(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    assert command(client, "c=2 water=1 ml=50 cap_s=30").status_code == 200


def test_an_abandoned_command_frees_the_slot_after_the_ttl(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")  # sent; the board dies now
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE commands SET created_ts = created_ts - 3600, "
            "sent_ts = sent_ts - 3600"
        )

    assert command(client, "c=0 stop=1").status_code == 200
    assert states(db) == {1: "expired", 2: "queued"}


# --------------------------------------------------------------------------- #
# The interval knob
# --------------------------------------------------------------------------- #


def test_the_interval_knob_changes_what_reports_are_told(client, db):
    assert interval(client, "c=0 next=120").text == "next=120\n"

    answer = report(client, "c=0 t=1000 ch0=8000")
    assert answer.text == "next=120\n"


def test_next_0_clears_the_override(client, db):
    interval(client, "c=0 next=120")

    assert interval(client, "c=0 next=0").text == "next=60\n"
    assert report(client, "c=0 t=1000 ch0=8000").text == "next=60\n"


# --------------------------------------------------------------------------- #
# Health: heartbeat, knob and slot, per controller
# --------------------------------------------------------------------------- #


def test_health_shows_heartbeat_and_the_open_command(client, db):
    command(client, "c=0 water=3 ml=50 cap_s=30")
    report(client, "c=0 t=1000 ch0=8000")

    (entry,) = client.get("/health").json()["controllers"]
    assert entry["controller"] == 0
    assert entry["last_seen"] > 0
    assert entry["command"] == {"id": 1, "kind": "water", "state": "sent"}


def test_health_lists_a_configured_but_never_seen_controller(client, db):
    interval(client, "c=9 next=120")

    (entry,) = client.get("/health").json()["controllers"]
    assert entry == {
        "controller": 9,
        "last_seen": 0,
        "next_s": 120,
        "command": None,
        "float": None,
        "pos": None,
    }


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_dose_without_a_cap_gets_the_rules_own_cap(client, db):
    # One owner for the flow constant: the app never copies the formula.
    assert command(client, "c=0 water=3 ml=50").status_code == 200
    handed = report(client, "c=0 ch0=1").text
    assert "cmd=1 water=3 ml=50 cap_s=7" in handed  # 50 // 20 + 5
    report(client, "c=0 ch0=1 ack=1 flow_ml=50")
    command(client, "c=0 water=3 ml=1000")
    handed = report(client, "c=0 ch0=1").text
    assert "cmd=2 water=3 ml=1000 cap_s=55" in handed  # 1000 // 20 + 5, under MAX_CAP_S


@pytest.mark.parametrize(
    "body",
    [
        "water=3 ml=50 cap_s=30",  # no controller
        "c=0",  # neither water= nor stop=1
        "c=0 water=3 ml=50 cap_s=30 stop=1",  # both at once
        "c=0 stop=2",  # stop= is 1 or absent
        "c=0 stop=1 ml=50",  # stop takes no dose
        "c=0 water=3 cap_s=30",  # no ml=
        "c=0 water=3 ml=0 cap_s=30",  # zero dose
        "c=0 water=3 ml=5000 cap_s=30",  # a flood
        "c=0 water=3 ml=50 cap_s=600",  # cap too long
        "c=0 water=999 ml=50 cap_s=30",  # outlet out of range
        "c=0 water=3 water=4 ml=50 cap_s=30",  # water= twice
        "c= c=evil stop=1",  # empty c= must not open the door to a second
        "c=0 water=+3 ml=50 cap_s=30",  # leading +: refusal, not repair
    ],
)
def test_a_malformed_command_is_refused(client, db, body):
    answer = command(client, body)
    assert answer.status_code == 400
    assert answer.text.startswith("refused: ")
    assert states(db) == {}


@pytest.mark.parametrize(
    "body",
    [
        "next=120",  # no controller
        "c=0",  # no next=
        "c=0 next=4",  # below the floor
        "c=0 next=9999",  # above the ceiling
        "c=0 next=abc",  # not an integer
    ],
)
def test_a_malformed_interval_is_refused(client, db, body):
    assert interval(client, body).status_code == 400


def test_the_knob_cannot_let_a_live_board_outlive_the_command_ttl(client, db):
    # next=3600 parses, but with a 900s TTL the board's on-time report would
    # land after its own command had been swept aside — the double-dose hole.
    answer = interval(client, "c=0 next=3600")
    assert answer.status_code == 400
    assert "BUTLER_CMD_TTL_S" in answer.text


def test_a_ttl_shorter_than_the_report_beat_refuses_to_start(db):
    with pytest.raises(ValueError, match="BUTLER_CMD_TTL_S"):
        create_app(db_path=str(db), token=TOKEN, next_s=600, cmd_ttl_s=900)


def test_an_out_of_range_default_interval_refuses_to_start(db):
    with pytest.raises(ValueError, match="BUTLER_NEXT_S"):
        create_app(db_path=str(db), token=TOKEN, next_s=0)


def test_commands_and_the_knob_need_the_token_too(client, db):
    assert command(client, "c=0 stop=1", token="nope").status_code == 401
    assert interval(client, "c=0 next=120", token="nope").status_code == 401
    assert states(db) == {}


# --------------------------------------------------------------------------- #
# The parsers on their own
# --------------------------------------------------------------------------- #


def test_ack_parsing_is_as_strict_as_the_rest():
    r = parse_report("c=7 ch0=1 ack=17 flow_ml=48")
    assert (r.ack, r.flow_ml) == (17, 48)
    for bad in [
        "c=7 ch0=1 flow_ml=48",  # flow_ml without ack
        "c=7 ch0=1 ack=0",  # command ids start at 1
        "c=7 ch0=1 ack=17 ack=18",  # ack twice
        "c=7 ch0=1 ack=nope",  # not an integer
    ]:
        with pytest.raises(ValueError):
            parse_report(bad)


def test_parse_command_shapes():
    assert parse_command("c=7 water=3 ml=50 cap_s=30") == (7, "water", 3, 50, 30)
    assert parse_command("c=7 stop=1 future=1") == (7, "stop", None, None, None)


def test_cap_for_stays_under_the_firmware_cap_after_a_retune(monkeypatch):
    assert cap_for(1) == 5  # the slack alone
    assert cap_for(butler.MAX_DOSE_ML) == 55  # under MAX_CAP_S at today's flow floor
    monkeypatch.setattr(butler, "FLOW_FLOOR_ML_S", 10)  # a bench retune
    assert cap_for(butler.MAX_DOSE_ML) == butler.MAX_CAP_S
