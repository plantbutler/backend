"""Pots, plants and calibration: mapping is an edit, % is derived, never stored."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from butler import create_app, moisture_pct, parse_pot

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    return TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )


def pot(client, body, token=TOKEN):
    return client.post("/pot", content=body, headers={"X-Token": token})


def report(client, body, token=TOKEN):
    return client.post("/report", content=body, headers={"X-Token": token})


def garden(client):
    return client.get("/pots").json()["pots"]


def pot_id(answer):
    """The id out of a `pot=<id> name=<name>` answer."""
    return answer.text.split()[0].removeprefix("pot=")


def mappings(db, pid):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT channel, outlet, to_ts FROM pot_mappings WHERE pot_id = ? "
            "ORDER BY from_ts, rowid",
            (pid,),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Upserts: repotting is an edit
# --------------------------------------------------------------------------- #


def test_a_pot_is_born_from_one_line(client):
    answer = pot(
        client,
        "name=basil controller=butler1 channel=0 outlet=3 "
        "plant_type=basil plant_size=small pot_size=14cm soil=universal",
    )
    assert answer.status_code == 200
    # The contract changed with the pot's identity: the answer names the id
    # the caller must key on from now on, and the nickname it went in under.
    assert answer.text == f"pot={pot_id(answer)} name=basil\n"
    assert pot_id(answer).startswith("pot-")

    (entry,) = garden(client)
    assert entry["id"] == pot_id(answer)
    assert entry["name"] == "basil"
    assert entry["outlet"] == 3
    assert entry["plant_type"] == "basil"
    assert entry["mode"] == "manual"
    assert entry["enabled"] == 1
    assert entry["pct"] is None  # uncalibrated
    assert entry["last_dose"] is None  # never watered


def test_an_update_touches_only_the_keys_given(client):
    pot(client, "name=basil controller=butler1 channel=0 plant_type=basil")

    pot(client, "name=basil outlet=4")

    (entry,) = garden(client)
    assert entry["outlet"] == 4
    assert entry["plant_type"] == "basil"
    assert entry["channel"] == 0


def test_a_name_alone_is_a_valid_pot(client):
    assert pot(client, "name=mystery").status_code == 200
    (entry,) = garden(client)
    assert entry["name"] == "mystery"


# --------------------------------------------------------------------------- #
# Calibration: % is derived at read time, never stored
# --------------------------------------------------------------------------- #


def test_calibration_turns_raw_into_percent(client):
    pot(client, "name=basil controller=butler1 channel=0")
    report(client, "c=butler1 t=1000 ch0=8000")
    assert garden(client)[0]["pct"] is None

    pot(client, "name=basil dry_raw=12000 wet_raw=4000")

    (entry,) = garden(client)
    assert entry["raw"] == 8000
    assert entry["pct"] == 50


def test_recalibrating_reinterprets_history_without_touching_it(client, db):
    pot(client, "name=basil controller=butler1 channel=0 dry_raw=12000 wet_raw=4000")
    report(client, "c=butler1 t=1000 ch0=8000")
    assert garden(client)[0]["pct"] == 50

    pot(client, "name=basil dry_raw=10000 wet_raw=8000")  # no new reading

    (entry,) = garden(client)
    assert entry["pct"] == 100  # same raw, new meaning
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT raw FROM readings").fetchall() == [(8000,)]


def test_percent_clamps_and_survives_either_sensor_direction():
    assert moisture_pct(8000, 12000, 4000) == 50  # capacitive: dry counts high
    assert moisture_pct(8000, 4000, 12000) == 50  # resistive: dry counts low
    assert moisture_pct(15000, 12000, 4000) == 0  # drier than dry air
    assert moisture_pct(1000, 12000, 4000) == 100  # wetter than water
    assert moisture_pct(8000, None, 4000) is None
    assert moisture_pct(8000, 12000, None) is None


def test_equal_calibration_points_are_refused_even_across_requests(client):
    assert pot(client, "name=a dry_raw=5000 wet_raw=5000").status_code == 400

    pot(client, "name=b dry_raw=5000")
    answer = pot(client, "name=b wet_raw=5000")
    assert answer.status_code == 400
    assert "must differ" in answer.text


def test_an_inverted_target_range_is_refused(client):
    pot(client, "name=a target_low_pct=30")
    answer = pot(client, "name=a target_high_pct=30")
    assert answer.status_code == 400
    assert "target_low_pct" in answer.text


# --------------------------------------------------------------------------- #
# Collisions: one sensor, one hose, one pot
# --------------------------------------------------------------------------- #


def test_two_enabled_pots_cannot_share_a_channel(client):
    pot(client, "name=basil controller=butler1 channel=0")

    answer = pot(client, "name=mint controller=butler1 channel=0")
    assert answer.status_code == 400
    assert "taken by pot basil" in answer.text


def test_two_enabled_pots_cannot_share_an_outlet(client):
    pot(client, "name=basil controller=butler1 outlet=3")

    answer = pot(client, "name=mint controller=butler1 outlet=3")
    assert answer.status_code == 400
    assert "taken by pot basil" in answer.text


def test_a_disabled_pot_frees_its_channel_and_hose(client):
    pot(client, "name=basil controller=butler1 channel=0 outlet=3")
    pot(client, "name=basil enabled=0")

    assert (
        pot(client, "name=mint controller=butler1 channel=0 outlet=3").status_code
        == 200
    )


def test_different_controllers_do_not_collide(client):
    pot(client, "name=basil controller=butler1 channel=0")
    assert pot(client, "name=mint controller=butler2 channel=0").status_code == 200


# --------------------------------------------------------------------------- #
# Refusals and the shape of the garden
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "plant_type=basil",  # no name
        "name=basil name=mint",  # name twice
        "name=basil channel=999",  # channel out of range
        "name=basil dry_raw=abc",  # not an integer
        "name=basil target_low_pct=101",  # percent over 100
        "name=basil mode=vacation",  # not a mode
        "name=basil enabled=2",  # enabled is 0 or 1
        "name=basil soil=",  # empty value
        "name=basil dose_ml=0",  # zero dose
    ],
)
def test_a_malformed_pot_is_refused(client, db, body):
    answer = pot(client, body)
    assert answer.status_code == 400
    assert answer.text.startswith("refused: ")
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM pots").fetchone() == (0,)


def test_writing_a_pot_needs_the_token_reading_the_garden_does_not(client):
    assert pot(client, "name=basil", token="nope").status_code == 401
    assert client.get("/pots").status_code == 200


def test_unknown_keys_are_ignored_like_everywhere_else(client):
    assert pot(client, "name=basil photo=basil.jpg").status_code == 200
    (entry,) = garden(client)
    assert "photo" not in entry


def test_parse_pot_is_strict_about_its_own_fields():
    assert parse_pot("name=basil channel=0 future=1") == {"name": "basil", "channel": 0}
    for bad in ["", "name=basil channel=0 channel=1", "name=basil =x"]:
        with pytest.raises(ValueError):
            parse_pot(bad)


# --------------------------------------------------------------------------- #
# Identity: the id is the pot, the name is a nickname
# --------------------------------------------------------------------------- #


def test_create_mints_an_id_and_returns_it(client):
    answer = pot(client, "name=basil")
    assert answer.status_code == 200
    got_id, got_name = answer.text.split()
    assert got_id.startswith("pot=pot-")
    assert got_name == "name=basil"


def test_a_pot_can_be_renamed_by_id(client):
    pid = pot_id(pot(client, "name=basil"))

    assert pot(client, f"id={pid} name=genovese").status_code == 200

    assert [e["name"] for e in garden(client)] == ["genovese"]
    assert [e["id"] for e in garden(client)] == [pid]


def test_renaming_onto_a_taken_name_is_refused(client):
    basil = pot_id(pot(client, "name=basil"))
    pot(client, "name=mint")

    answer = pot(client, f"id={basil} name=mint")

    assert answer.status_code == 400
    assert "taken" in answer.text
    assert sorted(e["name"] for e in garden(client)) == ["basil", "mint"]


def test_an_unknown_id_is_refused_rather_than_creating(client):
    answer = pot(client, "id=pot-000000 name=ghost")

    assert answer.status_code == 400
    assert "no pot" in answer.text
    assert garden(client) == []


def test_an_edit_by_id_needs_no_name(client):
    pid = pot_id(pot(client, "name=basil controller=butler1 channel=0"))

    assert pot(client, f"id={pid} dry_raw=12000 wet_raw=4000").status_code == 200

    (entry,) = garden(client)
    assert entry["name"] == "basil"
    assert entry["dry_raw"] == 12000


# --------------------------------------------------------------------------- #
# Remapping: the wiring has a window, so history stays attributed
# --------------------------------------------------------------------------- #


def test_remapping_closes_the_old_row_and_opens_a_new_one(client, db):
    pid = pot_id(pot(client, "name=basil controller=butler1 channel=3 outlet=1"))

    assert pot(client, f"id={pid} channel=4").status_code == 200

    rows = mappings(db, pid)
    assert len(rows) == 2
    assert rows[0][0] == 3 and rows[0][2] is not None  # the old row is closed
    assert rows[1] == (4, 1, None)  # the new one carries the kept outlet


def test_an_unchanged_mapping_opens_no_second_row(client, db):
    pid = pot_id(pot(client, "name=basil controller=butler1 channel=3"))

    pot(client, f"id={pid} channel=3 soil=peat")

    assert mappings(db, pid) == [(3, None, None)]


def test_a_pot_that_was_never_wired_has_no_mapping_row(client, db):
    pid = pot_id(pot(client, "name=mystery dry_raw=12000"))

    assert mappings(db, pid) == []


def test_a_freed_channel_can_be_taken_by_another_pot(client, db):
    """The collision check reads pots_now, so a closed window frees the hose."""
    basil = pot_id(pot(client, "name=basil controller=butler1 channel=0 outlet=3"))
    mint = pot_id(pot(client, "name=mint"))

    pot(client, f"id={basil} channel=1 outlet=4")

    assert (
        pot(client, f"id={mint} controller=butler1 channel=0 outlet=3").status_code
        == 200
    )
    assert len(mappings(db, basil)) == 2


# --------------------------------------------------------------------------- #
# species: what the care lookup will key on
# --------------------------------------------------------------------------- #


def test_species_round_trips(client):
    pid = pot_id(pot(client, "name=basil species=Ocimum_basilicum"))

    (entry,) = garden(client)
    assert entry["species"] == "Ocimum_basilicum"
    assert entry["id"] == pid
