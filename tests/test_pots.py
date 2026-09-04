"""Pots, plants and calibration: mapping is an edit, % is derived, never stored."""

import sqlite3
import time
import types

import pytest
from fastapi.testclient import TestClient

import butler
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
    """The id out of a `pot=<id> name=<name>` answer.

    Asserts the 200 first: a refusal's text parses into a plausible-looking
    string too, and a test that then asks about that id gets an empty answer
    and passes for the wrong reason.
    """
    assert answer.status_code == 200, answer.text
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
        "name=basil controller=0 channel=0 outlet=3 "
        "plant_type=herb plant_height_cm=21 pot_diameter_cm=14 soil=peat",
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
    assert entry["plant_type"] == "herb"
    # Measurements, not adjectives: the wire carries numbers and the band
    # engine reads them as numbers.
    assert entry["pot_diameter_cm"] == 14.0
    assert entry["plant_height_cm"] == 21.0
    assert entry["mode"] == "manual"
    assert entry["status"] == "alive"
    assert entry["pct"] is None  # uncalibrated
    assert entry["last_dose"] is None  # never watered


def test_an_update_touches_only_the_keys_given(client):
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 plant_type=herb"))

    pot(client, f"id={basil} outlet=4")

    (entry,) = garden(client)
    assert entry["outlet"] == 4
    assert entry["plant_type"] == "herb"
    assert entry["channel"] == 0


def test_a_name_alone_is_a_valid_pot(client):
    assert pot(client, "name=mystery").status_code == 200
    (entry,) = garden(client)
    assert entry["name"] == "mystery"


# --------------------------------------------------------------------------- #
# Calibration: % is derived at read time, never stored
# --------------------------------------------------------------------------- #


def test_calibration_turns_raw_into_percent(client):
    basil = pot_id(pot(client, "name=basil controller=0 channel=0"))
    report(client, "c=0 t=1000 ch0=8000")
    assert garden(client)[0]["pct"] is None

    pot(client, f"id={basil} dry_raw=12000 wet_raw=4000")

    (entry,) = garden(client)
    assert entry["raw"] == 8000
    assert entry["pct"] == 50


def test_recalibrating_reinterprets_history_without_touching_it(client, db):
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 dry_raw=12000 wet_raw=4000"))
    report(client, "c=0 t=1000 ch0=8000")
    assert garden(client)[0]["pct"] == 50

    pot(client, f"id={basil} dry_raw=10000 wet_raw=8000")  # no new reading

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

    b = pot_id(pot(client, "name=b dry_raw=5000"))
    answer = pot(client, f"id={b} wet_raw=5000")
    assert answer.status_code == 400
    assert "must differ" in answer.text


def test_an_inverted_target_range_is_refused(client):
    a = pot_id(pot(client, "name=a target_low_pct=30"))
    answer = pot(client, f"id={a} target_high_pct=30")
    assert answer.status_code == 400
    assert "target_low_pct" in answer.text


# --------------------------------------------------------------------------- #
# Collisions: one sensor, one hose, one pot
# --------------------------------------------------------------------------- #


def test_two_live_pots_cannot_share_a_channel(client):
    pot(client, "name=basil controller=0 channel=0")

    answer = pot(client, "name=mint controller=0 channel=0")
    assert answer.status_code == 400
    assert "taken by pot basil" in answer.text


def test_two_live_pots_cannot_share_an_outlet(client):
    pot(client, "name=basil controller=0 outlet=3")

    answer = pot(client, "name=mint controller=0 outlet=3")
    assert answer.status_code == 400
    assert "taken by pot basil" in answer.text


def test_a_buried_pot_frees_its_channel_and_hose(client, db):
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    assert pot(client, f"id={basil} status=graveyard").status_code == 200
    # The 200 alone proves nothing: what frees the hardware is the closed
    # window, and a graveyard pot that kept its window open would pass the
    # create below anyway, on the collision check alone.
    assert [row[2] is None for row in mappings(db, basil)] == [False], "window closed"

    assert (
        pot(client, "name=mint controller=0 channel=0 outlet=3").status_code
        == 200
    )


