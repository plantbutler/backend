"""Tell me when it's wrong: the alert rules, the tick, the wire, the ticker."""

import http.server
import sqlite3
import threading
import time
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from butler import (
    FLAP_WINDOW_S,
    PERSIST_S,
    PROPOSAL_NUDGE_S,
    REALERT_FLOOR_S,
    SOAK_S,
    UP_AFTER_S,
    UP_PROBE_FLOOR_S,
    Alert,
    create_app,
    post_ntfy,
)

TOKEN = "test-token"
DRY = 11000  # pct 12 with the calibration below
WET = 8000  # pct 50


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def sent():
    return []


@pytest.fixture
def pinged():
    return []


def build_app(db, sent, pinged, **over):
    settings = {
        "db_path": str(db),
        "token": TOKEN,
        "next_s": 60,
        "cmd_ttl_s": 900,
        "quiet": "0-0",
        "send": lambda alert: sent.append(alert) or True,
        "ping": lambda: pinged.append(True) or True,
    } | over
    return create_app(**settings)


@pytest.fixture
def app(db, sent, pinged):
    return build_app(db, sent, pinged)


@pytest.fixture
def client(app):
    return TestClient(app)


def tick(app, now=None):
    return app.state.tick(now)


def see_everything(app):
    # Silence is measured from the later of last_seen and the butler's own
    # observation start, so a test that backdates last_seen must backdate
    # the observation window too.
    app.state.observed["since"] = 0


def post(client, path, body):
    return client.post(path, content=body, headers={"X-Token": TOKEN})


def report(client, raw=DRY, safe=True, extra=""):
    body = f"c=b1 ch0={raw}"
    if safe:
        body += " float=1 pos=ok"
    if extra:
        body += f" {extra}"
    answer = post(client, "/report", body)
    assert answer.status_code == 200, answer.text
    return answer


def make_pot(client, **over):
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
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    answer = post(client, "/pot", body)
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def run_sql(db, sql, *params):
    with sqlite3.connect(db) as con:
        return con.execute(sql, params).fetchall()


def alert_rows(db):
    return run_sql(
        db,
        "SELECT key, detail FROM alerts WHERE key NOT LIKE 'meta:%' ORDER BY key",
    )


def dose_rows(db):
    return run_sql(
        db, "SELECT key, detail FROM alerts WHERE key LIKE 'dose:%' ORDER BY key"
    )


