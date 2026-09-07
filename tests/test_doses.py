"""The watering history: every dose a pot was handed, and the odd ones too."""

import time

import pytest
from starlette.datastructures import QueryParams

from butler import parse_doses
from conftest import minted, post, run_sql, water


def pot(db, pot_id, name, controller=0, outlet=0, from_ts=0, to_ts=None):
    """A pot and one mapping window. The window no longer decides whose
    dose it was — the row's stamp does — but it says which sensor the pot
    was on, which enqueue reads to pick that stamp."""
    run_sql(db, "INSERT OR IGNORE INTO pots (id, name) VALUES (?, ?)", pot_id, name)
    run_sql(
        db,
        "INSERT INTO pot_mappings (pot_id, controller, channel, outlet, from_ts, to_ts) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        pot_id, controller, outlet, from_ts, to_ts,
    )


def dose(db, cmd_id, sent_ts, state="acked", ml=100, flow_ml=None, outlet=0,
         controller=0, acked_ts=None, created_ts=None, kind="water",
         source="manual", pot_id="pot-1"):
    """`pot_id` is the stamp the row carries — whom the dose was made for,
    written when the command was. None is a real value: a hose no pot was
    on, and a stop, which names no hose at all."""
    run_sql(
        db,
        "INSERT INTO commands (id, created_ts, controller, kind, outlet, ml, "
        "cap_s, state, source, sent_ts, acked_ts, flow_ml, pot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 30, ?, ?, ?, ?, ?, ?)",
        cmd_id,
        created_ts if created_ts is not None else (sent_ts or 0),
        controller, kind, outlet, ml, state, source, sent_ts, acked_ts,
        flow_ml, pot_id,
    )


def post_pot(client, body):
    return minted(post(client, "/pot", body))


