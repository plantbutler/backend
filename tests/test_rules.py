"""Rules that water: every gate errs dry, and the flip to auto is a human act."""

import sqlite3
import time
import types

import pytest
from fastapi.testclient import TestClient

import butler
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
    return answer.text.split()[0].removeprefix("pot=")


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


def soak_both(client, n):
    """Reports carrying ch0 and ch1, so a pot whose sensor channel is
    corrected mid-test keeps reading a channel that is actually there: a
    silent channel is skipped by the ladder before any gate is reached."""
    for _ in range(n):
        report(client, extra=f"ch1={DRY}")


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


def test_the_garden_carries_the_last_handed_dose_and_its_verdict(client, db):
    # POST /verdict wants a command id; the app finds it here, never in a log.
    make_pot(client, mode="learning")
    soak(client, 5)
    (entry,) = client.get("/pots").json()["pots"]
    assert entry["proposal"]["id"] == 1
    assert entry["last_dose"] is None  # proposed is not handed

    post(client, "/approve", "cmd=1")
    report(client)  # handed to the board
    dose = client.get("/pots").json()["pots"][0]["last_dose"]
    assert (dose["id"], dose["state"], dose["source"], dose["ml"]) == (
        1,
        "sent",
        "rules",
        100,
    )
    assert dose["sent_ts"] is not None
    assert (dose["flow_ml"], dose["acked_ts"], dose["verdict"]) == (None, None, None)

    report(client, extra="ack=1 flow_ml=97")
    post(client, "/verdict", "cmd=1 verdict=too_much")
    dose = client.get("/pots").json()["pots"][0]["last_dose"]
    assert (dose["state"], dose["flow_ml"], dose["verdict"]) == (
        "acked",
        97,
        "too_much",
    )
    assert dose["acked_ts"] is not None

    # A manual water-now on the same hose takes its place.
    post(client, "/command", "c=b1 water=3 ml=50 cap_s=5")
    report(client, raw=WET)
    dose = client.get("/pots").json()["pots"][0]["last_dose"]
    assert (dose["id"], dose["source"], dose["ml"], dose["verdict"]) == (
        2,
        "manual",
        50,
        None,
    )

    # Pin the ORDER BY without trusting the wall clock: on a sent_ts tie the
    # newer id wins; a newer id handed EARLIER loses to sent_ts (a proposal
    # approved after a manual water-now is the real case).
    def last_dose_id():
        return client.get("/pots").json()["pots"][0]["last_dose"]["id"]

    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE commands SET sent_ts = (SELECT sent_ts FROM commands WHERE id = 1) "
            "WHERE id = 2"
        )
    assert last_dose_id() == 2
    with sqlite3.connect(db) as con:
        con.execute("UPDATE commands SET sent_ts = sent_ts - 60 WHERE id = 2")
    assert last_dose_id() == 1


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


# --------------------------------------------------------------------------- #
# Remapping: the dose belongs to the pot, not to the hose
# --------------------------------------------------------------------------- #


def rewind(db, seconds):
    """Everything that has happened so far moves that far into the past.

    Mapping windows and commands are stamped in whole seconds and a test
    runs in milliseconds, so without this the pot is created, watered and
    remapped at one single timestamp and every window boundary is also
    the dose. Real gardens are not repotted in the second the pump runs.
    """
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE commands SET created_ts = created_ts - ?, "
            "sent_ts = sent_ts - ?, acked_ts = acked_ts - ?",
            (seconds, seconds, seconds),
        )
        con.execute(
            "UPDATE pot_mappings SET from_ts = from_ts - ?, to_ts = to_ts - ?",
            (seconds, seconds),
        )


def cards(client):
    return {p["name"]: p for p in client.get("/pots").json()["pots"]}


def test_a_dose_stays_with_the_pot_when_the_hoses_are_swapped(client, db):
    """Whose dose was it? The pot that was on that hose when it was handed.

    Otherwise the verdict the app files from mint's card — the learning
    log adaptive dosing will be fit on — judges mint's soil against the
    dose basil got.
    """
    basil = make_pot(client)
    soak(client, 5)  # basil waters on outlet 3
    report(client, extra="ack=1 flow_ml=97")
    rewind(db, 3600)  # an hour later, the hoses are swapped

    post(client, "/pot", f"id={basil} channel=1 outlet=4")
    post(client, "/pot", "name=mint controller=b1 channel=0 outlet=3")

    assert cards(client)["basil"]["last_dose"]["id"] == 1
    assert cards(client)["mint"]["last_dose"] is None  # mint was never watered