def age_controller(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE controllers SET last_seen = last_seen - ?", (seconds,))


def age_status(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE status SET float_since = float_since - ?, "
            "pos_since = pos_since - ?, float_bad = float_bad - ?, "
            "float_bad_prev = float_bad_prev - ?, pos_bad = pos_bad - ?, "
            "pos_bad_prev = pos_bad_prev - ?",
            (seconds,) * 6,
        )


def keys(sent):
    return [a.key for a in sent if a.message is not None]


def plant_dose(
    db,
    *,
    state="acked",
    ml=100,
    flow_ml=None,
    sent_ago=SOAK_S + 600,
    acked_ago=SOAK_S + 590,
    before_raw=DRY,
    after_raw=None,
    outlet=3,
    channel=0,
):
    """One executed dose and its readings, timestamps fully controlled."""
    now = int(time.time())
    sent_ts = now - sent_ago
    acked_ts = None if acked_ago is None else now - acked_ago
    with sqlite3.connect(db) as con:
        # A pot is wired before it is watered. make_pot stamps its mapping
        # window milliseconds ago and the dose is planted well in the past,
        # so the window has to move back with it or the dose belongs to no
        # pot at all and the judgement has nothing to read.
        con.execute(
            "UPDATE pot_mappings SET from_ts = ? WHERE to_ts IS NULL AND from_ts > ?",
            (sent_ts - 10, sent_ts - 10),
        )
        con.execute(
            "INSERT INTO commands (created_ts, controller, kind, outlet, ml, "
            "cap_s, state, source, sent_ts, acked_ts, flow_ml) "
            "VALUES (?, 'b1', 'water', ?, ?, 10, ?, 'rules', ?, ?, ?)",
            (sent_ts - 5, outlet, ml, state, sent_ts, acked_ts, flow_ml),
        )
        for i in range(5):  # the window the dose was decided on
            con.execute(
                "INSERT INTO readings (ts, controller, channel, raw) "
                "VALUES (?, 'b1', ?, ?)",
                (sent_ts - 4 + i, channel, before_raw),
            )
        if after_raw is not None and acked_ts is not None:
            for i in range(5):  # what the sensor said once the water soaked
                con.execute(
                    "INSERT INTO readings (ts, controller, channel, raw) "
                    "VALUES (?, 'b1', ?, ?)",
                    (acked_ts + 60 * (i + 1), channel, after_raw),
                )


# --------------------------------------------------------------------------- #
# A controller that went quiet
# --------------------------------------------------------------------------- #


def test_a_silent_controller_raises_once_after_the_threshold(app, client, db, sent):
    report(client)
    see_everything(app)
    now = int(time.time())
    age_controller(db, 599)
    assert tick(app, now) is True
    assert sent == []

    age_controller(db, 2)  # 601 s of silence: over max(600, 3 * 60)
    assert tick(app, now) is True
    assert keys(sent) == ["silent:b1"]
    assert sent[0].priority == "high"
    assert "b1" in sent[0].message

    tick(app, now)
    assert keys(sent) == ["silent:b1"]  # raised once, not once per tick


def test_a_returning_controller_clears_once_and_reflaps_are_floored(
    app, client, db, sent
):
    report(client)
    see_everything(app)
    now = int(time.time())
    age_controller(db, 700)
    tick(app, now)

    report(client)  # it is back
    tick(app, now)
    assert [a.key for a in sent] == ["silent:b1", "silent:b1"]
    assert sent[1].priority == "default"
    assert "reporting again" in sent[1].message

    age_controller(db, 700)  # flaps right back down, inside the floor
    tick(app, now)
    assert len(sent) == 2  # no third page within REALERT_FLOOR_S

    with sqlite3.connect(db) as con:  # the hour passes
        con.execute(
            "UPDATE alerts SET cleared_ts = cleared_ts - ? WHERE key = 'silent:b1'",
            (REALERT_FLOOR_S,),
        )
    tick(app, now)
    assert len(sent) == 3


def test_the_silent_threshold_scales_with_a_slow_report_interval(app, client, db, sent):
    report(client)
    post(client, "/interval", "c=b1 next=300")  # threshold becomes 3 * 300
    see_everything(app)
    now = int(time.time())
    age_controller(db, 700)  # over 600, under 900
    tick(app, now)
    assert sent == []

    age_controller(db, 300)
    tick(app, now)
    assert keys(sent) == ["silent:b1"]


def test_the_butlers_own_downtime_is_not_the_boards_silence(app, client, db, sent):
    report(client)
    age_controller(db, 5000)
    tick(app, int(time.time()))  # observation started with the process
    assert sent == []


def test_a_long_tick_gap_resets_the_observation_window(app, client, db, sent):
    report(client)
    see_everything(app)
    now = int(time.time())
    tick(app, now - 5000)
    age_controller(db, 5000)
    tick(app, now)  # a 5000 s gap between ticks: the butler was away
    assert sent == []


def test_butler_downtime_does_not_fake_a_reporting_again_clear(app, client, db, sent):
    report(client)
    see_everything(app)
    now = int(time.time())
    age_controller(db, 700)
    tick(app, now)
    assert keys(sent) == ["silent:b1"]

    app.state.observed["since"] = now  # the butler just woke; board still dead
    tick(app, now)
    assert len(sent) == 1  # no false "reporting again"
    assert run_sql(db, "SELECT cleared_ts FROM alerts WHERE key = 'silent:b1'") == [
        (None,)
    ]


def test_the_observation_window_survives_a_short_restart(db, sent, pinged):
    now = int(time.time())
    build_app(db, sent, pinged)  # bootstraps the schema
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO alerts (key, raised_ts, cleared_ts, detail) "
            "VALUES ('meta:tick', ?, NULL, '0')",
            (now - 30,),
        )
    fresh = build_app(db, sent, pinged)  # a 30 s restart
    assert fresh.state.observed["since"] == 0  # inherited, not reset

    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE alerts SET raised_ts = ? WHERE key = 'meta:tick'", (now - 5000,)
        )
    stale = build_app(db, sent, pinged)  # real downtime: a fresh window
    assert stale.state.observed["since"] >= now - 2