def test_a_buried_pot_comes_back_unwired(client, db):
    """The plant that comes back is not in the socket the old one left, so
    restoring opens no window and the form asks where it went."""
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    pot(client, f"id={basil} status=graveyard")
    assert pot(client, f"id={basil} status=alive").status_code == 200

    (entry,) = garden(client)
    assert entry["status"] == "alive"
    assert (entry["controller"], entry["channel"], entry["outlet"]) == (None, None, None)
    assert [row[2] is None for row in mappings(db, basil)] == [False], "still closed"


def test_burying_a_pot_and_wiring_it_in_one_body_is_refused(client):
    """Two opposite instructions in one request. Asked of the REQUEST, not
    of the row — burying a pot that is wired right now is the whole point."""
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    answer = pot(client, f"id={basil} status=graveyard outlet=4")
    assert answer.status_code == 400
    assert "holds no wiring" in answer.text


def test_the_displacement_backstop_still_closes_a_stray_open_window(client, db):
    """The invariant here INVERTED with the graveyard: burying a pot now
    closes its window, so this can no longer be reached through the app.
    It is reached by hand instead, because the backstop is the only defence
    left — a reading is stamped with one pot as it lands, and two open
    windows on one channel would make that pick arbitrary and permanent."""
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    with sqlite3.connect(db) as con:  # the 2026-09-03 defect, recreated
        con.execute("UPDATE pots SET status = 'graveyard' WHERE id = ?", (basil,))
    assert mappings(db, basil) == [(0, 3, None)], "open, as the old defect left it"

    mint = pot_id(pot(client, "name=mint controller=0 channel=0 outlet=3"))
    assert [row[2] is None for row in mappings(db, basil)] == [False], "basil let go"
    assert mappings(db, mint) == [(0, 3, None)], "mint holds it now"


def test_a_pot_may_not_park_on_a_working_pots_hose(client, db):
    """The collision check is asked whatever the SAVED pot's own status is:
    the point is the other pot. It used to be skipped for a disabled pot,
    which let one open a second window on a working pot's hose."""
    pot(client, "name=basil controller=0 channel=0 outlet=3")

    answer = pot(client, "name=mint controller=0 outlet=3")
    assert answer.status_code == 400
    assert "taken by pot basil" in answer.text


def test_a_displaced_window_keeps_the_doses_it_held(client, db):
    """Burying a pot closes its window; it must not erase its history. The
    dose carries basil's stamp and keeps it, whoever takes the hose next."""
    import time

    now = int(time.time())
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    with sqlite3.connect(db) as con:
        # Backdate the window so it has a duration to hold a dose inside:
        # created and displaced in the same second, it would have none, and
        # a dose on that second goes to the pot that arrived, by the
        # half-open rule the attribution join is built on.
        con.execute(
            "UPDATE pot_mappings SET from_ts = ? WHERE pot_id = ?", (now - 1000, basil)
        )
        con.execute(
            "INSERT INTO commands (id, created_ts, controller, kind, outlet, ml, "
            "cap_s, state, source, sent_ts, acked_ts, flow_ml, pot_id) VALUES "
            "(1, ?, 0, 'water', 3, 100, 30, 'acked', 'manual', ?, ?, 98, ?)",
            (now - 510, now - 500, now - 490, basil),
        )
    assert pot(client, f"id={basil} status=graveyard").status_code == 200
    mint = pot_id(pot(client, "name=mint controller=0 channel=0 outlet=3"))

    mine = client.get(f"/doses?pot={basil}").json()["doses"]
    assert [r["id"] for r in mine] == [1], "the dose stays with the pot that held the hose"
    theirs = client.get(f"/doses?pot={mint}").json()["doses"]
    assert theirs == [], "and the newcomer does not inherit it"


def test_different_controllers_do_not_collide(client):
    pot(client, "name=basil controller=0 channel=0")
    assert pot(client, "name=mint controller=2 channel=0").status_code == 200


