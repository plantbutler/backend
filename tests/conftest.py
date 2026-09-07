"""The fixtures and moves every test file was declaring for itself.

The app is built one way. A module that wants it built differently — a quiet
window it does not have to think about, its alerts captured instead of sent —
redefines `settings`, and `app` and `client` follow from that.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import butler
from butler import create_app

TOKEN = "test-token"


def make_app(db, photos=None, **over):
    """The app the fixtures build, with only the differences named.

    Leaving `photos` out asks for the store create_app would have picked
    anyway — beside the database — so the two never drift apart.
    """
    settings = {
        "db_path": str(db),
        "token": TOKEN,
        "next_s": 60,
        "cmd_ttl_s": 900,
        "photos_dir": str(photos if photos is not None else db.parent / "photos"),
    } | over
    return create_app(**settings)


def capturing(sent, pinged):
    """Alerts into one list and dead-man pings into another, both delivered."""
    return {
        "send": lambda alert: sent.append(alert) or True,
        "ping": lambda: pinged.append(True) or True,
    }


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def photos(tmp_path):
    """Where the photographs land: beside the database, as in production."""
    return tmp_path / "photos"


@pytest.fixture
def sent():
    return []


@pytest.fixture
def pinged():
    return []


@pytest.fixture
def settings():
    """What this module wants create_app called with beyond make_app's own."""
    return {}


@pytest.fixture
def app(db, photos, settings):
    return make_app(db, photos, **settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def post(client, path, body, token=TOKEN):
    return client.post(path, content=body, headers={"X-Token": token})


def report(client, body):
    answer = post(client, "/report", body)
    assert answer.status_code == 200, answer.text
    return answer


def make_pot(client, drop=None, **over):
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
    if drop:
        del fields[drop]
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    answer = post(client, "/pot", body)
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


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


def age_controller(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE controllers SET last_seen = last_seen - ?", (seconds,))


def taps(db):
    return [ts for (ts,) in run_sql(db, "SELECT ts FROM refills ORDER BY ts, rowid")]


def word_since(db):
    """When the float's word last changed, 1 -> 0 or 0 -> 1."""
    return run_sql(db, "SELECT float_word_since FROM status WHERE controller = 0")[0][0]


def origin(db):
    with sqlite3.connect(db) as con:
        return butler.counter_origin(con, 0)
