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
