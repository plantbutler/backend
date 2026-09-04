"""The watering history: every dose a pot was handed, and the odd ones too."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from butler import create_app, parse_doses

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    return TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )


def pot(db, pot_id, name, controller=0, outlet=0, from_ts=0, to_ts=None):
    """A pot and one mapping window, stated outright. The windows no longer
    decide whose dose it was — the stamp on the row does — but they still
    say which sensor a pot was on, and enqueue reads the open one to choose
    the stamp, so they stay part of the fixture."""
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT OR IGNORE INTO pots (id, name) VALUES (?, ?)", (pot_id, name)
        )
        con.execute(
            "INSERT INTO pot_mappings (pot_id, controller, channel, outlet, from_ts, to_ts) "
            "VALUES (?, ?, 0, ?, ?, ?)",
            (pot_id, controller, outlet, from_ts, to_ts),
        )


def dose(db, cmd_id, sent_ts, state="acked", ml=100, flow_ml=None, outlet=0,
         controller=0, acked_ts=None, created_ts=None, kind="water",
         source="manual", pot_id="pot-1"):
    """`pot_id` is the stamp the row carries — whom the dose was made for,
    written when the command was. None is a real value: a hose no pot was
    on, and a stop, which names no hose at all."""
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO commands (id, created_ts, controller, kind, outlet, ml, "
            "cap_s, state, source, sent_ts, acked_ts, flow_ml, pot_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 30, ?, ?, ?, ?, ?, ?)",
            (
                cmd_id,
                created_ts if created_ts is not None else (sent_ts or 0),
                controller,
                kind,
                outlet,
                ml,
                state,
                source,
                sent_ts,
                acked_ts,
                flow_ml,
                pot_id,
            ),
        )


def post_pot(client, body):
    answer = client.post("/pot", content=body, headers={"X-Token": TOKEN})
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def water(client, pot_id, controller, outlet, ml=100):
    """A manual dose through the real path, then handed out and acked, so
    the stamp and the sent_ts are the ones production would write."""
    answer = client.post(
        "/command",
        content=f"c={controller} water={outlet} ml={ml}",
        headers={"X-Token": TOKEN},
    )
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.split()[0].removeprefix("cmd="))
    report = client.post(
        "/report", content=f"c={controller} ch0=8000", headers={"X-Token": TOKEN}
    )
    assert f"cmd={cmd_id}" in report.text, report.text
    client.post(
        "/report",
        content=f"c={controller} ch0=8000 ack={cmd_id} flow_ml={ml}",
        headers={"X-Token": TOKEN},
    )
    return cmd_id


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
    """A dose belongs to whoever the board was told to water, not to whoever
    hangs on that hose now. Driven through POST /pot so the remap and the
    stamps are the real ones, not hand-written windows: with the pot on the
    row, a fixture that wrote its own windows would prove nothing."""
    basil = post_pot(client, "name=basil controller=0 channel=0 outlet=0")
    water(client, basil, 0, 0)  # 1: basil, on outlet 0
    post_pot(client, f"id={basil} outlet=3")  # basil moves hose
    water(client, basil, 0, 3)  # 2: basil, moved
    mint = post_pot(client, "name=mint controller=0 channel=1 outlet=0")
    water(client, mint, 0, 0)  # 3: mint, on outlet 0 now
    assert [r["id"] for r in get(client, pot=basil)] == [2, 1]
    assert [r["id"] for r in get(client, pot=mint)] == [3]


def test_the_odd_rows_are_listed_not_filtered_out(client, db):
    """The row worth reading is the one that went wrong."""
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
    """An unattributable dose is the most interesting row there is: it must
    not vanish just because no window claims it."""
    now = int(time.time())
    pot(db, "pot-1", "basil", outlet=0, from_ts=now - 100)
    dose(db, 1, now - 500, outlet=7, acked_ts=now - 490, pot_id=None)  # no pot
    dose(db, 2, now - 50, acked_ts=now - 40)
    rows = get(client)
    assert [r["id"] for r in rows] == [2, 1]
    assert rows[1]["pot"] is None and rows[1]["pot_name"] is None
    assert rows[0]["pot"] == "pot-1"
    # A pot's own list can only hold what it was handed.
    assert [r["id"] for r in get(client, pot="pot-1")] == [2]


def test_a_dose_never_handed_out_has_no_pot_and_sorts_by_when_it_was_made(client, db):
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 500, acked_ts=now - 490)
    # Stamped for basil and never handed out: the stamp says whom it was
    # made for, and `sent_ts IS NOT NULL` is what makes it not yet a dose.
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
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO verdicts (command_id, ts, verdict) VALUES (1, ?, 'too_much')",
            (now,),
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
    """This used to need a GROUP BY: two overlapping windows claimed one
    dose and the garden list showed it twice. A stamped row has exactly one
    owner, so overlapping windows cannot reach it at all — which is also why
    the backstop in the mapping write matters more than it did (test_pots)."""
    now = int(time.time())
    pot(db, "pot-1", "basil", outlet=0, from_ts=0)
    pot(db, "pot-2", "mint", outlet=0, from_ts=0)
    dose(db, 1, now - 100, acked_ts=now - 90)
    assert [r["id"] for r in get(client)] == [1]
    assert [r["id"] for r in get(client, pot="pot-1")] == [1]
    assert get(client, pot="pot-2") == [], "the window does not make it mint's"


def test_a_stop_is_not_a_dose(client, db):
    """A stop has no outlet and no millilitres: it could never be
    attributed, and listing it as an unattributable dose would bury the row
    that actually matters."""
    now = int(time.time())
    pot(db, "pot-1", "basil")
    dose(db, 1, now - 100, acked_ts=now - 90)
    dose(db, 2, now - 50, kind="stop", outlet=None, ml=None, acked_ts=now - 40,
         pot_id=None)
    assert [r["id"] for r in get(client)] == [1]
    assert [r["id"] for r in get(client, pot="pot-1")] == [1]


def test_the_cursor_pages_back_through_doses_that_share_a_second(client, db):
    """The commands table is never pruned, so the older history has to be
    reachable — and several doses can share a second, which a cursor on the
    timestamp alone would skip or repeat."""
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
    # Nothing was skipped and nothing came twice.
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
