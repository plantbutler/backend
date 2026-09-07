"""Latches on the wire (0.20.0): the board's flap and dry latches arrive
as ch210 and ch211 beside ch207's contra, the backend latches on levels
(the dry one under the resetmid words), /health and the stale page tell
the flap apart, and a tap answers the flap for one dose (spec
2026-09-07-latches-on-the-wire, section 2)."""

import sqlite3

from fastapi.testclient import TestClient

import butler
from butler import FLAP_WINDOW_S, PERSIST_S, SOAK_S, create_app
from test_tank import (  # the fixtures and the tank's own moves, shared
    DRY,
    TOKEN,
    age,
    alerts,
    app,  # noqa: F401 — a fixture
    client,  # noqa: F401 — a fixture
    db,  # noqa: F401 — a fixture
    dry_reports,
    health,
    keys,
    make_pot,
    post,
    report,
    rules_water,
    run_sql,
    sent,  # noqa: F401 — a fixture
    tap,
    taps,
    tick,
)


def flap_since(db):
    """When the board's flap last tripped, 0 -> 1."""
    return run_sql(db, "SELECT flap_since FROM status WHERE controller = 0")[0][0]


def origin(db):
    with sqlite3.connect(db) as con:
        return butler.counter_origin(con, 0)


def flapped(client, n=1):
    """n dry reports under the flap: the board's word is 0, forced, and
    ch210 says why. Returns the last response text."""
    text = ""
    for _ in range(n):
        text = report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1").text
    return text


# --------------------------------------------------------------------------- #
# The dry latch is a level (spec D1)
# --------------------------------------------------------------------------- #


def test_ch211_latches_the_backend_dry_as_ch207_latches_contra(app, client, db, sent):
    """The board's dry latch — set by a reset with a dose in flight,
    cleared only by `dry off` — is on the wire as ch211, a level like
    ch207, and latches the backend under the resetmid words: the 409,
    the page, the queued dose expired, and the level re-asserting the
    latch on every report until the human resumes (spec D1)."""
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    latched = health(client)["latched"]
    assert latched["reason"] == "resetmid" and latched["since"] > 0
    assert run_sql(db, "SELECT state FROM commands") == [("expired",)]
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.startswith("refused: board 0 stopped watering (resetmid since ")
    assert answer.text.rstrip().endswith(
        "check the tank, type dry off on the board, then resume"
    )
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert sent[0].priority == "high"
    assert sent[0].message == (
        "board 0 stopped watering: it reset with the pump running — check the "
        "tank, type dry off on the board, then resume in the app"
    )
    # A level: the board repeats it on every report, and a repeat is not
    # a new fault — the stamp and the name stay, and the page is not news.
    run_sql(db, "UPDATE status SET latched_ts = latched_ts - 60")
    stamp = health(client)["latched"]["since"]
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    assert health(client)["latched"] == {"since": stamp, "reason": "resetmid"}
    tick(app)
    assert keys(sent) == ["latch:0"]
    # Resumed before `dry off` is typed: the level re-latches on the next
    # report, as the contra does, and pages again, floor or no floor.
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    assert health(client)["latched"] is None
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    again = health(client)["latched"]
    assert again["reason"] == "resetmid" and again["since"] > stamp
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    # `dry off` typed: the level goes, the latch stands until the human
    # resumes, and stays down after.
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0")
    assert health(client)["latched"]["reason"] == "resetmid"
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0")
    assert health(client)["latched"] is None
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200


