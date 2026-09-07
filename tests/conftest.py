"""The fixtures and moves every test file was declaring for itself.

The app is built one way. A module that wants it built differently — a quiet
window it does not have to think about, its alerts captured instead of sent —
redefines `settings`, and `app` and `client` follow from that.
"""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import butler
from butler import FLAP_WINDOW_S, TANK_SAMPLES_TO_ARM, TANK_TOLERANCE_PCT, create_app

TOKEN = "test-token"

DRY = 11000  # pct 12 with make_pot's calibration
WET = 8000  # pct 50


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


def auth(token=TOKEN):
    """The token header, or no header at all — which is what `token=None`
    means everywhere here, and what several refusals are about."""
    return {} if token is None else {"X-Token": token}


def post(client, path, body, token=TOKEN):
    return client.post(path, content=body, headers=auth(token))


def report(client, body):
    answer = post(client, "/report", body)
    assert answer.status_code == 200, answer.text
    return answer


def minted(answer, prefix="pot"):
    """The id out of a `<prefix>=<id> ...` answer.

    The 200 first: a refusal's text splits into a plausible id too, and a
    test that then asks about it passes for the wrong reason.
    """
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix(f"{prefix}=")


WIRED = {
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
}


def make_pot(client, *, drop=(), bare=False, **over):
    """A pot, wired to channel 0 and outlet 3 and calibrated, unless less
    is asked for.

    `bare=True` starts from the name alone, which is what a test wanting a
    second pot in the same garden needs — the wired defaults collide on
    both the channel and the outlet — and what the advice tests need, since
    a band already filled in is not the band they are asking about. `drop`
    names fields to leave out of the wired set.
    """
    fields = ({"name": "basil"} if bare else WIRED) | over
    for field in (drop,) if isinstance(drop, str) else drop:
        del fields[field]
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    return minted(post(client, "/pot", body))


def garden(client):
    """The pots, as the app lists them. `GET /pots` takes no token."""
    answer = client.get("/pots")
    assert answer.status_code == 200, answer.text
    return answer.json()["pots"]


def by_name(client):
    return {p["name"]: p for p in garden(client)}


def only_pot(client):
    (entry,) = garden(client)
    return entry


def water(client, controller=0, outlet=0, ml=100):
    """A manual dose all the way through the real routes: queued, handed
    out on one report and acknowledged on the next, so the stamp and the
    sent_ts are the ones production would write."""
    queued = post(client, "/command", f"c={controller} water={outlet} ml={ml}")
    cmd_id = int(minted(queued, "cmd"))
    handed = post(client, "/report", f"c={controller} ch0=8000")
    assert f"cmd={cmd_id}" in handed.text, handed.text
    post(client, "/report", f"c={controller} ch0=8000 ack={cmd_id} flow_ml={ml}")
    return cmd_id


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


def count(db, table, where="1", *params):
    return run_sql(db, f"SELECT COUNT(*) FROM {table} WHERE {where}", *params)[0][0]


def columns(db, table):
    return [row[1] for row in run_sql(db, f"PRAGMA table_info({table})")]


def raise_alert(db, key, raised_ts=None):
    """A page standing, put there by hand rather than earned."""
    run_sql(
        db,
        "INSERT INTO alerts (key, raised_ts) VALUES (?, ?)",
        key,
        int(time.time()) if raised_ts is None else raised_ts,
    )


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


# --------------------------------------------------------------------------- #
# The tank: the moves test_tank, test_tank_size and test_latches share
# --------------------------------------------------------------------------- #


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
            "float_seen = float_seen - ?, float_bad = float_bad - ?, "
            "float_bad_prev = float_bad_prev - ?, flap_since = flap_since - ?",
            (seconds,) * 7,
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


def refill(client, controller=0):
    """The human taps "refilled"; the tap's ts."""
    answer = post(client, "/refill", f"c={controller}")
    assert answer.status_code == 200, answer.text
    return int(answer.text.removeprefix("refill=").strip())


def tap(client, db):
    """The human says the tank is full, a minute ago; the tap's ts as it
    stands after that. A later `age` moves it again, so a test that taps
    twice reads the taps back with `taps`."""
    ts = refill(client)
    age(db, 60)
    return ts - 60


