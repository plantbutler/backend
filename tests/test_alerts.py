"""Tell me when it's wrong: the alert rules, the tick, the wire, the ticker."""

import http.server
import sqlite3
import threading
import time
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from butler import (
    PERSIST_S,
    PROPOSAL_NUDGE_S,
    REALERT_FLOOR_S,
    SOAK_S,
    UP_AFTER_S,
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


@pytest.fixture
def app(db, sent, pinged):
    return create_app(
        db_path=str(db),
        token=TOKEN,
        next_s=60,
        cmd_ttl_s=900,
        quiet="0-0",
        send=lambda alert: sent.append(alert) or True,
        ping=lambda: pinged.append(True) or True,
    )


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


def run_sql(db, sql, *params):
    with sqlite3.connect(db) as con:
        return con.execute(sql, params).fetchall()


def age_controller(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE controllers SET last_seen = last_seen - ?", (seconds,))


def age_status(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE status SET float_since = float_since - ?, "
            "pos_since = pos_since - ?",
            (seconds, seconds),
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
):
    """One executed dose and its readings, timestamps fully controlled."""
    now = int(time.time())
    sent_ts = now - sent_ago
    acked_ts = None if acked_ago is None else now - acked_ago
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO commands (created_ts, controller, kind, outlet, ml, "
            "cap_s, state, source, sent_ts, acked_ts, flow_ml) "
            "VALUES (?, 'b1', 'water', ?, ?, 10, ?, 'rules', ?, ?, ?)",
            (sent_ts - 5, outlet, ml, state, sent_ts, acked_ts, flow_ml),
        )
        for i in range(5):  # the window the dose was decided on
            con.execute(
                "INSERT INTO readings (ts, controller, channel, raw) "
                "VALUES (?, 'b1', 0, ?)",
                (sent_ts - 4 + i, before_raw),
            )
        if after_raw is not None and acked_ts is not None:
            for i in range(5):  # what the sensor said once the water soaked
                con.execute(
                    "INSERT INTO readings (ts, controller, channel, raw) "
                    "VALUES (?, 'b1', 0, ?)",
                    (acked_ts + 60 * (i + 1), after_raw),
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
        con.execute("UPDATE alerts SET cleared_ts = cleared_ts - ?", (REALERT_FLOOR_S,))
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


# --------------------------------------------------------------------------- #
# The board said so itself: float and position, debounced
# --------------------------------------------------------------------------- #


def test_an_empty_reservoir_raises_and_clears_only_after_persisting(
    app, client, db, sent
):
    report(client, safe=False, extra="float=0 pos=ok")
    now = int(time.time())
    tick(app, now)
    assert sent == []  # a float bouncing at the waterline must persist first

    age_status(db, PERSIST_S + 1)
    tick(app, now)
    assert keys(sent) == ["float:b1"]
    assert sent[0].priority == "high"
    tick(app, now)
    assert keys(sent) == ["float:b1"]  # once

    report(client, safe=False, extra="float=1 pos=ok")  # refilled: since resets
    tick(app, int(time.time()))
    assert len(sent) == 1  # the good state must persist too

    age_status(db, PERSIST_S + 1)
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["float:b1", "float:b1"]
    assert "full again" in sent[1].message


def test_a_lost_manifold_position_raises_and_clears(app, client, db, sent):
    report(client, safe=False, extra="float=1 pos=unknown")
    age_status(db, PERSIST_S + 1)
    tick(app, int(time.time()))
    assert keys(sent) == ["pos:b1"]
    assert "manifold" in sent[0].message

    report(client)
    age_status(db, PERSIST_S + 1)
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
# Every dose is judged exactly once, a soak later
# --------------------------------------------------------------------------- #


def test_a_dose_that_worked_is_recorded_silently(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=95, after_raw=WET)  # 12% -> 50%
    tick(app, int(time.time()))
    assert sent == []  # tell me when it's WRONG
    assert run_sql(db, "SELECT key, detail FROM alerts") == [("dose:1", "ok")]

    tick(app, int(time.time()))
    assert run_sql(db, "SELECT COUNT(*) FROM alerts") == [(1,)]  # judged once


def test_a_dose_with_no_moisture_rise_alerts_at_default_priority(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=95, after_raw=DRY + 100)  # 12% -> 11%
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["dose:1"]
    assert sent[0].priority == "default"  # the bench rig has not spoken yet
    assert "moisture went" in sent[0].message
    assert run_sql(db, "SELECT detail FROM alerts") == [("failed",)]


def test_a_dose_short_on_the_meter_alerts_high(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=12, after_raw=WET)
    tick(app, int(time.time()))
    assert sent[0].priority == "high"
    assert "12 of 100 ml" in sent[0].message


def test_a_dose_never_acknowledged_alerts_high(app, client, db, sent):
    make_pot(client)
    plant_dose(db, state="expired", acked_ago=None)
    tick(app, int(time.time()))
    assert [a.key for a in sent] == ["dose:1"]
    assert sent[0].priority == "high"
    assert "never acknowledged" in sent[0].message


def test_a_dose_is_not_judged_before_its_soak_is_over(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=95, sent_ago=60, acked_ago=50)
    tick(app, int(time.time()))
    assert sent == []
    assert run_sql(db, "SELECT key FROM alerts") == []


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
    assert run_sql(db, "SELECT detail FROM alerts") == [("ok",)]


def test_doses_older_than_a_day_are_history_not_news(app, client, db, sent):
    make_pot(client)
    plant_dose(db, flow_ml=12, sent_ago=90000, acked_ago=89990)
    tick(app, int(time.time()))
    assert sent == []
    assert run_sql(db, "SELECT key FROM alerts") == []


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
        con.execute("UPDATE alerts SET raised_ts = raised_ts - ?", (PROPOSAL_NUDGE_S,))
    tick(app, now)
    assert len(sent) == 2


# --------------------------------------------------------------------------- #
# The tick: send-then-record, the dead-man, the up-probe
# --------------------------------------------------------------------------- #


def test_a_quiet_clean_tick_pings_the_dead_man(app, client, db, sent, pinged):
    assert tick(app, int(time.time())) is True
    assert sent == []
    assert pinged == [True]


def test_a_failed_send_leaves_no_row_no_ping_and_is_retried(db, sent, pinged):
    calls = {"n": 0}

    def flaky(alert):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        sent.append(alert)
        return True

    app = create_app(
        db_path=str(db),
        token=TOKEN,
        next_s=60,
        cmd_ttl_s=900,
        quiet="0-0",
        send=flaky,
        ping=lambda: pinged.append(True) or True,
    )
    client = TestClient(app)
    report(client)
    see_everything(app)
    age_controller(db, 700)
    now = int(time.time())

    assert tick(app, now) is False
    assert run_sql(db, "SELECT key FROM alerts") == []  # not recorded: retried
    assert pinged == []  # an unreachable ntfy must trip the dead man

    assert tick(app, now) is True
    assert [a.key for a in sent] == ["silent:b1"]
    assert pinged == [True]


def test_the_first_failed_send_stops_the_loop(db, client_less=None):
    tried = []
    app = create_app(
        db_path=str(db),
        token=TOKEN,
        next_s=60,
        cmd_ttl_s=900,
        quiet="0-0",
        send=lambda alert: tried.append(alert.key) and False,
    )
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
    age_status(db, PERSIST_S + 1)
    tick(app, int(time.time()))

    health = client.get("/health").json()
    (b1,) = health["controllers"]
    assert b1["float"] == 0 and b1["pos"] == "unknown"
    assert sorted(a["key"] for a in health["alerts"]) == ["float:b1", "pos:b1"]

    report(client)  # full and homed again
    age_status(db, PERSIST_S + 1)
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


def test_post_ntfy_returns_false_on_a_5xx_and_on_a_dead_server(wire):
    url, recorder = wire
    recorder.status = 500
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
        age_status(db, PERSIST_S + 1)
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