# --------------------------------------------------------------------------- #
# The board said so itself: float and position
# --------------------------------------------------------------------------- #


def test_one_reservoir_blip_is_slosh_two_are_empty(app, client, db, sent):
    report(client, safe=False, extra="float=0 pos=ok")
    tick(app, int(time.time()))
    assert sent == []  # a single blip at the waterline is slosh

    report(client, safe=False, extra="float=1 pos=ok")  # flapping...
    report(client, safe=False, extra="float=0 pos=ok")  # ...still means empty
    tick(app, int(time.time()))
    assert keys(sent) == ["float:b1"]
    assert sent[0].priority == "high"
    assert "reservoir" in sent[0].message

    tick(app, int(time.time()))
    assert keys(sent) == ["float:b1"]  # once


def test_a_full_reservoir_clears_after_a_quiet_window_and_reflaps_floor(
    app, client, db, sent
):
    report(client, safe=False, extra="float=0 pos=ok")
    report(client, safe=False, extra="float=0 pos=ok")
    now = int(time.time())
    tick(app, now)
    assert keys(sent) == ["float:b1"]

    report(client)  # full again, but the bad sightings are still recent
    tick(app, now)
    assert len(sent) == 1

    age_status(db, FLAP_WINDOW_S + 1)  # a quiet window passes
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["float:b1", "float:b1"]
    assert "full again" in sent[1].message

    report(client, safe=False, extra="float=0 pos=ok")  # flaps right back
    report(client, safe=False, extra="float=0 pos=ok")
    tick(app, int(time.time()))
    assert len(sent) == 2  # inside the hourly floor: no third page

    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE alerts SET cleared_ts = cleared_ts - ? WHERE key = 'float:b1'",
            (REALERT_FLOOR_S,),
        )
    tick(app, int(time.time()))
    assert len(sent) == 3


def test_a_lost_manifold_position_raises_and_clears(app, client, db, sent):
    report(client, safe=False, extra="float=1 pos=unknown")
    report(client, safe=False, extra="float=1 pos=unknown")
    tick(app, int(time.time()))
    assert keys(sent) == ["pos:b1"]
    assert "manifold" in sent[0].message

    report(client)
    age_status(db, FLAP_WINDOW_S + 1)
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["pos:b1", "pos:b1"]
    assert sent[1].priority == "default"


def test_a_report_without_safety_fields_neither_raises_nor_clears(
    app, client, db, sent
):
    report(client, safe=False)  # the firmware does not send float=/pos= yet
    age_status(db, 10000)
    tick(app, int(time.time()))
    assert sent == []


def test_a_vanished_safety_field_is_its_own_alarm(app, client, db, sent):
    report(client)  # float= and pos= both seen once
    report(client, safe=False)  # and then both gone
    tick(app, int(time.time()))
    assert sent == []  # must persist first: one stripped report is a fluke

    age_status(db, PERSIST_S + 1)
    tick(app, int(time.time()))
    assert sorted(keys(sent)) == ["fields:float:b1", "fields:pos:b1"]
    assert all(a.priority == "high" for a in sent)

    report(client)  # the fields are back
    age_status(db, PERSIST_S + 1)
    tick(app, int(time.time()))
    assert len(sent) == 4
    assert {a.key for a in sent[2:]} == {"fields:float:b1", "fields:pos:b1"}
    assert "again" in sent[-1].message


def test_float_since_moves_only_when_the_value_changes(client, db):
    report(client)
    age_status(db, 500)
    report(client)  # same values: since must not move
    ((f_since, p_since),) = run_sql(db, "SELECT float_since, pos_since FROM status")
    now = int(time.time())
    assert f_since <= now - 490 and p_since <= now - 490

    report(client, safe=False, extra="float=0 pos=ok")  # float flips, pos holds
    ((f_since, p_since),) = run_sql(db, "SELECT float_since, pos_since FROM status")
    assert f_since >= now - 2
    assert p_since <= now - 490


