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


def test_another_pot_on_that_hose_keeps_its_own_alarm(client, db):
    """The alarm is not this pot's to clear while somebody alive still
    holds the pair. Same controller AND same outlet, or the key would not
    even be the same string and the test would prove nothing."""
    basil = pot(client, "name=basil controller=b1 channel=0 outlet=0")
    with sqlite3.connect(db) as con:
        # A second live pot on the same hose is a config error the mapping
        # write refuses, so it is written by hand: the point is only that
        # free_alerts looks before it deletes.
        con.execute("INSERT INTO pots (id, name) VALUES ('pot-other', 'mint')")
        con.execute(
            "INSERT INTO pot_mappings (pot_id, controller, channel, outlet, from_ts) "
            "VALUES ('pot-other', 'b1', 0, 0, 0)"
        )
        for key in ("sensor:b1:0", "proposal:b1:0"):
            con.execute(
                "INSERT INTO alerts (key, raised_ts) VALUES (?, ?)",
                (key, int(time.time())),
            )
    post(client, "/pot", f"id={basil} status=graveyard")
    assert count(db, "alerts", "key = 'sensor:b1:0'") == 1
    assert count(db, "alerts", "key = 'proposal:b1:0'") == 1


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


def test_a_delete_frees_the_alerts_the_pot_leaves_behind(client, db):
    """The delete's own free_alerts call, which the graveyard's identical
    one does not cover: both keys, and both branches of the helper."""
    basil, _ = furnished(client, db)
    with sqlite3.connect(db) as con:
        for key in ("sensor:b1:0", "proposal:b1:0", "silent:b1"):
            con.execute(
                "INSERT INTO alerts (key, raised_ts) VALUES (?, ?)",
                (key, int(time.time())),
            )
    post(client, "/pot/delete", f"id={basil}")

    assert count(db, "alerts", "key = 'sensor:b1:0'") == 0
    assert count(db, "alerts", "key = 'proposal:b1:0'") == 0
    # The board's own conditions are not this pot's to clear.
    assert count(db, "alerts", "key = 'silent:b1'") == 1


def test_a_remap_frees_the_socket_it_left(client, db):
    """A pot moved to another channel leaves the old channel's alarm with
    nobody to clear it: both the raise and the clear read the pot's CURRENT
    wiring, so the row would stand in /health for ever."""
    basil = pot(client, "name=basil controller=b1 channel=0 outlet=0")
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO alerts (key, raised_ts) VALUES ('sensor:b1:0', ?)",
            (int(time.time()),),
        )
    post(client, "/pot", f"id={basil} channel=1")
    assert count(db, "alerts", "key = 'sensor:b1:0'") == 0


def test_burying_a_pot_takes_a_queued_dose_with_it(client, db):
    """Burial hands the outlet back to the garden, so a dose still waiting
    for the board would pour into whatever is wired there next."""
    basil = pot(client, "name=basil controller=b1 channel=0 outlet=0")
    answer = post(client, "/command", "c=b1 water=0 ml=100")
    assert answer.status_code == 200, answer.text
    post(client, "/pot", f"id={basil} status=graveyard")

    report = post(client, "/report", "c=b1 ch0=8000")
    assert "cmd=" not in report.text, report.text


def test_a_pot_the_board_is_holding_a_dose_for_cannot_be_erased(client, db):
    """The row would go while the water is still running: the board acks an
    id that no longer exists, and the freed slot lets the next command out
    on top of the one already pouring."""
    basil = pot(client, "name=basil controller=b1 channel=0 outlet=0")
    post(client, "/command", "c=b1 water=0 ml=100")
    assert "cmd=" in post(client, "/report", "c=b1 ch0=8000").text

    answer = post(client, "/pot/delete", f"id={basil}")
    assert answer.status_code == 400
    assert "holding a dose" in answer.text
    assert count(db, "pots") == 1


def test_wiring_a_pot_that_is_already_buried_is_refused(client, db):
    """The contradiction spread over two requests. One body saying both is
    already refused; this is the same thing said twice."""
    basil = pot(client, "name=basil controller=b1 channel=0 outlet=0")
    post(client, "/pot", f"id={basil} status=graveyard")

    answer = post(client, "/pot", f"id={basil} controller=b1 channel=0 outlet=0")
    assert answer.status_code == 400
    assert "bring it back first" in answer.text
    # Restoring and wiring in one body is NOT a contradiction: it is how a
    # plant comes back.
    assert post(
        client, "/pot", f"id={basil} status=alive controller=b1 channel=0 outlet=0"
    ).status_code == 200


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


def test_a_deleted_command_id_is_never_handed_out_again(client, db):
    """The belt. Without AUTOINCREMENT `id` is a rowid alias and sqlite
    hands a deleted command's id straight back, so the next dose inherits
    the erased pot's `dose:<id>` judgement row — and is then skipped for
    ever by the loop's NOT EXISTS guard, which is silence exactly where a
    failed pump should page."""
    basil, cmd_id = furnished(client, db)
    post(client, "/pot/delete", f"id={basil}")

    pot(client, "name=mint controller=b1 channel=0 outlet=0")
    assert water(client) > cmd_id


def test_the_delete_still_clears_the_ledger_it_could_leave_behind(client, db):
    """And the braces. A database made before the AUTOINCREMENT — or a
    future one that loses it — must not be left holding a judgement row or
    a verdict for a command nobody can look up."""
    basil, cmd_id = furnished(client, db)
    with sqlite3.connect(db) as con:  # the judgement ledger row for that dose
        con.execute(
            "INSERT INTO alerts (key, raised_ts) VALUES (?, ?)",
            (f"dose:{cmd_id}", int(time.time())),
        )
    post(client, "/pot/delete", f"id={basil}")

    assert count(db, "alerts", "key = ?", (f"dose:{cmd_id}",)) == 0
    assert count(db, "verdicts", "command_id = ?", (cmd_id,)) == 0


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
