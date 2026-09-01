"""Rules that water: every gate errs dry, and the flip to auto is a human act."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from butler import create_app, in_quiet, parse_quiet, parse_report

TOKEN = "test-token"
DRY = 11000  # pct 12 with the calibration below
WET = 8000  # pct 50


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    # quiet="0-0": the tests must not care what time it is
    return TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900, quiet="0-0")
    )


def make_pot(client, drop=None, **over):
    fields = {
        "name": "basil",
        "controller": "b1",
        "channel": 0,
        "outlet": 3,
        "dry_raw": 12000,
        "wet_raw": 4000,
        "target_low_pct": 30,
        "target_high_pct": 60,
        "dose_ml": 100,
        "mode": "auto",
    } | over
    if drop:
        del fields[drop]
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    answer = client.post("/pot", content=body, headers={"X-Token": TOKEN})
    assert answer.status_code == 200, answer.text


def report(client, raw=DRY, safe=True, extra="", token=TOKEN):
    body = f"c=b1 ch0={raw}"
    if safe:
        body += " float=1 pos=ok"
    if extra:
        body += f" {extra}"
    return client.post("/report", content=body, headers={"X-Token": token})


def soak(client, n, raw=DRY, safe=True):
    last = None
    for _ in range(n):
        last = report(client, raw, safe)
    return last


def commands(db):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT id, state, source, ml, cap_s, outlet FROM commands ORDER BY id"
        ).fetchall()


def post(client, path, body, token=TOKEN):
    return client.post(path, content=body, headers={"X-Token": token})


# --------------------------------------------------------------------------- #
# Auto: a dry median waters, in the same round trip
# --------------------------------------------------------------------------- #


def test_five_dry_reports_water_on_the_fifth(client, db):
    make_pot(client)
    assert soak(client, 4).text == "next=60\n"  # window not full yet

    fifth = report(client)
    assert fifth.text == "next=60\ncmd=1 water=3 ml=100 cap_s=10\n"
    assert commands(db) == [(1, "sent", "rules", 100, 10, 3)]


def test_a_wet_median_holds_even_with_dry_readings_in_it(client, db):
    make_pot(client)
    soak(client, 2, raw=DRY)
    soak(client, 3, raw=WET)  # window D D W W W: median wet
    assert commands(db) == []

    soak(client, 2, raw=DRY)  # window W W W D D: median still wet
    assert commands(db) == []

    soak(client, 1, raw=DRY)  # window W W D D D: the median finally tips
    assert len(commands(db)) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"drop": "target_low_pct"},
        {"drop": "dose_ml"},
        {"drop": "outlet"},
        {"mode": "manual"},
        {"enabled": 0},
    ],
)
def test_an_uncalibrated_or_manual_or_disabled_pot_never_waters(client, db, kwargs):
    # One variant per fresh database: /pot is a partial upsert, so reusing
    # the pot would quietly merge back the very field the variant drops.
    make_pot(client, **kwargs)
    soak(client, 6)
    assert commands(db) == []


# --------------------------------------------------------------------------- #
# Safety: the report itself must say it is safe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "extra",
    [
        "",  # the board does not send status fields yet: rules stay dark
        "float=0 pos=ok",  # reservoir empty
        "float=1 pos=unknown",  # manifold lost
        "float=1",  # half a story is no story
        "pos=ok",
    ],
)
def test_without_fresh_safety_fields_nothing_waters(client, db, extra):
    make_pot(client)
    for _ in range(6):
        answer = post(client, "/report", f"c=b1 ch0={DRY} {extra}".strip())
        assert answer.status_code == 200
    assert commands(db) == []


def test_the_status_fields_are_parsed_as_strictly_as_the_rest(client):
    for bad in ["float=2", "float=1 float=1", "pos=wet", "pos=ok pos=ok"]:
        answer = post(client, "/report", f"c=b1 ch0=1 {bad}")
        assert answer.status_code == 400, bad
    r = parse_report("c=x ch0=1 float=1 pos=unknown")
    assert (r.float_ok, r.pos) == (1, "unknown")


def test_quiet_hours_hold_the_water(db):
    hour = time.localtime().tm_hour
    covering = f"{hour}-{(hour + 1) % 24}"
    client = TestClient(
        create_app(
            db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900, quiet=covering
        )
    )
    make_pot(client)
    soak(client, 6)
    assert commands(db) == []


def test_quiet_window_logic():
    assert in_quiet(23, 22, 8) and in_quiet(3, 22, 8)
    assert not in_quiet(12, 22, 8)
    assert in_quiet(12, 8, 22)
    assert not in_quiet(12, 0, 0)  # 0-0 disables
    assert parse_quiet("22-08") == (22, 8)
    for bad in ["22", "8-25", "a-b", "22:08", "-5-8"]:
        with pytest.raises(ValueError):
            parse_quiet(bad)


def test_a_bad_quiet_setting_refuses_to_start(db):
    with pytest.raises(ValueError, match="BUTLER_QUIET"):
        create_app(db_path=str(db), token=TOKEN, quiet="8-25")


# --------------------------------------------------------------------------- #
# Cooldown and the daily cap
# --------------------------------------------------------------------------- #


def test_cooldown_blocks_a_second_dose(client, db):
    make_pot(client)  # default cooldown: 6 h
    soak(client, 5)  # waters
    report(client, extra="ack=1 flow_ml=97")

    soak(client, 6)  # still bone dry, but freshly watered
    assert len(commands(db)) == 1


def test_the_daily_cap_counts_what_actually_flowed(client, db):
    make_pot(client, cooldown_h=0, daily_cap_ml=150)
    soak(client, 5)  # first dose: 100 of 150
    report(client, extra="ack=1 flow_ml=100")

    soak(client, 6)  # 100 + 100 > 150: capped
    assert len(commands(db)) == 1


def test_under_the_cap_a_thirsty_pot_waters_again(client, db):
    make_pot(client, cooldown_h=0, daily_cap_ml=300)
    soak(client, 5)

    ack = report(client, extra="ack=1 flow_ml=100")  # still dry, under cap
    assert "cmd=2" in ack.text  # the refill rides the ack's own response
    assert len(commands(db)) == 2


# --------------------------------------------------------------------------- #
# The slot: rules never fight the human for it
# --------------------------------------------------------------------------- #


def test_auto_yields_the_slot_and_retries_next_report(client, db):
    make_pot(client, cooldown_h=0)
    soak(client, 5, safe=False)  # fills the window; unsafe reports never water
    post(client, "/command", "c=b1 water=7 ml=10 cap_s=5")

    first = report(client)  # the manual command rides out; rules step aside
    assert "water=7" in first.text
    assert len(commands(db)) == 1

    retry = report(client)  # manual expired unacked; rules take the free slot
    assert "water=3 ml=100" in retry.text
    assert [c[2] for c in commands(db)] == ["manual", "rules"]


# --------------------------------------------------------------------------- #
# Learning: propose, approve, verdict
# --------------------------------------------------------------------------- #


def test_learning_proposes_instead_of_watering(client, db):
    make_pot(client, mode="learning")
    last = soak(client, 5)

    assert "cmd=" not in last.text  # a proposal is not a hand-off
    assert commands(db) == [(1, "proposed", "rules", 100, 10, 3)]
    (entry,) = client.get("/pots").json()["pots"]
    assert entry["proposal"]["id"] == 1
    assert entry["proposal"]["ml"] == 100


def test_dry_reports_do_not_pile_up_proposals(client, db):
    make_pot(client, mode="learning")
    soak(client, 9)
    assert len(commands(db)) == 1


def test_approved_proposal_is_handed_acked_and_verdicted(client, db):
    make_pot(client, mode="learning")
    soak(client, 5)

    assert post(client, "/approve", "cmd=1").text == "cmd=1\n"
    handed = report(client)
    assert "cmd=1 water=3 ml=100 cap_s=10" in handed.text
    report(client, extra="ack=1 flow_ml=97")

    answer = post(client, "/verdict", "cmd=1 verdict=too_much")
    assert answer.text == "cmd=1 verdict=too_much\n"
    post(client, "/verdict", "cmd=1 verdict=ok")  # second look replaces
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT command_id, verdict FROM verdicts").fetchall() == [
            (1, "ok")
        ]


def test_approval_restarts_the_queued_ttl_clock(client, db):
    make_pot(client, mode="learning")
    soak(client, 5)
    with sqlite3.connect(db) as con:  # the human took 800 s to walk over
        con.execute("UPDATE commands SET created_ts = created_ts - 800")

    post(client, "/approve", "cmd=1")
    handed = report(client)  # NOT swept as a stale queued command
    assert "cmd=1" in handed.text


def test_an_unapproved_proposal_expires(client, db):
    make_pot(client, mode="learning")
    soak(client, 5)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE commands SET created_ts = created_ts - 7300")

    report(client)
    assert commands(db)[0][1] == "expired"
    assert post(client, "/approve", "cmd=1").status_code == 400


def test_a_stale_proposal_from_a_dark_board_cannot_be_approved(client, db):
    # The sweep runs on the board's own reports; a dark board never sweeps,
    # so /approve must enforce the TTL itself.
    make_pot(client, mode="learning")
    soak(client, 5)
    with sqlite3.connect(db) as con:  # the board goes dark for three days
        con.execute("UPDATE commands SET created_ts = created_ts - 259200")

    (entry,) = client.get("/pots").json()["pots"]
    assert entry["proposal"] is None  # not advertised past its TTL
    assert post(client, "/approve", "cmd=1").status_code == 400
    assert commands(db)[0][1] == "expired"


def test_a_dead_boards_abandoned_command_does_not_wedge_approval(client, db):
    make_pot(client, mode="learning")
    soak(client, 5)  # proposal cmd=1
    post(client, "/command", "c=b1 stop=1")  # cmd=2 queued; the board dies
    with sqlite3.connect(db) as con:
        con.execute("UPDATE commands SET created_ts = created_ts - 1000 WHERE id = 2")

    assert post(client, "/approve", "cmd=1").status_code == 200


def test_a_channel_gone_silent_errs_dry(client, db):
    make_pot(client)
    soak(client, 5, safe=False)  # a dry window builds; unsafe, so no water
    silent = post(client, "/report", "c=b1 ch5=9999 float=1 pos=ok")
    assert silent.status_code == 200
    assert commands(db) == []  # no fresh ch0 reading: the stale window holds

    assert "cmd=1" in report(client).text  # the sensor speaks again: waters


def test_the_cap_credits_a_dose_that_underflowed(client, db):
    # The meter says 40 of the asked 100 flowed: 40 + 100 <= 150.
    make_pot(client, cooldown_h=0, daily_cap_ml=150)
    soak(client, 5)

    ack = report(client, extra="ack=1 flow_ml=40")
    assert "cmd=2" in ack.text


def test_a_handed_but_unacked_dose_counts_its_full_ml(client, db):
    # The board may have watered without acking: assume the whole dose ran.
    make_pot(client, cooldown_h=0, daily_cap_ml=150)
    soak(client, 5)  # cmd=1 handed

    last = report(client)  # no ack: flow unknown, so 100 + 100 > 150
    assert "cmd=" not in last.text
    assert len(commands(db)) == 1


def test_approve_refusals(client, db):
    make_pot(client, mode="learning")
    soak(client, 5)
    post(client, "/command", "c=b1 stop=1")  # a manual command holds the slot

    busy = post(client, "/approve", "cmd=1")
    assert busy.status_code == 409
    assert "state=queued" in busy.text
    assert post(client, "/approve", "cmd=99").status_code == 400
    assert post(client, "/approve", "nonsense").status_code == 400
    assert post(client, "/approve", "cmd=1", token="nope").status_code == 401


def test_verdict_refusals(client, db):
    make_pot(client, mode="learning")
    soak(client, 5)

    early = post(client, "/verdict", "cmd=1 verdict=ok")  # never handed
    assert early.status_code == 400
    assert "never handed" in early.text
    assert post(client, "/verdict", "cmd=1 verdict=maybe").status_code == 400
    assert post(client, "/verdict", "cmd=1").status_code == 400
    assert post(client, "/verdict", "verdict=ok").status_code == 400
    assert post(client, "/verdict", "cmd=1 verdict=ok", token="nope").status_code == 401
