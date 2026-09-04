"""Columns a database was born without: the ALTER that is not a rebuild.

`CREATE TABLE IF NOT EXISTS` is additive about tables and nothing else, so a
column appended to a CREATE that has already run never reaches an existing
database. `butler.add_columns` is the additive answer to that, and this is
what it has to get right on the one database that matters — the live one,
which is neither fresh nor old enough to need the rebuild.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from butler import add_columns, create_app

TOKEN = "test-token"

# The shape 0.14.0 left behind: pots already keyed on `pot-xxxxxx`, wiring
# already in pot_mappings, but the two sizes still free text.
PRE_SIZES = """
CREATE TABLE pots (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  species         TEXT,
  plant_type      TEXT,
  plant_size      TEXT,
  pot_size        TEXT,
  soil            TEXT,
  dry_raw         INTEGER, wet_raw INTEGER,
  target_low_pct  INTEGER, target_high_pct INTEGER,
  dose_ml         INTEGER,
  mode            TEXT NOT NULL DEFAULT 'manual',
  cooldown_h      INTEGER, daily_cap_ml INTEGER,
  enabled         INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE pot_mappings (
  pot_id TEXT NOT NULL, controller TEXT, channel INTEGER, outlet INTEGER,
  from_ts INTEGER NOT NULL, to_ts INTEGER
);
CREATE VIEW pots_now AS
SELECT p.id, p.name, p.species, m.controller, m.channel, m.outlet,
       p.plant_type, p.plant_size, p.pot_size, p.soil,
       p.dry_raw, p.wet_raw, p.target_low_pct, p.target_high_pct,
       p.dose_ml, p.mode, p.cooldown_h, p.daily_cap_ml, p.enabled
  FROM pots p LEFT JOIN pot_mappings m ON m.pot_id = p.id AND m.to_ts IS NULL;
CREATE TABLE species_names (
  query TEXT PRIMARY KEY, fetched_ts INTEGER NOT NULL,
  accepted TEXT, rank TEXT, matched TEXT NOT NULL
);
"""


@pytest.fixture
def old(tmp_path):
    """A database in the pre-0.15.0 shape, with a pot of each kind of size."""
    path = tmp_path / "butler.db"
    con = sqlite3.connect(path)
    con.executescript(PRE_SIZES)
    con.executemany(
        "INSERT INTO pots (id, name, plant_type, plant_size, pot_size) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("pot-000001", "measured", "herb", "40", "14cm"),
            ("pot-000002", "worded", "herb", "tall", "small"),
        ],
    )
    con.execute(
        "INSERT INTO species_names (query, fetched_ts, accepted, rank, matched) "
        "VALUES ('ocimum basilicum', 1, 'Ocimum basilicum', 'SPECIES', 'exact')"
    )
    con.commit()
    con.close()
    return path


def columns(path, table):
    con = sqlite3.connect(path)
    got = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    con.close()
    return got


def test_the_columns_arrive_and_the_old_ones_stay(old):
    con = sqlite3.connect(old)
    # `readings` and `commands` are not in this fixture at all, and
    # add_columns skips a table it cannot find without claiming it did
    # anything — which is why only the pots and species_names entries show.
    assert add_columns(con) == [
        "pots.plant_height_cm",
        "pots.pot_diameter_cm",
        "species_names.family",
        "pots.status",
    ]
    con.close()
    # Additive means additive: nothing is dropped, so a rollback to the
    # previous container still reads every pot it wrote.
    assert "plant_size" in columns(old, "pots")
    assert "pot_size" in columns(old, "pots")
    assert "enabled" in columns(old, "pots")


def test_a_measurement_carries_over_and_a_word_does_not(old):
    con = sqlite3.connect(old)
    add_columns(con)
    got = dict(
        (name, (h, d))
        for name, h, d in con.execute(
            "SELECT name, plant_height_cm, pot_diameter_cm FROM pots"
        )
    )
    con.close()
    assert got["measured"] == (40.0, 14.0)  # "40" and "14cm" were numbers
    # "small" meant something to the keyword table it was written for, and
    # inventing centimetres out of it would be worse than asking again.
    assert got["worded"] == (None, None)


def test_running_twice_adds_nothing_the_second_time(old):
    con = sqlite3.connect(old)
    add_columns(con)
    assert add_columns(con) == []
    con.close()


def test_a_fresh_database_needs_none_of_it(tmp_path):
    con = sqlite3.connect(tmp_path / "fresh.db")
    assert add_columns(con) == []  # no tables yet: nothing to alter
    con.close()


def test_the_app_starts_on_the_old_shape_and_serves_the_new_one(old):
    """The whole point. A view is created without checking its columns
    exist, so getting the order wrong here fails on every read rather than
    at startup, which is the worst way to find out."""
    client = TestClient(create_app(db_path=str(old), token=TOKEN))
    pots = {p["name"]: p for p in client.get("/pots").json()["pots"]}
    assert pots["measured"]["pot_diameter_cm"] == 14.0
    assert pots["measured"]["plant_height_cm"] == 40.0
    assert pots["worded"]["pot_diameter_cm"] is None
    assert "plant_size" not in pots["measured"]  # the view moved on
    # And the carried-over numbers reach the band engine, which is what
    # they were carried over for. The pot is the reference 14 cm and so
    # moves nothing and is not named; the plant is 40 cm and is.
    assert pots["measured"]["advice"]["why"] == "herb, 40 cm plant"


def test_the_cached_names_gain_a_family_they_can_be_refilled_with(old):
    client = TestClient(create_app(db_path=str(old), token=TOKEN))
    assert client.get("/pots").status_code == 200
    assert "family" in columns(old, "species_names")
    con = sqlite3.connect(old)
    (family,) = con.execute(
        "SELECT family FROM species_names WHERE query = 'ocimum basilicum'"
    ).fetchone()
    con.close()
    # The ALTER cannot invent what GBIF was never asked for, so the carried
    # row starts empty. What stops it staying empty for ever is taxon_for:
    # a row that resolved a name but carries no family counts as stale
    # rather than as a hit, so the next lookup re-asks and fills it in.
    # (That half is tested in test_species; here the point is only that the
    # column arrives NULL rather than wrong.)
    assert family is None


def test_the_pots_carried_over_are_alive(old):
    """The switch became a word, and the fixture's pots were all enabled."""
    client = TestClient(create_app(db_path=str(old), token=TOKEN))
    answer = client.get("/pots")
    assert answer.status_code == 200, answer.text
    assert {p["status"] for p in answer.json()["pots"]} == {"alive"}