def full(client):
    """The float says full, twice: one sighting is not yet the word the
    tank is measured on — the firm word is what two consecutive reports
    carrying float= agree on — and the rise is the firm word's."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=1 pos=ok")


def empty(client):
    """The float says empty, twice: one sighting is a glitch by the
    board's own design (any of its three samples failing fails the word),
    and the drop is the firm word's, confirmed by the second."""
    report(client, "c=0 ch0=1 float=0 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok")


def still_empty(client, db):
    """A flap window on, the float still says empty: the sighting that
    confirms an earlier one — a dose's ack, a first report of empty — so
    the firm word drops, far enough from it that the two are the tank's
    run and not a float flapping at the line, which is the float: rule's
    subject and would page here."""
    age(db, FLAP_WINDOW_S + 1)
    report(client, "c=0 ch0=1 float=0 pos=ok")


def rise(db):
    """When the float's firm word last went 0 -> 1."""
    return run_sql(db, "SELECT float_rise FROM status WHERE controller = 0")[0][0]


def hand(client, ml):
    """A manual dose, handed to the board on its next report."""
    cmd_id = int(minted(post(client, "/command", f"c=0 water=3 ml={ml}"), "cmd"))
    handed = report(client, "c=0 ch0=1 float=1 pos=ok").text
    assert f"cmd={cmd_id} water=3 ml={ml}" in handed
    return cmd_id


def ack(client, cmd_id, flow=None, float_ok=1):
    """The report after the hand-off: it acknowledges the dose, carries the
    meter's count when there is one, and says `float_ok` about the float."""
    count = "" if flow is None else f" flow_ml={flow}"
    report(client, f"c=0 ch0=1 float={float_ok} pos=ok ack={cmd_id}{count}")


METERED = object()
"""`dose`'s default count: the meter saw exactly the dose that was asked for."""


def dose(client, ml, flow=METERED, float_ok=1):
    """A manual dose, handed on one report and acked on the next, whose
    float= says `float_ok` — one sighting, which on empty is not yet the
    word the tank is measured on (still_empty is). The meter counts the
    whole dose unless `flow` says otherwise, and `flow=None` acks with no
    count at all."""
    ack(client, hand(client, ml), ml if flow is METERED else flow, float_ok)


def pumped(db, ml, sent_ts, controller=0, flow=None):
    """One acked manual dose on the books, handed at `sent_ts` and acked
    the second after: what the counter and the samples read."""
    run_sql(
        db,
        "INSERT INTO commands (created_ts, controller, kind, outlet, ml, cap_s, "
        "state, source, sent_ts, acked_ts, flow_ml) "
        "VALUES (?, ?, 'water', 3, ?, 30, 'acked', 'manual', ?, ?, ?)",
        sent_ts, controller, ml, sent_ts, sent_ts + 1, ml if flow is None else flow,
    )


def dry_reports(client, n=5, extra=""):
    """n dry reports with the safety fields the rules need; no t=, so none
    is a retry of another. Returns the last response text."""
    text = ""
    for _ in range(n):
        text = report(client, f"c=0 ch0={DRY} float=1 pos=ok {extra}".strip()).text
    return text


def alerts(client):
    """The keys /health says stand raised."""
    return [a["key"] for a in client.get("/health").json()["alerts"]]


def rules_water(db):
    return run_sql(db, "SELECT id FROM commands WHERE source = 'rules'")


def learn_the_tank(app, client, db, sent, size):
    """Two runs of `size` ml, each ended by the float — saying empty on
    the dose's ack and still a flap window on — and a flap window apart:
    the tank is known, and its two announcements are ticked away. Returns
    the line `over` starts past."""
    full(client)
    for _ in range(TANK_SAMPLES_TO_ARM):
        tap(client, db)
        dose(client, size, float_ok=0)
        still_empty(client, db)
        age(db, FLAP_WINDOW_S + 1)
        full(client)
    assert health(client)["tank_ml"] == size
    tick(app)
    assert [k.split(":")[0] for k in keys(sent)] == ["tank"] * TANK_SAMPLES_TO_ARM
    # Both waited for this one tick, and each is judged as it closed: the
    # first knew nothing yet, the second knew the first.
    assert sent[0].message.endswith(f"(tank size learning, 1 of {TANK_SAMPLES_TO_ARM})")
    assert sent[1].message.endswith(
        f"(tank {size} ml over {TANK_SAMPLES_TO_ARM} samples)"
    )
    sent.clear()
    return size * (100 + TANK_TOLERANCE_PCT) // 100
