"""Latches on the wire (0.20.0): the board's flap and dry latches arrive
as ch210 and ch211 beside ch207's contra, the backend latches on levels
(the dry one with reason `dry`, the reset's edge consulted beside it),
/health and the stale page tell the flap apart, and a tap answers the
flap for one dose — a try that waits out no refusal's cooldown and is
paged for only once refused (spec 2026-09-07-latches-on-the-wire,
sections 2 and 5)."""

import sqlite3

from fastapi.testclient import TestClient

import butler
from butler import FLAP_WINDOW_S, PERSIST_S, SOAK_S, create_app
from test_tank import (  # the fixtures and the tank's own moves, shared
    DRY,
    TOKEN,
    WET,
    age,
    alerts,
    app,  # noqa: F401 — a fixture
    client,  # noqa: F401 — a fixture
    db,  # noqa: F401 — a fixture
    dose,
    dry_reports,
    health,
    keys,
    learn_the_tank,
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


def float_since(db):
    """When the board's word last changed: not the flap's clock."""
    return run_sql(db, "SELECT float_since FROM status WHERE controller = 0")[0][0]


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


def pages(sent, key):
    """What was sent under `key`, in order: the float and dose pages a
    walk earns on the side are not the subject."""
    return [a.message for a in sent if a.key == key and a.message is not None]


# --------------------------------------------------------------------------- #
# The dry latch is a level (spec D1)
# --------------------------------------------------------------------------- #


def test_ch211_latches_the_backend_dry_as_ch207_latches_contra(app, client, db, sent):
    """The board's dry latch — set by a reset with a dose in flight or by
    `dry on` at the console, cleared only by `dry off` — is on the wire
    as ch211, a level like ch207, and latches the backend with reason
    `dry` and its own words: the 409, the page, the queued dose expired,
    and the level re-asserting the latch on every report until the human
    resumes (spec D1, A2)."""
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    latched = health(client)["latched"]
    assert latched["reason"] == "dry" and latched["since"] > 0
    assert run_sql(db, "SELECT state FROM commands") == [("expired",)]
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.startswith("refused: board 0 stopped watering (dry since ")
    assert answer.text.rstrip().endswith(
        "check the tank, type dry off on the board, then resume"
    )
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert sent[0].priority == "high"
    assert sent[0].message == (
        "board 0 stopped watering: the board is held dry: a reset with the pump "
        "running, or dry on at the console — check the tank, type dry off on the "
        "board, then resume in the app"
    )
    # A level: the board repeats it on every report, and a repeat is not
    # a new fault — the stamp and the name stay, and the page is not news.
    run_sql(db, "UPDATE status SET latched_ts = latched_ts - 60")
    stamp = health(client)["latched"]["since"]
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    assert health(client)["latched"] == {"since": stamp, "reason": "dry"}
    tick(app)
    assert keys(sent) == ["latch:0"]
    # Resumed before `dry off` is typed: the level re-latches on the next
    # report, as the contra does, and pages again, floor or no floor.
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    assert health(client)["latched"] is None
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    again = health(client)["latched"]
    assert again["reason"] == "dry" and again["since"] > stamp
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    # `dry off` typed: the level goes, the latch stands until the human
    # resumes, and stays down after.
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0")
    assert health(client)["latched"]["reason"] == "dry"
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
    resume it latches again with its own words (spec D1, A2)."""
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
    assert again["reason"] == "dry" and again["since"] > stamp
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
    assert health(client)["latched"] == {"since": stamp, "reason": "dry"}
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    assert "type dry off on the board" in sent[1].message
    assert run_sql(db, "SELECT detail FROM alerts WHERE key = 'latch:0'") == [
        ("dry",)
    ]
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]  # once per name
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=1")
    assert health(client)["latched"] == {"since": stamp, "reason": "contra"}
    tick(app)
    assert keys(sent) == ["latch:0"] * 3 and "type clear contra" in sent[2].message
    assert alerts(client) == ["latch:0"]


def test_the_resetmid_edge_is_consulted_on_every_report(client):
    """err= turning to resetmid is the reset's edge, consulted on every
    report, level or no level: an older board carries no ch211 and says
    a reset with nothing else, and a board that carries ch211=0 may be
    one whose `dry off` was typed before its first post-reset report —
    read as "not dry, nothing to see", that reset hid for good. One that
    carries ch211=1 latches on the level, `dry` before `resetmid`,
    whatever err= does; and a sticky err= is no edge (spec A2)."""
    report(client, "c=0 ch0=1 float=1 pos=ok err=range")
    report(client, "c=0 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client)["latched"]["reason"] == "resetmid"
    report(client, "c=1 ch0=1 float=1 pos=ok ch211=0 err=range")
    report(client, "c=1 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    assert health(client, 1)["latched"]["reason"] == "resetmid"
    report(client, "c=2 ch0=1 float=1 pos=ok ch211=0 err=resetmid")  # a first report
    assert health(client, 2)["latched"]["reason"] == "resetmid"
    report(client, "c=3 ch0=1 float=1 pos=ok ch211=1 err=resetmid")
    assert health(client, 3)["latched"]["reason"] == "dry"
    assert post(client, "/resume", "c=3").text == "resumed=3\n"
    # `dry off` typed; err= is sticky at resetmid and says nothing more.
    report(client, "c=3 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    report(client, "c=3 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    assert health(client, 3)["latched"] is None


def test_dry_off_typed_before_the_first_post_reset_report_latches_on_the_edge(
    app, client, sent
):
    """Bring-up 7c, exactly: the board resets with the pump running and
    `dry off` is typed at the console before its first report after the
    reset. That report carries ch211=0 and err=resetmid — no level, the
    edge alone — and the edge latches, with the reset's own words in the
    409 and on the page; the board's next reports repeat the error, and
    after the resume the latch stays down (spec A2)."""
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0")
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    latched = health(client)["latched"]
    assert latched["reason"] == "resetmid" and latched["since"] > 0
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.startswith("refused: board 0 stopped watering (resetmid since ")
    assert answer.text.rstrip().endswith(
        "check the tank, type dry off on the board, then resume"
    )
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert sent[0].message == (
        "board 0 stopped watering: it reset with the pump running — check the "
        "tank, type dry off on the board, then resume in the app"
    )
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    assert health(client)["latched"]["reason"] == "resetmid"
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0 err=resetmid")
    assert health(client)["latched"] is None
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200


def test_a_contra_alone_on_a_board_that_says_it_is_not_dry_latches_contra(
    app, client, db, sent
):
    """A board with ch211 on the wire whose contra trips says ch207=1
    ch211=0: the contra alone, on a board saying it is not dry. It
    latches contra with the contra words, the level re-asserts it, and
    `clear contra` typed — ch207 going, ch211 still 0 — leaves the latch
    standing under its name until the human resumes, and down after
    (spec D1, A2)."""
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=0")
    latched = health(client)["latched"]
    assert latched["reason"] == "contra" and latched["since"] > 0
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.startswith("refused: board 0 stopped watering (contra since ")
    assert answer.text.rstrip().endswith(
        "check the tank, type clear contra on the board, then resume"
    )
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert sent[0].message == (
        "board 0 stopped watering: the float said full and the meter saw "
        "nothing — check the tank, type clear contra on the board, then resume "
        "in the app"
    )
    run_sql(db, "UPDATE status SET latched_ts = latched_ts - 60")
    stamp = health(client)["latched"]["since"]
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ch211=0")
    assert health(client)["latched"] == {"since": stamp, "reason": "contra"}
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 ch211=0")  # clear contra typed
    assert health(client)["latched"] == {"since": stamp, "reason": "contra"}
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=0 ch211=0")
    assert health(client)["latched"] is None
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200


def test_a_dead_float_waits_behind_the_boards_own_dry_level(app, client, db, sent):
    """status.dry's consumer, the alerts' quiet gate: a /resume before
    `dry off` is typed lifts the backend's latch and not the board's,
    which keeps saying ch211=1, and the latch page already says what to
    do — no stale: page while the level stands. The tick after a report
    without it pages the float as before (spec A2)."""
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=0 pos=ok ch211=1")
    tick(app)
    assert keys(sent) == ["latch:0"] and "type dry off" in sent[0].message
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)  # with the float saying empty
    age(db, PERSIST_S - 60)
    report(client, "c=0 ch0=1 float=0 pos=ok ch211=1")  # a word, its minutes on
    assert post(client, "/resume", "c=0").status_code == 200
    tick(app)
    assert keys(sent) == ["latch:0"]  # the board still says ch211=1
    age(db, FLAP_WINDOW_S + 1)  # one more sighting of empty is slosh, not a flap
    report(client, "c=0 ch0=1 float=0 pos=ok ch211=0")  # dry off typed, still empty
    tick(app)
    assert keys(sent) == ["latch:0", "stale:0"]


def test_over_waits_behind_the_boards_own_dry_level(app, client, db, sent):
    """The same gate for the dangerous page: resumed with ch211=1 still
    on the wire, the board is the latch page's business, not a stuck
    float's, until `dry off` is typed (spec A2)."""
    learn_the_tank(app, client, db, sent, 200)
    tap(client, db)
    dose(client, 250)
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=1")
    assert health(client)["over"] == 1  # the fact stands
    tick(app)
    tick(app)
    assert keys(sent) == ["latch:0"]
    assert post(client, "/resume", "c=0").status_code == 200
    tick(app)
    assert keys(sent) == ["latch:0"]  # resumed, but the board still says ch211=1
    report(client, "c=0 ch0=1 float=1 pos=ok ch211=0")  # dry off typed
    tick(app)
    assert keys(sent) == ["latch:0", "over:0"]


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
    """The tap made after the flap tripped answers it, and while its try
    is to come — not yet handed, or with the board — the page would tell
    the person to do what they just did: nothing. Refused, the tap is
    spent, and the page says what tripped and what to do (spec D2, A1)."""
    tapped = stuck_at_empty_after_a_tap(client, db, "ch210=1")
    tick(app)
    assert pages(sent, "stale:0") == []  # the tap answers the flap: its try is pending
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    assert "cmd=1 water=3 ml=50" in flapped(client)
    tick(app)
    assert pages(sent, "stale:0") == []  # with the board
    report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=1 flow_ml=0 err=float")
    tick(app)
    (alert,) = [a for a in sent if a.key == "stale:0"]
    assert alert.priority == "high"
    assert alert.message == (
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
    """On the board the word drops first and the flap trips reports
    later, after three refusals at dose time. A tap between the two was
    made before the flap tripped: it answers nothing, however much later
    than the word's own drop it came. The clock the tap is judged against
    is the flap's, status.flap_since, not the word's, status.float_since —
    the one report where the two differ (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=2)
    report(client, f"c=0 ch0={DRY} float=0 pos=ok")  # the word drops; no flap yet
    age(db, FLAP_WINDOW_S + 1)
    tapped = tap(client, db)  # full to the top, said after the word fell...
    assert "cmd=" not in flapped(client, n=2)  # ...and before the flap tripped: dry
    assert rules_water(db) == []
    assert float_since(db) < tapped < flap_since(db)
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


def test_a_stop_handed_after_the_tap_does_not_spend_it(client, db):
    """Only water spends the tap. A stop sent between the tap and the
    flap's next report is the safe direction, not the try the tap bought:
    it holds the slot for one report, and once the board has acked it the
    rules queue their dose as they would (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)
    assert "cmd=" not in flapped(client)
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    assert post(client, "/command", "c=0 stop=1").status_code == 200
    assert "cmd=1 stop=1" in flapped(client)  # the slot is the stop's: dry for a beat
    assert rules_water(db) == []
    acked = report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=1").text
    assert "cmd=2 water=3 ml=100" in acked
    assert rules_water(db) == [(2,)]


def test_a_tap_in_the_flaps_own_second_answers_nothing(client, db):
    """Later than the flap tripped, not at. The tap and the report that
    tripped the flap are two transactions stamped with one clock, so in
    one second which came first is unknowable, and a tap made before the
    flap was on the wire answers nothing: the tie goes dry, which costs
    the human one more tap a minute on. Pinned by hand — whether the two
    land in one second is the wall clock's business (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)
    assert "cmd=" not in flapped(client)
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    tripped = flap_since(db)
    run_sql(db, "UPDATE refills SET ts = ?", tripped)
    assert "cmd=" not in flapped(client)
    assert rules_water(db) == []
    run_sql(db, "UPDATE refills SET ts = ?", tripped + 1)
    assert "cmd=1 water=3 ml=100" in flapped(client)
    assert rules_water(db) == [(1,)]


def test_a_tap_that_never_saw_the_float_answers_nothing(client, db):
    """A tap whose snapshot is NULL — made while the board had never said
    float= (the rows 0.18.0 left behind are the same) — never meant "full
    to the top", and is no base for anything: not the counter's, and not
    the flap's, however much later than flap_since it came. The board's
    first word makes the next tap one that saw the float, and that one
    buys the try (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    for _ in range(5):
        report(client, f"c=0 ch0={DRY} pos=ok ch210=1")  # tripped; float= never said
    assert flap_since(db) > 0
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    assert run_sql(db, "SELECT float_ok FROM refills") == [(None,)]
    assert "cmd=" not in report(client, f"c=0 ch0={DRY} pos=ok ch210=1").text
    assert "cmd=" not in flapped(client)  # the board's first word; the tap saw none
    assert rules_water(db) == []
    age(db, 60)
    tap(client, db)
    assert run_sql(db, "SELECT float_ok FROM refills ORDER BY ts") == [(None,), (0,)]
    assert "cmd=1 water=3 ml=100" in flapped(client)


def test_water_handed_in_the_taps_own_second_spends_it(client, db):
    """Spent by water handed at or after the tap, not only after. The
    hand-off and the tap are two transactions stamped with one clock, and
    a dose handed in the tap's second was the try it bought: the tie goes
    dry, as the tap's own does — a strict comparison let a second report
    in that second hand a second try against the standing flap, the loop
    the flap exists to stop. Pinned by hand (spec D3)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)
    assert "cmd=" not in flapped(client)
    age(db, FLAP_WINDOW_S + 1)
    tapped = tap(client, db)
    assert "cmd=1 water=3 ml=100" in flapped(client)
    report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=1 flow_ml=0 err=float")
    assert "cmd=" not in flapped(client)
    run_sql(db, "UPDATE commands SET sent_ts = ? WHERE id = 1", tapped)
    assert "cmd=" not in flapped(client)
    assert rules_water(db) == [(1,)]
    run_sql(db, "UPDATE commands SET sent_ts = ? WHERE id = 1", tapped - 1)
    assert "cmd=2 water=3 ml=100" in flapped(client)  # handed before the tap: not its try


# --------------------------------------------------------------------------- #
# The tap's try waits out no refusal's cooldown, and is paged for only
# once refused (spec A1)
# --------------------------------------------------------------------------- #


def test_the_taps_try_does_not_wait_out_the_refusals_cooldown(app, client, db, sent):
    """The flap trips on refusals, and a refusal is an acked dose with
    flow_ml=0, which the cooldown counts as water: the try the tap bought
    waited six hours while the page told the person to do what they had
    just done. Three refusals at the line trip the flap; the person
    refills and taps; the very next report is handed the try, the last
    refusal well inside the cooldown, and no stale page comes between.
    Refused, the ack spends the tap, the refusal cools the pot as before,
    and the tripped text pages on the next tick; the float saying full
    afterwards is all normal (spec A1)."""
    make_pot(client, cooldown_h=6, daily_cap_ml=100_000)
    assert "cmd=1 water=3 ml=100" in dry_reports(client)  # handed on the fifth
    for cmd_id in (1, 2):
        # The word says full — the float is at the line — and the board's
        # own check at dose time says otherwise: a refusal, which cools
        # the pot as a dose does (a pot the board refuses for ever must
        # not be asked at report pace), and the cooldown out, the next.
        text = report(
            client, f"c=0 ch0={DRY} float=1 pos=ok ack={cmd_id} flow_ml=0 err=float"
        ).text
        assert "cmd=" not in text
        assert "cmd=" not in dry_reports(client)
        age(db, 6 * 3600 + 1)
        assert f"cmd={cmd_id + 1} water=3 ml=100" in dry_reports(client, n=1)
    # The third refusal trips the flap: the board forces its word to 0
    # and says why. Inside the cooldown, and dry either way.
    text = report(
        client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=3 flow_ml=0 err=float"
    ).text
    assert "cmd=" not in text and health(client)["flap"] == 1
    assert "cmd=" not in flapped(client)
    assert rules_water(db) == [(1,), (2,), (3,)]
    age(db, 60)
    tap(client, db)  # refilled to the top, and said so
    tick(app)
    assert pages(sent, "stale:0") == []
    assert "cmd=4 water=3 ml=100" in flapped(client)  # the try, the refusal minutes old
    assert rules_water(db) == [(1,), (2,), (3,), (4,)]
    age(db, PERSIST_S)  # the tap is its minutes old: what stale: waits for
    text = report(
        client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=4 flow_ml=0 err=float"
    ).text
    assert "cmd=" not in text
    assert "cmd=" not in flapped(client)  # the tap spent, the refusal cooling the pot
    tick(app)
    tapped = taps(db)[-1]
    assert pages(sent, "stale:0") == [
        f"the float on board 0 still says empty 4 min after the refill at "
        f"{butler.hhmm(tapped)}: the board's own float check tripped — refill to "
        "the top and tap refilled, and the butler will try one dose"
    ]
    report(client, f"c=0 ch0={DRY} float=1 pos=ok ch210=0")  # the float rose: normal
    assert health(client)["flap"] == 0
    tick(app)
    assert pages(sent, "stale:0")[-1] == "the float on board 0 moved"
    assert "cmd=" not in dry_reports(client)  # the refusal's cooldown, as before
    age(db, 6 * 3600 + 1)
    assert "cmd=5 water=3 ml=100" in dry_reports(client, n=1)  # on the word itself


def test_no_stale_page_while_the_taps_try_is_pending(app, client, db, sent):
    """The tap buys a try the rules make when they next would — a pot
    above its target waits — and until then the page would tell the
    person to refill and tap, which they just did. Skipped while the tap
    answers the flap and while its try is with the board: the float is
    dead by every other measure, and nothing pages; refused, it does
    (spec A1)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    report(client, f"c=0 ch0={WET} float=1 pos=ok")
    report(client, f"c=0 ch0={WET} float=1 pos=ok")
    report(client, f"c=0 ch0={WET} float=0 pos=ok ch210=1")  # tripped
    report(client, f"c=0 ch0={WET} float=0 pos=ok ch210=1")
    age(db, 60)
    tap(client, db)
    age(db, PERSIST_S)
    report(client, f"c=0 ch0={WET} float=0 pos=ok ch210=1")  # its minutes on; not dry
    assert rules_water(db) == []
    with sqlite3.connect(db) as con:
        assert butler.float_dead(con, 0, butler.latest_refill(con, 0)) is not None
    tick(app)
    assert pages(sent, "stale:0") == []
    flapped(client, n=2)
    assert "cmd=1 water=3 ml=100" in flapped(client)  # dry at last: the try
    tick(app)
    assert pages(sent, "stale:0") == []  # with the board
    report(client, f"c=0 ch0={DRY} float=0 pos=ok ch210=1 ack=1 flow_ml=0 err=float")
    tick(app)
    assert len(pages(sent, "stale:0")) == 1
    assert "the board's own float check tripped" in pages(sent, "stale:0")[0]


def test_the_flap_path_wants_the_boards_word_of_zero(client, db):
    """The flap forces the board's word to 0, and that word is what the
    tap answers: a report that omits float= under ch210=1 says nothing
    the tap can answer and hands no try — an omitted float= is dry here
    as everywhere (spec A1)."""
    make_pot(client, cooldown_h=0, daily_cap_ml=100_000)
    dry_reports(client, n=4)
    assert "cmd=" not in flapped(client)
    age(db, FLAP_WINDOW_S + 1)
    tap(client, db)
    assert "cmd=" not in report(client, f"c=0 ch0={DRY} pos=ok ch210=1").text
    assert rules_water(db) == []
    assert "cmd=1 water=3 ml=100" in flapped(client)


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
    assert entry["flap"] == 1 and entry["latched"]["reason"] == "dry"