# --------------------------------------------------------------------------- #
# A sensor that went quiet while its controller stayed healthy
# --------------------------------------------------------------------------- #


def test_a_dead_sensor_channel_pages_while_the_controller_reports(
    app, client, db, sent
):
    make_pot(client, mode="manual")
    report(client)
    see_everything(app)
    with sqlite3.connect(db) as con:  # the wire comes loose; the board go on
        con.execute("UPDATE readings SET ts = ts - 700")
    tick(app, int(time.time()))
    assert keys(sent) == ["sensor:b1:0"]
    assert sent[0].priority == "high"
    assert "basil" in sent[0].message

    report(client)  # the wire is back
    tick(app, int(time.time()))
    assert keys(sent) == ["sensor:b1:0", "sensor:b1:0"]
    assert "back" in sent[1].message


def test_a_silent_controller_does_not_double_page_its_sensors(app, client, db, sent):
    make_pot(client, mode="manual")
    report(client)
    see_everything(app)
    age_controller(db, 700)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE readings SET ts = ts - 700")
    tick(app, int(time.time()))
    assert keys(sent) == ["silent:b1"]  # one page, not one per sensor


# --------------------------------------------------------------------------- #
# Every dose is judged exactly once
# --------------------------------------------------------------------------- #


def test_a_dose_that_worked_is_recorded_silently(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=95, after_raw=WET)  # 12% -> 50%
    tick(app, int(time.time()))
    assert sent == []  # tell me when it's WRONG
    assert dose_rows(db) == [("dose:1", "ok")]

    tick(app, int(time.time()))
    assert dose_rows(db) == [("dose:1", "ok")]  # judged once


def test_a_dose_with_no_moisture_rise_alerts_at_default_priority(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=95, after_raw=DRY + 100)  # 12% -> 11%
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["dose:1"]
    assert sent[0].priority == "default"  # the bench rig has not spoken yet
    assert "moisture went" in sent[0].message
    assert dose_rows(db) == [("dose:1", "failed")]


def test_a_dose_is_judged_for_the_pot_that_got_it(app, client, db, sent):
    """The hoses are swapped after the dose. The page must still name the
    pot that was watered and read that pot's own sensor, or it reports a
    failure against a plant that was never dosed."""
    basil = make_pot(client)  # channel 0, outlet 3
    plant_dose(db, flow_ml=95, after_raw=DRY + 100)  # basil did not drink it
    post(client, "/pot", f"id={basil} channel=1 outlet=4")
    post(client, "/pot", "name=mint controller=b1 channel=0 outlet=3")

    tick(app, int(time.time()))

    assert [a.key for a in sent] == ["dose:1"]
    assert "basil" in sent[0].message
    assert "mint" not in sent[0].message
    assert "moisture went" in sent[0].message  # judged on basil's own window


def test_a_dose_short_on_the_meter_alerts_high(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=12, after_raw=WET)
    tick(app, int(time.time()))
    assert sent[0].priority == "high"
    assert "12 of 100 ml" in sent[0].message


def test_a_dose_never_acknowledged_alerts_high_and_immediately(app, client, db, sent):
    make_pot(client)
    plant_dose(db, state="expired", acked_ago=None, sent_ago=90)  # 90 s ago
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["dose:1"]  # no pointless soak wait
    assert sent[0].priority == "high"
    assert "never acknowledged" in sent[0].message


def test_a_dose_is_not_judged_before_its_soak_is_over(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=95, sent_ago=60, acked_ago=50)
    tick(app, int(time.time()))
    assert sent == []
    assert dose_rows(db) == []


def test_the_soak_scales_with_a_slow_report_interval(db, sent, pinged):
    app = build_app(db, sent, pinged, cmd_ttl_s=7200)
    client = TestClient(app)
    make_pot(client, mode="manual")
    report(client)
    post(client, "/interval", "c=b1 next=1800")  # the soak becomes 3 * 1800
    plant_dose(db, flow_ml=95, sent_ago=2010, acked_ago=2000)  # past SOAK_S
    tick(app, int(time.time()))
    assert dose_rows(db) == []  # a slow reporter's window is still open


