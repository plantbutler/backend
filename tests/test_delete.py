"""Erasing a pot, and the graveyard that is the reversible half of it.

The two are deliberately different operations. The graveyard keeps every
record and gives back the hardware; the delete keeps nothing. Only one of
them is reachable by accident, and it is the reversible one.
"""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from butler import create_app

TOKEN = "test-token"
DRY, WET = 9000, 4000


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def photos(tmp_path):
    return tmp_path / "photos"


@pytest.fixture
def client(db, photos):
    return TestClient(
        create_app(
            db_path=str(db),
            token=TOKEN,
            next_s=60,
            cmd_ttl_s=900,
            photos_dir=str(photos),
        )
    )


def post(client, path, body):
    return client.post(path, content=body, headers={"X-Token": TOKEN})


def pot(client, body):
    answer = post(client, "/pot", body)
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def count(db, table, where="1", args=()):
    with sqlite3.connect(db) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def water(client, controller="b1", outlet=0, ml=100):
    """A manual dose all the way through: queued, handed out, acknowledged."""
    answer = post(client, "/command", f"c={controller} water={outlet} ml={ml}")
    assert answer.status_code == 200, answer.text
    cmd_id = int(answer.text.split()[0].removeprefix("cmd="))
    post(client, "/report", f"c={controller} ch0=8000")
    post(client, "/report", f"c={controller} ch0=8000 ack={cmd_id} flow_ml={ml}")
    return cmd_id


def furnished(client, db, name="basil"):
    """One pot with everything a pot can accumulate hanging off it."""
    pot_id = pot(
        client,
        f"name={name} controller=b1 channel=0 outlet=0 plant_type=herb "
        f"dry_raw={DRY} wet_raw={WET}",
    )
    post(client, "/report", "c=b1 ch0=8000")
    cmd_id = water(client)
    post(client, "/verdict", f"cmd={cmd_id} verdict=ok")
    post(client, "/advice", f"pot={pot_id} kind=target dismiss=1")
    picture = client.post(
        f"/photo?pot={pot_id}",
        content=b"\xff\xd8\xff" + b"x" * 200,
        headers={"X-Token": TOKEN, "Content-Type": "image/jpeg"},
    )
    assert picture.status_code == 200, picture.text
    return pot_id, cmd_id


# --------------------------------------------------------------------------- #
# The graveyard
# --------------------------------------------------------------------------- #


def test_burying_a_pot_keeps_everything_and_frees_the_hardware(client, db):
    basil, cmd_id = furnished(client, db)
    assert post(client, "/pot", f"id={basil} status=graveyard").status_code == 200

    entry = {p["id"]: p for p in client.get("/pots").json()["pots"]}[basil]
    assert entry["status"] == "graveyard"
    assert (entry["controller"], entry["channel"], entry["outlet"]) == (None, None, None)
    # Everything it was is still there — that is the whole difference.
    assert count(db, "commands", "pot_id = ?", (basil,)) == 1
    assert count(db, "readings", "pot_id = ?", (basil,)) >= 1
    assert count(db, "photos", "pot_id = ?", (basil,)) == 1
    assert count(db, "verdicts") == 1
    assert client.get(f"/history?pot={basil}").json()["points"] != []


def test_burying_a_pot_expires_the_proposal_it_was_waiting_on(client, db):
    """Nothing else does this, and a proposal outliving its plant is an
    offer to water a pot that is no longer on that hose."""
    basil = pot(
        client,
        f"name=basil controller=b1 channel=0 outlet=0 plant_type=herb "
        f"dry_raw={DRY} wet_raw={WET} target_low_pct=40 dose_ml=100 mode=learning",
    )
    for _ in range(6):
        post(client, "/report", f"c=b1 float=1 pos=ok ch0={DRY}")
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM commands WHERE state = 'proposed'"
        ).fetchone() == (1,)

    post(client, "/pot", f"id={basil} status=graveyard")
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT COUNT(*) FROM commands WHERE state = 'proposed'"
        ).fetchone() == (0,)


def test_burying_a_pot_clears_the_sensor_alarm_nobody_could_clear(client, db):
    """Both the raise and the clear live inside a loop over the live pots,
    so a pot that leaves the loop while its alarm stands leaves a row
    nothing can ever clear: it sits in /health for good."""
    basil = pot(client, "name=basil controller=b1 channel=0")
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO alerts (key, raised_ts) VALUES ('sensor:b1:0', ?)",
            (int(time.time()),),
        )
    post(client, "/pot", f"id={basil} status=graveyard")
    assert count(db, "alerts", "key = 'sensor:b1:0'") == 0


def test_another_pot_on_that_channel_keeps_its_own_alarm(client, db):
    """The alarm is not this pot's to clear if somebody alive still holds
    the socket — which happens when two boards share channel numbers."""
    basil = pot(client, "name=basil controller=b1 channel=0")
    pot(client, "name=mint controller=b2 channel=0")
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO alerts (key, raised_ts) VALUES ('sensor:b2:0', ?)",
            (int(time.time()),),
        )
    post(client, "/pot", f"id={basil} status=graveyard")
    assert count(db, "alerts", "key = 'sensor:b2:0'") == 1


# --------------------------------------------------------------------------- #
# The delete
# --------------------------------------------------------------------------- #