def test_a_water_now_on_a_hose_the_pot_has_left_is_not_its_dose(client, db):
    """The sentence the README owes the app's author, pinned.

    `last_dose` is not "the newest command on that hose": basil is dosed
    on outlet 3, then moves to outlet 4 while mint takes outlet 3, and the
    water-now that goes down outlet 3 next is MINT's. A client written to
    the older wording files basil's verdict against mint's soil — into the
    log adaptive dosing will one day be fit on.
    """
    basil = make_pot(client)
    soak(client, 5)
    report(client, extra="ack=1 flow_ml=97")  # cmd 1: basil's, on outlet 3
    rewind(db, 3600)  # an hour later the hoses are rearranged

    post(client, "/pot", f"id={basil} channel=1 outlet=4")
    post(client, "/pot", "name=mint controller=b1 channel=0 outlet=3")
    post(client, "/command", "c=b1 water=3 ml=50 cap_s=5")
    report(client, extra=f"ch1={DRY}")  # cmd 2 handed out, down outlet 3

    assert cards(client)["basil"]["last_dose"]["id"] == 1
    assert cards(client)["mint"]["last_dose"]["id"] == 2


def test_a_proposal_is_not_inherited_by_the_next_pot_on_the_hose(client, db):
    """A proposal is an offer to open a hose, so it does not travel with the
    pot either — and the pot that arrives on that hose must not be offered
    a dose that was sized for someone else's dryness."""
    basil = make_pot(client, mode="learning")
    soak(client, 5)  # proposal 1, for basil, on outlet 3
    rewind(db, 3600)

    post(client, "/pot", f"id={basil} outlet=4")
    post(client, "/pot", "name=mint controller=b1 channel=1 outlet=3")

    assert cards(client)["mint"]["proposal"] is None
    assert cards(client)["basil"]["proposal"] is None  # not on that hose now


def test_the_cooldown_stays_with_the_pot_when_its_hose_moves(client, db):
    """The six hours belong to the plant, not to the plumbing. Watering a
    pot and then moving its hose must not water it twice.

    No rewind here on purpose: the remap lands in the very second of the
    dose, which is the one genuinely ambiguous point of the window, and
    the gates have to read that ambiguity as "watered" in both directions.
    """
    basil = make_pot(client)  # auto, outlet 3, default 6 h cooldown
    soak(client, 5)
    report(client, extra="ack=1 flow_ml=97")

    post(client, "/pot", f"id={basil} outlet=4")

    soak(client, 6)  # still bone dry, still freshly watered
    assert len(commands(db)) == 1


def test_the_daily_cap_stays_with_the_pot_when_its_hose_moves(client, db):
    """The same, for the millilitres: a remap is not a fresh allowance."""
    basil = make_pot(client, cooldown_h=0, daily_cap_ml=150)
    soak(client, 5)
    report(client, extra="ack=1 flow_ml=100")

    post(client, "/pot", f"id={basil} outlet=4")

    soak(client, 6)  # 100 + 100 > 150 wherever the hose hangs
    assert len(commands(db)) == 1


def test_a_proposal_survives_a_correction_that_leaves_the_hose_alone(client, db):
    """A proposal is about a HOSE, and fixing a miswired sensor channel does
    not move the hose. It opens a new mapping window all the same, and if
    the offer is bound to that window it silently leaves the card — while
    its 'proposed' row goes on holding the hose slot, so nothing can be
    approved and nothing else can be proposed until it times out.
    """
    basil = make_pot(client, mode="learning")
    soak(client, 5)  # proposal 1, for basil, on outlet 3
    rewind(db, 600)  # ten minutes later the sensor channel is corrected

    post(client, "/pot", f"id={basil} channel=1")

    assert cards(client)["basil"]["proposal"]["id"] == 1