def test_both_levels_name_contra_first_and_the_dry_latch_after_the_resume(
    app, client, db, sent
):
    """A board that reset mid-dose under a standing contra says both on
    every report, ch207=1 and ch211=1 (and err=resetmid, sticky). The
    contra is the reason: its step comes first. Once `clear contra` is
    typed and ch207 goes, the dry level still stands, and after the
    resume it latches again with its own words (spec D1)."""
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=1 err=resetmid")
    assert health(client)["latched"]["reason"] == "contra"
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.rstrip().endswith(
        "check the tank, type clear contra on the board, then resume"
    )
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert "type clear contra on the board" in sent[0].message
    run_sql(db, "UPDATE status SET latched_ts = latched_ts - 60")
    stamp = health(client)["latched"]["since"]
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=1 err=resetmid")  # both, again
    assert health(client)["latched"] == {"since": stamp, "reason": "contra"}
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 ch211=1 err=resetmid")  # clear contra typed
    again = health(client)["latched"]
    assert again["reason"] == "resetmid" and again["since"] > stamp
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    assert sent[1].priority == "high"
    assert "type dry off on the board" in sent[1].message
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.rstrip().endswith(
        "check the tank, type dry off on the board, then resume"
    )
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 ch211=0 err=resetmid")  # dry off typed
    assert health(client)["latched"] is None


def test_clear_contra_under_both_levels_renames_the_standing_latch(app, client, db, sent):
    """Nobody has resumed yet: `clear contra` typed with the dry level
    still on the wire overwrites the reason, keeps the stamp, and the
    page comes again with the new words, floor or no floor — a person
    told "clear contra" must also be told "dry off". And a contra
    landing under a dry latch takes the name back: its step comes first
    (spec D1, D14 d)."""
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=1")
    tick(app)
    assert keys(sent) == ["latch:0"] and "type clear contra" in sent[0].message
    run_sql(db, "UPDATE status SET latched_ts = latched_ts - 60")
    stamp = health(client)["latched"]["since"]
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 ch211=1")
    assert health(client)["latched"] == {"since": stamp, "reason": "resetmid"}
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    assert "type dry off on the board" in sent[1].message
    assert run_sql(db, "SELECT detail FROM alerts WHERE key = 'latch:0'") == [
        ("resetmid",)
    ]
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]  # once per name
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=1")
    assert health(client)["latched"] == {"since": stamp, "reason": "contra"}
    tick(app)
    assert keys(sent) == ["latch:0"] * 3 and "type clear contra" in sent[2].message
    assert alerts(client) == ["latch:0"]