def test_a_dose_on_an_unmapped_outlet_still_reports_short_flow(app, client, db, sent):
    plant_dose(db, flow_ml=12)  # no pot anywhere
    tick(app, int(time.time()))
    assert "outlet 3" in sent[0].message
    assert sent[0].priority == "high"


def test_an_already_wet_pot_has_no_headroom_and_does_not_cry_wolf(
    app, client, db, sent
):
    make_pot(client)
    plant_dose(db, flow_ml=95, before_raw=4100, after_raw=4100)  # 98% -> 98%
    tick(app, int(time.time()))
    assert sent == []
    assert dose_rows(db) == [("dose:1", "ok")]


def test_a_dose_with_no_evidence_is_unverified_not_ok(app, client, db, sent):
    make_pot(client)
    plant_dose(db)  # acked, but no meter number and no post-ack readings
    tick(app, int(time.time()))
    assert sent == []
    assert dose_rows(db) == [("dose:1", "unverified")]


def test_doses_older_than_a_day_are_history_not_news(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=12, sent_ago=90000, acked_ago=89990)
    tick(app, int(time.time()))
    assert sent == []
    assert dose_rows(db) == []


def test_correlated_dose_failures_page_once_per_controller(app, client, db, sent):
    make_pot(client)
    make_pot(client, name="mint", channel=1, outlet=4)
    plant_dose(db, flow_ml=10, after_raw=WET)  # the shared pump died:
    plant_dose(db, flow_ml=10, after_raw=WET, outlet=4, channel=1)
    tick(app, int(time.time()))

    paged = [a for a in sent if a.message is not None]
    assert len(paged) == 1  # one page for the controller, not one per pot
    assert dose_rows(db) == [("dose:1", "failed"), ("dose:2", "failed")]

    plant_dose(db, flow_ml=10, after_raw=WET)  # a third failure, same hour
    tick(app, int(time.time()))
    assert len([a for a in sent if a.message is not None]) == 1
    assert dose_rows(db)[-1] == ("dose:3", "failed")


# --------------------------------------------------------------------------- #
# A learning proposal nobody is polling /pots for
# --------------------------------------------------------------------------- #


def test_a_waiting_proposal_nudges_once_a_day_keyed_on_the_hose(app, client, db, sent):
    make_pot(client, mode="learning")
    for _ in range(5):
        report(client)
    assert run_sql(db, "SELECT state FROM commands") == [("proposed",)]

    now = int(time.time())
    tick(app, now)
    assert [a.key for a in sent] == ["proposal:b1:3"]
    assert "basil" in sent[0].message and "100 ml" in sent[0].message
    tick(app, now)
    assert len(sent) == 1  # one nudge, not one per tick

    # The proposal expires and respawns under a fresh id while the pot stays
    # dry; the hose key means that is still the same, single nudge.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE commands SET state = 'expired'")
    report(client)
    assert run_sql(db, "SELECT state FROM commands ORDER BY id DESC LIMIT 1") == [
        ("proposed",)
    ]
    tick(app, now)
    assert len(sent) == 1

    with sqlite3.connect(db) as con:  # a day passes
        con.execute(
            "UPDATE alerts SET raised_ts = raised_ts - ? WHERE key LIKE 'proposal:%'",
            (PROPOSAL_NUDGE_S,),
        )
    tick(app, now)
    assert len(sent) == 2


def rewind(db, seconds):
    """Everything so far moves that far into the past, so that a remap in
    the next line does not land in the same second as the proposal."""
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE commands SET created_ts = created_ts - ?, "
            "sent_ts = sent_ts - ?, acked_ts = acked_ts - ?",
            (seconds,) * 3,
        )
        con.execute(
            "UPDATE pot_mappings SET from_ts = from_ts - ?, to_ts = to_ts - ?",
            (seconds, seconds),
        )


def test_a_channel_correction_does_not_mute_the_proposal_nudge(app, client, db, sent):
    """The nudge is about a hose, and correcting a miswired SENSOR channel
    does not move the hose. It opens a new mapping window all the same, so
    a nudge fenced on that window goes quiet exactly when it is needed:
    during setup, which is when channels get corrected and proposals are
    sitting on the card waiting for a human."""
    basil = make_pot(client, mode="learning")
    for _ in range(5):
        report(client)
    assert run_sql(db, "SELECT id, state FROM commands") == [(1, "proposed")]
    rewind(db, 600)  # ten minutes later, the channel is corrected

    post(client, "/pot", f"id={basil} channel=1")

    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["proposal:b1:3"]
    assert "basil" in sent[0].message and "proposal 1" in sent[0].message