def test_a_delete_erases_every_trace_including_the_files(client, db, photos):
    basil, _ = furnished(client, db)
    files = list((photos / basil).glob("*"))
    assert files, "the fixture wrote a picture"

    answer = post(client, "/pot/delete", f"id={basil}")
    assert answer.status_code == 200, answer.text
    assert answer.text == "ok\n"

    for table in ("pots", "pot_mappings", "commands", "readings", "photos",
                  "advice_dismissed"):
        where = "id = ?" if table == "pots" else "pot_id = ?"
        assert count(db, table, where, (basil,)) == 0, table
    assert count(db, "verdicts") == 0
    assert not any(f.exists() for f in files)
    assert not (photos / basil).exists()
    assert client.get("/pots").json()["pots"] == []


def test_a_delete_leaves_another_pots_things_alone(client, db):
    basil, _ = furnished(client, db, name="basil")
    mint = pot(client, "name=mint controller=b1 channel=1 outlet=1")
    post(client, "/report", "c=b1 ch1=7000")

    post(client, "/pot/delete", f"id={basil}")
    assert count(db, "readings", "pot_id = ?", (mint,)) >= 1
    assert [p["id"] for p in client.get("/pots").json()["pots"]] == [mint]


def test_deleting_the_same_pot_twice_is_refused_not_silently_ok(client, db):
    basil, _ = furnished(client, db)
    assert post(client, "/pot/delete", f"id={basil}").status_code == 200
    second = post(client, "/pot/delete", f"id={basil}")
    assert second.status_code == 400
    assert "no such pot" in second.text


def test_a_recycled_command_id_is_still_judged(client, db):
    """commands.id is a rowid alias with no AUTOINCREMENT, so sqlite hands
    the same ids out again after a delete. A leftover `dose:<id>` row would
    make the judgement loop skip the NEXT dose for ever on its NOT EXISTS
    guard, and a leftover verdict would label it — a stranger's verdict on
    a plant that never got that water, and silence where a failed pump
    should have paged."""
    basil, cmd_id = furnished(client, db)
    with sqlite3.connect(db) as con:  # the judgement ledger row for that dose
        con.execute(
            "INSERT INTO alerts (key, raised_ts) VALUES (?, ?)",
            (f"dose:{cmd_id}", int(time.time())),
        )
    post(client, "/pot/delete", f"id={basil}")

    assert count(db, "alerts", "key = ?", (f"dose:{cmd_id}",)) == 0
    assert count(db, "verdicts", "command_id = ?", (cmd_id,)) == 0

    # And the id really is reused, which is what makes the two above matter.
    pot(client, "name=mint controller=b1 channel=0 outlet=0")
    assert water(client) == cmd_id


def test_a_deleted_pots_readings_are_not_inherited_by_the_next_plant(client, db):
    """The reason the whole rework exists: a new plant in a dead one's
    socket must open its chart on its own soil, not on the dead one's."""
    basil = pot(client, f"name=basil controller=b1 channel=0 dry_raw={DRY} wet_raw={WET}")
    post(client, "/report", "c=b1 t=1 ch0=8000")
    post(client, "/pot/delete", f"id={basil}")

    mint = pot(client, f"name=mint controller=b1 channel=0 dry_raw={DRY} wet_raw={WET}")
    assert client.get(f"/history?pot={mint}").json()["points"] == []
    post(client, "/report", "c=b1 t=2 ch0=7000")
    assert [p["raw"] for p in client.get(f"/history?pot={mint}").json()["points"]] == [
        7000
    ]


def test_the_same_is_true_of_a_pot_that_was_only_buried(client, db):
    basil = pot(client, f"name=basil controller=b1 channel=0 dry_raw={DRY} wet_raw={WET}")
    post(client, "/report", "c=b1 t=1 ch0=8000")
    post(client, "/pot", f"id={basil} status=graveyard")

    mint = pot(client, f"name=mint controller=b1 channel=0 dry_raw={DRY} wet_raw={WET}")
    post(client, "/report", "c=b1 t=2 ch0=7000")
    assert [p["raw"] for p in client.get(f"/history?pot={mint}").json()["points"]] == [
        7000
    ]
    # And basil keeps its own, which is what the graveyard is for.
    assert [p["raw"] for p in client.get(f"/history?pot={basil}").json()["points"]] == [
        8000
    ]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_deleting_needs_the_token(client, db):
    basil, _ = furnished(client, db)
    answer = client.post("/pot/delete", content=f"id={basil}", headers={"X-Token": "no"})
    assert answer.status_code == 401
    assert count(db, "pots") == 1


@pytest.mark.parametrize(
    "body",
    [
        "",  # a body that went missing must never erase anything
        "pot=pot-3f9a21",  # /photo/delete's key, not this one
        "id=",  # empty
        "id=pot-1 id=pot-2",  # given twice
        "id=../../etc",  # SAFE_ID: this one becomes a directory path
        "id=pot 3f9a21",  # not a k=v token
    ],
)
def test_a_malformed_delete_is_refused_and_erases_nothing(client, db, body):
    furnished(client, db)
    answer = post(client, "/pot/delete", body)
    assert answer.status_code == 400
    assert answer.text.startswith("refused: ")
    assert count(db, "pots") == 1