def get(client, **params):
    answer = client.get("/doses", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()["doses"]


def test_a_pot_gets_the_doses_it_was_handed_newest_first(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 3000, acked_ts=now - 2990, flow_ml=98)
    dose(db, 2, now - 100, acked_ts=now - 90, flow_ml=101)
    rows = get(client, pot="pot-1")
    assert [r["id"] for r in rows] == [2, 1]
    assert rows[0]["ml"] == 100 and rows[0]["flow_ml"] == 101
    assert rows[0]["pot"] == "pot-1" and rows[0]["pot_name"] == "basil"
    assert rows[0]["state"] == "acked" and rows[0]["source"] == "manual"


def test_a_remap_takes_the_pots_history_with_it(client, db):
    """A dose belongs to whoever the board was told to water, not whoever
    holds the hose now. Driven through POST /pot so the remap and stamps
    are the real ones, not hand-written windows."""
    basil = post_pot(client, "name=basil controller=0 channel=0 outlet=0")
    water(client)  # 1: basil, on outlet 0
    post_pot(client, f"id={basil} outlet=3")  # basil moves hose
    water(client, outlet=3)  # 2: basil, moved
    mint = post_pot(client, "name=mint controller=0 channel=1 outlet=0")
    water(client)  # 3: mint, on outlet 0 now
    assert [r["id"] for r in get(client, pot=basil)] == [2, 1]
    assert [r["id"] for r in get(client, pot=mint)] == [3]


def test_the_odd_rows_are_listed_not_filtered_out(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 400, state="expired")  # handed out, never acked
    dose(db, 2, now - 300, state="acked", ml=100, flow_ml=12, acked_ts=now - 290)
    dose(db, 3, now - 200, state="sent")  # still out there
    rows = get(client, pot="pot-1")
    assert [r["id"] for r in rows] == [3, 2, 1]
    assert rows[2]["state"] == "expired" and rows[2]["acked_ts"] is None
    assert rows[1]["flow_ml"] == 12


def test_a_proposal_is_not_history(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, None, state="proposed", created_ts=now - 50)
    dose(db, 2, now - 40, acked_ts=now - 30)
    assert [r["id"] for r in get(client, pot="pot-1")] == [2]
    assert [r["id"] for r in get(client)] == [2]


def test_the_garden_list_keeps_a_dose_nobody_can_be_blamed_for(client, db):
    """An unattributable dose must not vanish just because no window claims it."""
    now = int(time.time())
    pot(db, "pot-1", "basil", outlet=0, from_ts=now - 100)
    dose(db, 1, now - 500, outlet=7, acked_ts=now - 490, pot_id=None)
    dose(db, 2, now - 50, acked_ts=now - 40)
    rows = get(client)
    assert [r["id"] for r in rows] == [2, 1]
    assert rows[1]["pot"] is None and rows[1]["pot_name"] is None
    assert rows[0]["pot"] == "pot-1"
    # a pot's own list can only hold what it was handed
    assert [r["id"] for r in get(client, pot="pot-1")] == [2]


def test_a_dose_never_handed_out_has_no_pot_and_sorts_by_when_it_was_made(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 500, acked_ts=now - 490)
    # not handed out yet: sent_ts IS NOT NULL is what makes a row a dose
    dose(db, 2, None, state="queued", created_ts=now - 10)
    rows = get(client)
    assert [r["id"] for r in rows] == [2, 1]
    assert rows[0]["pot"] is None and rows[0]["sent_ts"] is None
    assert [r["id"] for r in get(client, pot="pot-1")] == [1]


def test_the_verdict_rides_along(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 100, acked_ts=now - 90, flow_ml=100)
    dose(db, 2, now - 50, acked_ts=now - 40, flow_ml=100)
    run_sql(
        db, "INSERT INTO verdicts (command_id, ts, verdict) VALUES (1, ?, 'too_much')", now
    )
    rows = {r["id"]: r for r in get(client, pot="pot-1")}
    assert rows[1]["verdict"] == "too_much"
    assert rows[2]["verdict"] is None


def test_limit_bounds_the_list_and_the_newest_survive(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    for i in range(1, 8):
        dose(db, i, now - 1000 + i * 10, acked_ts=now - 1000 + i * 10 + 1)
    assert [r["id"] for r in get(client, pot="pot-1", limit=3)] == [7, 6, 5]


def test_two_open_windows_on_one_hose_cannot_split_a_dose(client, db):
    """A stamped row has exactly one owner: two overlapping mapping windows
    on one hose cannot both claim the same dose."""
    now = int(time.time())
    pot(db, "pot-1", "basil", outlet=0, from_ts=0)
    pot(db, "pot-2", "mint", outlet=0, from_ts=0)
    dose(db, 1, now - 100, acked_ts=now - 90)
    assert [r["id"] for r in get(client)] == [1]
    assert [r["id"] for r in get(client, pot="pot-1")] == [1]
    assert get(client, pot="pot-2") == [], "the window does not make it mint's"


def test_a_stop_is_not_a_dose(client, db):
    """A stop has no outlet or millilitres, so it could never be attributed;
    listing it as an unattributable dose would bury the row that matters."""
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 100, acked_ts=now - 90)
    dose(db, 2, now - 50, kind="stop", outlet=None, ml=None, acked_ts=now - 40,
         pot_id=None)
    assert [r["id"] for r in get(client)] == [1]
    assert [r["id"] for r in get(client, pot="pot-1")] == [1]


def test_the_cursor_pages_back_through_doses_that_share_a_second(client, db):
    """A cursor on the timestamp alone would skip or repeat doses that
    share a second."""
    now = int(time.time())
    pot(db, "pot-1", "basil")
    for i in range(1, 6):  # ids 1..5, all handed out in the same second
        dose(db, i, now - 100, acked_ts=now - 90)
    first = get(client, pot="pot-1", limit=2)
    assert [r["id"] for r in first] == [5, 4]
    last = first[-1]
    second = get(client, pot="pot-1", limit=2, before=last["sent_ts"], before_id=last["id"])
    assert [r["id"] for r in second] == [3, 2]
    third = get(client, pot="pot-1", limit=2, before=second[-1]["sent_ts"], before_id=second[-1]["id"])
    assert [r["id"] for r in third] == [1]
    assert [r["id"] for r in first + second + third] == [5, 4, 3, 2, 1]


def test_the_cursor_crosses_a_second_boundary_too(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 300, acked_ts=now - 290)
    dose(db, 2, now - 200, acked_ts=now - 190)
    dose(db, 3, now - 100, acked_ts=now - 90)
    page = get(client, pot="pot-1", limit=1)
    assert [r["id"] for r in page] == [3]
    rest = get(client, pot="pot-1", before=page[0]["sent_ts"], before_id=page[0]["id"])
    assert [r["id"] for r in rest] == [2, 1]


def test_doses_needs_no_token(client, db):
    """A read, like /pots and /history."""
    assert client.get("/doses").status_code == 200


def test_the_answer_carries_the_servers_clock(client, db):
    now = int(time.time())
    answer = client.get("/doses").json()
    assert abs(answer["now"] - now) <= 5


def test_parse_doses_refuses_what_it_should():
    assert parse_doses(QueryParams("")) == (None, 50, None, 0)
    assert parse_doses(QueryParams("pot=pot-1&limit=10")) == ("pot-1", 10, None, 0)
    assert parse_doses(QueryParams("before=900&before_id=7")) == (None, 50, 900, 7)
    for bad in (
        "pot=a&pot=b",
        "limit=1&limit=2",
        "pot=",
        "limit=0",
        "limit=201",
        "limit=x",
        "limit=-1",
        "before=x",
        "before=-1",
        "before=1&before=2",
        "before_id=7",  # a cursor id without the timestamp it belongs to
    ):
        with pytest.raises(ValueError):
            parse_doses(QueryParams(bad))