# --------------------------------------------------------------------------- #
# Refusals and the shape of the garden
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "plant_type=herb",  # no name
        "name=basil plant_type=basil",  # not one of the kinds
        "name=basil pot_diameter_cm=0",  # a 0 cm pot is a half-finished edit
        "name=basil pot_diameter_cm=-4",  # nor a negative one
        "name=basil pot_diameter_cm=1e3",  # float() would take this
        "name=basil pot_diameter_cm=inf",  # and this
        "name=basil pot_diameter_cm=nan",  # and this
        "name=basil pot_diameter_cm=201",  # bigger than a half-barrel
        "name=basil plant_height_cm=1001",  # a tree, not a pot plant
        "name=basil plant_height_cm=abc",  # not a measurement at all
        "name=basil name=mint",  # name twice
        "name=basil channel=999",  # channel out of range
        "name=basil dry_raw=abc",  # not an integer
        "name=basil target_low_pct=101",  # percent over 100
        "name=basil mode=vacation",  # not a mode
        "name=basil status=dead",  # not one of the statuses
        "name=basil soil=universal",  # not one of the soils
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
    assert pot(client, "name=basil colour=green").status_code == 200
    (entry,) = garden(client)
    assert "colour" not in entry
    # `photo` IS an answer key now — the newest picture, for the thumbnail
    # beside the name — but it is not a writable field, so a pot that has
    # never been photographed carries a null rather than nothing.
    assert entry["photo"] is None


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


def test_a_save_by_name_after_a_rename_creates_a_second_pot(client):
    """Which is why every edit keys on the id, and why the README says so.

    A `name=` save is a create when that name is free, so a client that
    remembered the nickname and missed the rename does not recalibrate the
    pot — it forks it, and calibrates a pot nobody is watering.
    """
    pid = pot_id(pot(client, "name=basil controller=0 channel=0"))
    pot(client, f"id={pid} name=genovese")

    assert pot(client, "name=basil dry_raw=12000 wet_raw=4000").status_code == 200

    entries = {e["name"]: e for e in garden(client)}
    assert sorted(entries) == ["basil", "genovese"]
    assert entries["genovese"]["dry_raw"] is None  # the real pot, uncalibrated
    assert entries["basil"]["controller"] is None  # the fork, wired to nothing


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
    pid = pot_id(pot(client, "name=basil controller=0 channel=0"))

    assert pot(client, f"id={pid} dry_raw=12000 wet_raw=4000").status_code == 200

    (entry,) = garden(client)
    assert entry["name"] == "basil"
    assert entry["dry_raw"] == 12000


# --------------------------------------------------------------------------- #
# Remapping: the wiring has a window, so history stays attributed
# --------------------------------------------------------------------------- #


def test_remapping_closes_the_old_row_and_opens_a_new_one(client, db):
    pid = pot_id(pot(client, "name=basil controller=0 channel=3 outlet=1"))

    assert pot(client, f"id={pid} channel=4").status_code == 200

    rows = mappings(db, pid)
    assert len(rows) == 2
    assert rows[0][0] == 3 and rows[0][2] is not None  # the old row is closed
    assert rows[1] == (4, 1, None)  # the new one carries the kept outlet


def test_an_unchanged_mapping_opens_no_second_row(client, db):
    pid = pot_id(pot(client, "name=basil controller=0 channel=3"))

    # Accepted AND silent about the mapping. Without the status assertion a
    # refusal that wrote nothing at all would satisfy the row count below,
    # which is the half of this test that is easy to lose.
    assert pot(client, f"id={pid} channel=3 soil=peat").status_code == 200

    assert mappings(db, pid) == [(3, None, None)]
    assert garden(client)[0]["soil"] == "peat"  # and the rest of it landed


def test_a_remap_saves_the_rest_of_the_request_too(client, db):
    """One POST /pot, two destinations: the wiring goes to pot_mappings and
    every other field to pots. Neither half may swallow the other."""
    pid = pot_id(pot(client, "name=basil controller=0 channel=3 outlet=1"))

    assert pot(client, f"id={pid} channel=4 soil=peat dry_raw=12000").status_code == 200

    (entry,) = garden(client)
    assert (entry["channel"], entry["soil"], entry["dry_raw"]) == (4, "peat", 12000)
    assert len(mappings(db, pid)) == 2