def test_the_resetmid_edge_stays_only_for_a_board_that_sends_no_ch211(client):
    """An older board carries no ch211: err= turning to resetmid is still
    the edge that latches it. A board that carries ch211=0 is a board
    saying it is not dry, and the edge is not consulted; one that
    carries ch211=1 latches on the level, whatever err= does (spec D1)."""
    report(client, "c=0 ch0=1 float=1 pos=ok err=range")
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client)["latched"]["reason"] == "resetmid"
    report(client, "c=1 ch0=1 float=1 pos=ok ch211=0 err=range")
    report(client, "c=1 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    assert health(client, 1)["latched"] is None
    report(client, "c=2 ch0=1 float=1 pos=ok ch211=0 err=resetmid")  # a first report
    assert health(client, 2)["latched"] is None
    report(client, "c=3 ch0=1 float=1 pos=ok ch211=1 err=resetmid")
    assert health(client, 3)["latched"]["reason"] == "resetmid"
    assert post(client, "/resume", "c=3").text == "resumed=3\n"
    # `dry off` typed; err= is sticky at resetmid and says nothing more.
    report(client, "c=3 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    report(client, "c=3 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    assert health(client, 3)["latched"] is None


def test_status_keeps_the_boards_three_latches_from_its_latest_report(client, db):
    report(client, "c=0 ch0=1 float=0 pos=ok ch207=1 ch210=1 ch211=1")
    assert run_sql(db, "SELECT contra, flap, dry FROM status") == [(1, 1, 1)]
    report(client, "c=0 ch0=1 float=0 pos=ok")  # absent is 0
    assert run_sql(db, "SELECT contra, flap, dry FROM status") == [(0, 0, 0)]


# --------------------------------------------------------------------------- #
# The flap is told apart (spec D2)
# --------------------------------------------------------------------------- #


def test_health_carries_the_flap_and_status_its_clock(client, db):
    report(client, "c=0 ch0=1 float=1 pos=ok")
    assert health(client)["flap"] == 0 and flap_since(db) is None
    report(client, "c=0 ch0=1 float=0 pos=ok ch210=1")
    assert health(client)["flap"] == 1
    tripped = flap_since(db)
    assert tripped > 0
    run_sql(db, "UPDATE status SET flap_since = flap_since - 60")
    report(client, "c=0 ch0=1 float=0 pos=ok ch210=1")  # the level repeats: the clock stays
    assert health(client)["flap"] == 1 and flap_since(db) == tripped - 60
    report(client, "c=0 ch0=1 float=1 pos=ok ch210=0")  # a granted dose reset it
    assert health(client)["flap"] == 0 and flap_since(db) == tripped - 60
    report(client, "c=0 ch0=1 float=1 pos=ok")  # absent is 0: an older board never trips
    assert health(client)["flap"] == 0
    report(client, "c=0 ch0=1 float=0 pos=ok ch210=1")  # tripped again: a new clock
    assert flap_since(db) >= tripped
    # A board's first report ever can carry it.
    report(client, "c=1 ch0=1 float=0 pos=ok ch210=1")
    assert health(client, 1)["flap"] == 1
    assert run_sql(db, "SELECT flap_since FROM status WHERE controller = 1")[0][0] > 0


def stuck_at_empty_after_a_tap(client, db, extra=""):
    """The float says empty, the human taps with it so, and the board
    says empty again its minutes on: what stale: pages on. Returns the
    tap's ts as it stands."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, f"c=0 ch0=1 float=0 pos=ok {extra}".strip())
    age(db, FLAP_WINDOW_S + 1)  # one sighting is slosh, not a flap
    tap(client, db)
    age(db, PERSIST_S - 60)
    report(client, f"c=0 ch0=1 float=0 pos=ok {extra}".strip())  # a word, its minutes on
    return taps(db)[-1]


def test_the_stale_page_names_the_flap_when_the_board_says_it_tripped(
    app, client, db, sent
):
    tapped = stuck_at_empty_after_a_tap(client, db, "ch210=1")
    tick(app)
    assert keys(sent) == ["stale:0"]
    assert sent[0].priority == "high"
    assert sent[0].message == (
        f"the float on board 0 still says empty 3 min after the refill at "
        f"{butler.hhmm(tapped)}: the board's own float check tripped — refill to "
        "the top and tap refilled, and the butler will try one dose"
    )


def test_the_stale_page_presumes_the_float_stuck_when_the_flap_is_down(
    app, client, db, sent
):
    tapped = stuck_at_empty_after_a_tap(client, db)
    tick(app)
    assert keys(sent) == ["stale:0"]
    assert sent[0].priority == "high"
    assert sent[0].message == (
        f"the float on board 0 still says empty 3 min after the refill at "
        f"{butler.hhmm(tapped)}: presumed stuck at empty, look at the magnet"
    )


# --------------------------------------------------------------------------- #
# A tap answers the flap (spec D3)
# --------------------------------------------------------------------------- #


def test_a_tap_later_than_the_flap_buys_the_rules_one_dose(client, db):
    """Under the flap the board's word is 0, forced, and the rules are
    dry on it; a tap made after the flap tripped is the human saying
    full, and the rules queue their next dose as they would. The board's
    own float check granted it: the flap resets, float=1 returns, the
    next dose goes on the word itself — and the tap stays the origin,
    since the flap-forced 0 was a firm drop and no drop followed the tap
    (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)  # four dry readings, the float up, its word firm
    assert "cmd=" not in flapped(client)  # the fifth, under the flap, before any tap: dry
    assert rules_water(db) == []
    age(db, FLAP_WINDOW_S + 1)
    tapped = tap(client, db)
    assert "cmd=1 water=3 ml=100" in flapped(client)
    assert rules_water(db) == [(1,)]
    granted = report(
        client, f"c=0 ch0={DRY} float=1 pos=ok ch210=0 ack=1 flow_ml=100"
    ).text
    assert health(client)["flap"] == 0
    assert "cmd=2 water=3 ml=100" in granted
    assert origin(db) == (tapped, "tap")
    assert health(client)["pumped_ml"] == 100


def test_a_tap_before_the_flap_tripped_answers_nothing(client, db):
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=2)
    tap(client, db)  # full to the top, said before...
    dry_reports(client, n=2)
    assert "cmd=" not in flapped(client, n=2)  # ...the flap tripped: dry until the next tap
    assert rules_water(db) == []
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    assert "cmd=1 water=3 ml=100" in flapped(client)


def test_a_refused_try_leaves_the_rules_dry_until_the_next_tap(app, client, db, sent):
    """The board re-checks at dose time. Refused — the float is down —
    the dose acks with nothing and err=float, the flap stands, and the
    tap is spent: one dosefail page, the rules dry again until the next
    tap, which buys the next try (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)
    assert "cmd=" not in flapped(client)
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    assert "cmd=1 water=3 ml=100" in flapped(client)
    refused = report(
        client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=1 flow_ml=0 err=float"
    ).text
    assert "cmd=" not in refused
    assert "cmd=" not in flapped(client, n=3)
    assert rules_water(db) == [(1,)]
    age(db, SOAK_S + 1)
    tick(app)
    assert keys(sent) == ["dose:1"]
    assert sent[0].priority == "high"
    assert sent[0].message == (
        "the 100 ml dose on basil did not work: the meter counted 0 of 100 ml"
    )
    assert run_sql(db, "SELECT 1 FROM alerts WHERE key = 'dosefail:0'") == [(1,)]
    assert "cmd=" not in flapped(client)
    age(db, 60)
    tap(client, db)
    assert "cmd=2 water=3 ml=100" in flapped(client)


def test_a_manual_dose_under_the_flap_goes_and_spends_the_tap(client, db):
    """POST /command stays ungated: a human is at the phone and the
    board's own float check runs. Handed after the tap, it is the one
    try the tap bought, whoever asked for it (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)
    assert "cmd=" not in flapped(client)
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    assert "cmd=1 water=3 ml=50" in flapped(client)  # before any tap: ungated
    report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=1 flow_ml=0 err=float")
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    assert "cmd=2 water=3 ml=50" in flapped(client)
    report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=2 flow_ml=0 err=float")
    assert "cmd=" not in flapped(client, n=2)
    assert rules_water(db) == []


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #


def test_the_three_columns_are_in_the_create_and_in_added_columns():
    added = [(a.table, a.column, a.kind, a.source) for a in butler.ADDED_COLUMNS]
    assert added[-3:] == [
        ("status", "flap", "INTEGER NOT NULL DEFAULT 0", None),
        ("status", "flap_since", "INTEGER", None),
        ("status", "dry", "INTEGER NOT NULL DEFAULT 0", None),
    ]
    assert (butler.FLAP_CHANNEL, butler.DRY_CHANNEL) == (210, 211)


def test_an_existing_database_grows_the_three_columns_at_startup(db):
    """The 0.19.0 shape of status is this one less the three. They arrive
    at startup, absent being 0 — a board that has not reported since the
    upgrade reads as never latched, never tripped — and NULL for a clock
    nothing has started; the next report fills them in."""
    TestClient(create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900))
    with sqlite3.connect(db) as con:
        for column in ("flap", "flap_since", "dry"):
            con.execute(f"ALTER TABLE status DROP COLUMN {column}")
        con.execute(
            "INSERT INTO status (controller, ts, float_ok, float_since) VALUES (0, 5, 0, 5)"
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    assert run_sql(db, "SELECT flap, flap_since, dry FROM status") == [(0, None, 0)]
    entry = health(client)
    assert entry["flap"] == 0 and entry["latched"] is None
    report(client, "c=0 ch0=1 float=0 pos=ok ch210=1 ch211=1")
    assert run_sql(db, "SELECT flap, dry FROM status") == [(1, 1)]
    assert flap_since(db) > 5
    entry = health(client)
    assert entry["flap"] == 1 and entry["latched"]["reason"] == "resetmid"