def test_a_remap_in_the_second_of_a_dose_spends_the_cap_once(client, db):
    """Both ends of the mapping window are inclusive on purpose, so a dose
    handed in the very second of a remap is held by the pot that left AND
    by the pot that arrived — two pots waiting errs dry.

    When the remap leaves the hose alone, though — a miswired sensor
    channel corrected — those two windows belong to the SAME pot, the dose
    matches both, and the daily cap SUMs it twice. One 100 ml dose spends
    200 of the allowance and the pot is refused for the rest of the day.
    """
    basil = make_pot(client, cooldown_h=0, daily_cap_ml=250)
    soak_both(client, 5)  # cmd 1: 100 ml on outlet 3, handed out
    rewind(db, 600)  # so the first window is a real one, not a single second

    post(client, "/pot", f"id={basil} channel=1")  # the sensor was miswired
    with sqlite3.connect(db) as con:
        # Put the dose on the boundary the remap wrote as the old window's
        # to_ts and the new one's from_ts: the one ambiguous second.
        (edge,) = con.execute(
            "SELECT from_ts FROM pot_mappings WHERE pot_id = ? AND to_ts IS NULL",
            (basil,),
        ).fetchone()
        con.execute(
            "UPDATE commands SET state = 'acked', sent_ts = ?, acked_ts = ?, "
            "flow_ml = 100 WHERE id = 1",
            (edge, edge),
        )

    soak_both(client, 6)  # 100 ml of 250 spent, so the next 100 ml fits
    assert len(commands(db)) == 2


# --------------------------------------------------------------------------- #
# Attribution can fail; the gates cannot
# --------------------------------------------------------------------------- #


def prime_the_line_by_hand(client, db, ml=120):
    """A dose on b1/outlet 3 that no pot can claim, a minute in the past.

    Setup day: the operator primes the line before anything is registered,
    so when the pot that hangs on that hose is saved a minute later, its
    first mapping window starts AFTER the dose and no window of its own
    holds it. The same shape arrives without a human when the server clock
    steps while the wiring is being saved.
    """
    post(client, "/command", f"c=b1 water=3 ml={ml}")
    report(client)  # the board is handed it
    report(client, extra=f"ack=1 flow_ml={ml}")
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE commands SET created_ts = created_ts - 60, "
            "sent_ts = sent_ts - 60, acked_ts = acked_ts - 60"
        )


def test_a_dose_the_pot_cannot_claim_still_holds_its_cooldown(client, db):
    """A dose that matches no mapping window is a dose that happened.

    Attribution is a lookup, and a lookup can come back empty for reasons
    that have nothing to do with the plant being dry: a hand dose before
    the pot was ever registered, a clock that stepped while the wiring was
    saved. Reading that emptiness as "never watered" is fail-OPEN, and
    decision #5 says unknown state makes watering less likely, not more.
    So the pot-keyed gate has the old hose-keyed one underneath it: 120 ml
    went down this hose a minute ago, whoever it belonged to.
    """
    prime_the_line_by_hand(client, db)
    make_pot(client)  # auto, outlet 3, default 6 h cooldown

    soak(client, 8)  # bone dry, and the ladder wants to water

    assert len(commands(db)) == 1


def test_a_dose_the_pot_cannot_claim_still_spends_its_daily_cap(client, db):
    """The same, for the millilitres. With the cooldown switched off, the
    cap is the only gate left, and 120 unattributable millilitres plus a
    100 ml dose is over an allowance of 150."""
    prime_the_line_by_hand(client, db)
    make_pot(client, cooldown_h=0, daily_cap_ml=150)

    soak(client, 8)

    assert len(commands(db)) == 1


def test_a_backwards_clock_step_at_a_remap_does_not_reopen_the_cooldown(
    client, db, monkeypatch
):
    """The gates read pot_mappings now, so a corrupt window is a wet pot.

    A container that starts before NTP has synced runs on a clock that
    steps backwards a moment later. Save the wiring, take the step, move
    the hose: the window that closes is stamped before it opened, matches
    nothing, and the dose inside it stops belonging to the pot. The hose
    floor cannot catch this one — the pot is on a different hose now, and
    that hose has no history of its own.

    Nor is `to_ts = max(now, from_ts)` enough. The pot was wired ten
    minutes before it was watered, so a window clamped only to its own
    start still ends before the dose it holds.
    """
    basil = make_pot(client)  # auto, outlet 3, default 6 h cooldown
    soak(client, 5)
    report(client, extra="ack=1 flow_ml=100")
    with sqlite3.connect(db) as con:
        # basil was wired ten minutes before the pump ran, not in the same
        # second: the dose sits strictly inside its window.
        con.execute(
            "UPDATE pot_mappings SET from_ts = from_ts - 600 WHERE pot_id = ?",
            (basil,),
        )

    monkeypatch.setattr(  # NTP corrects the host two hours backwards
        butler,
        "time",
        types.SimpleNamespace(
            time=lambda: time.time() - 7200, localtime=time.localtime
        ),
    )
    post(client, "/pot", f"id={basil} outlet=4")
    monkeypatch.undo()

    soak(client, 8)  # bone dry, minutes into a six-hour cooldown

    assert len(commands(db)) == 1