# --------------------------------------------------------------------------- #
# The tick: send-then-record, the dead-man, the probes
# --------------------------------------------------------------------------- #


def test_a_quiet_clean_tick_pings_the_dead_man(app, client, db, sent, pinged):
    assert tick(app, int(time.time())) is True
    assert sent == []
    assert pinged == [True]


def test_a_quiet_pass_still_proves_ntfy_reachable(db, pinged):
    reachable = {"ok": False}
    app = build_app(db, [], pinged, probe=lambda: reachable["ok"])

    assert tick(app, int(time.time())) is False  # ntfy dark, nothing to send
    assert pinged == []  # days of quiet outage must still stop the dead-man

    reachable["ok"] = True
    assert tick(app, int(time.time())) is True
    assert pinged == [True]


def test_a_pass_that_sent_something_needs_no_extra_proof(db, sent, pinged):
    app = build_app(db, sent, pinged, probe=lambda: False)  # probe would fail
    client = TestClient(app)
    report(client)
    see_everything(app)
    age_controller(db, 700)

    assert tick(app, int(time.time())) is True  # the send itself was proof
    assert keys(sent) == ["silent:b1"]
    assert pinged == [True]


def test_a_failed_send_leaves_no_row_no_ping_and_is_retried(db, sent, pinged):
    calls = {"n": 0}

    def flaky(alert):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        sent.append(alert)
        return True

    app = build_app(db, sent, pinged, send=flaky)
    client = TestClient(app)
    report(client)
    see_everything(app)
    age_controller(db, 700)
    now = int(time.time())

    assert tick(app, now) is False
    assert alert_rows(db) == []  # not recorded: retried
    assert pinged == []  # an unreachable ntfy must trip the dead man

    assert tick(app, now) is True
    assert [a.key for a in sent] == ["silent:b1"]
    assert pinged == [True]


def test_the_first_failed_send_stops_the_loop(db):
    tried = []
    app = build_app(db, [], [], send=lambda alert: tried.append(alert.key) and False)
    client = TestClient(app)
    post(client, "/report", f"c=b1 ch0={DRY}")
    post(client, "/report", f"c=b2 ch0={DRY}")
    see_everything(app)
    age_controller(db, 700)

    assert tick(app, int(time.time())) is False
    assert len(tried) == 1  # everything behind it fails the same way


def test_the_up_probe_goes_out_once_after_ten_minutes(app, client, db, sent):
    now = int(time.time())
    tick(app, now + UP_AFTER_S + 1)
    assert [a.key for a in sent] == [None]
    assert sent[0].priority == "min"
    assert "up" in sent[0].message

    tick(app, now + UP_AFTER_S + 2)
    assert len(sent) == 1  # one probe per process


def test_the_up_probe_is_floored_daily_across_restarts(db, pinged):
    now = int(time.time())
    first_sent = []
    first = build_app(db, first_sent, pinged)
    tick(first, now + UP_AFTER_S + 1)
    assert [a.key for a in first_sent] == [None]

    second_sent = []
    second = build_app(db, second_sent, pinged)  # a 15-min crash loop...
    tick(second, now + UP_AFTER_S + 10)
    assert second_sent == []  # ...must not probe once per incarnation

    with sqlite3.connect(db) as con:  # a day passes
        con.execute(
            "UPDATE alerts SET raised_ts = raised_ts - ? WHERE key = 'meta:up'",
            (UP_PROBE_FLOOR_S,),
        )
    third_sent = []
    third = build_app(db, third_sent, pinged)
    tick(third, now + UP_AFTER_S + 20)
    assert [a.key for a in third_sent] == [None]


# --------------------------------------------------------------------------- #
# Refusals to start
# --------------------------------------------------------------------------- #


def test_a_dead_man_without_a_topic_refuses_to_serve(db):
    with pytest.raises(ValueError, match="BUTLER_DEADMAN_URL"):
        create_app(db_path=str(db), token=TOKEN, deadman_url="http://nas/ping")