def test_a_pot_that_was_never_wired_has_no_mapping_row(client, db):
    pid = pot_id(pot(client, "name=mystery dry_raw=12000"))

    assert mappings(db, pid) == []


def test_a_freed_channel_can_be_taken_by_another_pot(client, db):
    """The collision check reads pots_now, so a closed window frees the hose."""
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    mint = pot_id(pot(client, "name=mint"))

    pot(client, f"id={basil} channel=1 outlet=4")

    assert (
        pot(client, f"id={mint} controller=0 channel=0 outlet=3").status_code
        == 200
    )
    assert len(mappings(db, basil)) == 2


def step_the_clock(monkeypatch, seconds):
    """Move the backend's idea of now, and nothing else's.

    Patching the module butler reads rather than the stdlib's own `time`,
    so a shifted clock stays inside the service under test.
    """
    monkeypatch.setattr(
        butler,
        "time",
        types.SimpleNamespace(
            time=lambda: time.time() + seconds, localtime=time.localtime
        ),
    )


def test_a_clock_that_steps_back_cannot_invert_a_window(client, db, monkeypatch):
    """A window that ends before it starts matches nothing, ever again.

    The container comes up, the wiring is saved, and only then does NTP
    correct the host clock backwards — a fresh Synology container is the
    ordinary way to get there. The next remap closes the open row at a
    moment before it opened, and every dose inside that window is orphaned
    from the pot it belongs to. Unlike a clock that is merely wrong, this
    is permanent: putting the clock right does not rewrite the row, and
    pot_mappings is now what the watering gates read.

    So the boundary is clamped. The window closes no earlier than it
    opened, and the next one starts on that same clamped second, because
    the whole attribution scheme assumes the windows are contiguous.
    """
    pid = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))
    step_the_clock(monkeypatch, -7200)

    assert pot(client, f"id={pid} outlet=4").status_code == 200

    with sqlite3.connect(db) as con:
        rows = con.execute(
            "SELECT outlet, from_ts, to_ts FROM pot_mappings WHERE pot_id = ? "
            "ORDER BY rowid",
            (pid,),
        ).fetchall()
    closed, opened = rows
    assert closed[2] >= closed[1], f"window closed before it opened: {closed}"
    assert opened[1] == closed[2], f"windows left with a gap: {rows}"
    assert opened[2] is None


# --------------------------------------------------------------------------- #
# species: what the care lookup will key on
# --------------------------------------------------------------------------- #


def test_species_round_trips(client):
    pid = pot_id(pot(client, "name=basil species=Ocimum_basilicum"))

    (entry,) = garden(client)
    assert entry["species"] == "Ocimum_basilicum"
    assert entry["id"] == pid


def test_a_create_never_edits_the_pot_that_already_has_that_name(client, db):
    """The whole pitch: an id-less POST /pot is a create, always. It used to
    look the name up and edit whatever answered to it, so a "new pot" made
    against a stale list quietly overwrote an existing one."""
    basil = pot_id(pot(client, "name=basil controller=0 channel=0 soil=peat"))

    answer = pot(client, "name=basil soil=peat")
    assert answer.status_code == 400
    assert "taken by pot" in answer.text
    assert "open it instead of creating one" in answer.text

    # Nothing moved, and no second pot appeared.
    (entry,) = garden(client)
    assert entry["id"] == basil
    assert entry["soil"] == "peat"


def test_editing_that_pot_still_works_through_its_id(client):
    basil = pot_id(pot(client, "name=basil soil=peat"))
    assert pot(client, f"id={basil} soil=peat").status_code == 200
    (entry,) = garden(client)
    assert entry["soil"] == "peat"
    assert entry["id"] == basil


def test_a_rename_onto_a_taken_name_keeps_its_own_words(client):
    """The rename refusal is not the create's: there is nothing to open."""
    pot(client, "name=basil")
    mint = pot_id(pot(client, "name=mint"))
    answer = pot(client, f"id={mint} name=basil")
    assert answer.status_code == 400
    assert "taken by pot" in answer.text
    assert "creating one" not in answer.text