def test_a_malformed_silent_threshold_refuses_with_its_name(db, monkeypatch):
    monkeypatch.setenv("BUTLER_SILENT_S", "ten")
    with pytest.raises(ValueError, match="BUTLER_SILENT_S"):
        create_app(db_path=str(db), token=TOKEN)


# --------------------------------------------------------------------------- #
# /health shows the safety fields and what stands raised
# --------------------------------------------------------------------------- #


def test_health_shows_safety_fields_and_raised_conditions(app, client, db, sent):
    report(client, safe=False, extra="float=0 pos=unknown")
    report(client, safe=False, extra="float=0 pos=unknown")
    tick(app, int(time.time()))

    health = client.get("/health").json()
    (b1,) = health["controllers"]
    assert b1["float"] == 0 and b1["pos"] == "unknown"
    assert sorted(a["key"] for a in health["alerts"]) == ["float:b1", "pos:b1"]

    report(client)  # full and homed again
    age_status(db, FLAP_WINDOW_S + 1)
    tick(app, int(time.time()))
    assert client.get("/health").json()["alerts"] == []


# --------------------------------------------------------------------------- #
# The wire itself, against a real local server
# --------------------------------------------------------------------------- #


class _Recorder(http.server.BaseHTTPRequestHandler):
    status = 200
    requests: ClassVar[list] = []

    def _serve(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).requests.append((self.path, dict(self.headers), body, method))
        self.send_response(type(self).status)
        if type(self).status in (301, 302):
            self.send_header("Location", "/elsewhere")
        self.end_headers()

    def do_POST(self):
        self._serve("POST")

    def do_GET(self):
        self._serve("GET")

    def log_message(self, *args):
        pass


@pytest.fixture
def wire():
    _Recorder.requests = []
    _Recorder.status = 200
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Recorder
    server.shutdown()


def test_post_ntfy_speaks_the_ntfy_wire_format(wire):
    url, recorder = wire
    alert = Alert("k", "high", "warning", "the reservoir on b1 is empty")
    assert post_ntfy(url, "garden-abc", alert) is True
    ((path, headers, body, method),) = recorder.requests
    assert (path, method, body) == (
        "/garden-abc",
        "POST",
        "the reservoir on b1 is empty",
    )
    assert headers["Title"] == "Plant Butler"
    assert headers["Priority"] == "high"
    assert headers["Tags"] == "warning"


def test_post_ntfy_returns_false_on_5xx_redirect_and_dead_server(wire):
    url, recorder = wire
    recorder.status = 500
    assert post_ntfy(url, "t", Alert("k", "high", "w", "m")) is False
    # A redirected POST would be replayed as a bodyless GET and the message
    # silently dropped: refusing to follow makes it a loud failure instead.
    recorder.status = 302
    assert post_ntfy(url, "t", Alert("k", "high", "w", "m")) is False
    assert post_ntfy("http://127.0.0.1:1", "t", Alert("k", "high", "w", "m")) is False


def test_the_real_ticker_evaluates_sends_and_pings(db, wire):
    url, recorder = wire
    app = create_app(
        db_path=str(db),
        token=TOKEN,
        next_s=60,
        cmd_ttl_s=900,
        quiet="0-0",
        ntfy_topic="garden-test",
        ntfy_url=url,
        deadman_url=f"{url}/ping",
        tick_s=0.05,
    )
    with TestClient(app) as client:  # the lifespan starts the real ticker
        report(client, safe=False, extra="float=0 pos=ok")
        report(client, safe=False, extra="float=0 pos=ok")
        deadline = time.time() + 5
        while time.time() < deadline and not any(
            r for r in recorder.requests if r[3] == "POST" and "reservoir" in r[2]
        ):
            time.sleep(0.02)

    posts = [r for r in recorder.requests if r[3] == "POST"]
    pings = [r for r in recorder.requests if r[3] == "GET" and r[0] == "/ping"]
    assert posts, "the ticker never posted the raised alert"
    assert posts[0][0] == "/garden-test"
    assert "reservoir" in posts[0][2]
    assert pings, "a clean tick should feed the dead man"
