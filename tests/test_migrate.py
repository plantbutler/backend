"""The one-time rebuild: pots get a random id, their wiring gets a window."""

import sqlite3

from butler import migrate, new_pot_id

OLD_POTS = """
CREATE TABLE pots (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  controller      TEXT,
  channel         INTEGER,
  outlet          INTEGER,
  plant_type      TEXT, plant_size TEXT, pot_size TEXT, soil TEXT,
  dry_raw         INTEGER, wet_raw INTEGER,
  target_low_pct  INTEGER, target_high_pct INTEGER,
  dose_ml         INTEGER,
  mode            TEXT NOT NULL DEFAULT 'manual',
  cooldown_h      INTEGER, daily_cap_ml INTEGER,
  enabled         INTEGER NOT NULL DEFAULT 1
);
"""


def old_db(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript(OLD_POTS)
    con.execute(
        "INSERT INTO pots (name, controller, channel, outlet, dry_raw, wet_raw, mode) "
        "VALUES ('basil', 'butler1', 3, 1, 13000, 4200, 'auto')"
    )
    con.execute("INSERT INTO pots (name) VALUES ('unmapped')")
    con.commit()
    return path, con


def test_new_pot_id_shape():
    got = new_pot_id()
    assert got.startswith("pot-")
    assert len(got) == 10
    assert got[4:] == got[4:].lower()
    assert new_pot_id() != new_pot_id()


def test_migrate_mints_ids_and_moves_the_mapping(tmp_path):
    path, con = old_db(tmp_path)
    assert migrate(con, path) is True
    rows = dict(con.execute("SELECT name, id FROM pots"))
    assert set(rows) == {"basil", "unmapped"}
    assert all(v.startswith("pot-") for v in rows.values())
    assert [r[0] for r in con.execute("SELECT name FROM pots_now WHERE channel = 3")] == ["basil"]
    mapping = con.execute(
        "SELECT pot_id, controller, channel, outlet, from_ts, to_ts FROM pot_mappings"
    ).fetchall()
    assert mapping == [(rows["basil"], "butler1", 3, 1, 0, None)]
    assert con.execute("SELECT dry_raw, mode FROM pots WHERE name = 'basil'").fetchone() == (13000, "auto")


def test_migrate_is_idempotent(tmp_path):
    path, con = old_db(tmp_path)
    migrate(con, path)
    before = con.execute("SELECT id FROM pots ORDER BY name").fetchall()
    assert migrate(con, path) is False
    assert con.execute("SELECT id FROM pots ORDER BY name").fetchall() == before


def test_migrate_leaves_a_backup(tmp_path):
    path, con = old_db(tmp_path)
    migrate(con, path)
    assert (tmp_path / "old.db.pre-identity.bak").exists()


def test_a_pot_with_no_mapping_gets_no_mapping_row(tmp_path):
    """The rebuild must not invent a window for a pot that was never wired.

    An `unmapped` pot with a (NULL, NULL, NULL) row in pot_mappings would
    read back through pots_now identically, but it would also claim to be
    the pot on controller NULL channel NULL, and the collision check would
    have to learn about a wiring that does not exist.
    """
    path, con = old_db(tmp_path)
    migrate(con, path)
    (unmapped,) = con.execute("SELECT id FROM pots WHERE name = 'unmapped'").fetchone()
    assert (
        con.execute(
            "SELECT COUNT(*) FROM pot_mappings WHERE pot_id = ?", (unmapped,)
        ).fetchone()
        == (0,)
    )
    # It is still in the garden, wiring columns NULL, so nothing is lost.
    assert con.execute(
        "SELECT controller, channel, outlet FROM pots_now WHERE name = 'unmapped'"
    ).fetchone() == (None, None, None)


def test_a_fresh_database_is_already_in_the_new_shape(tmp_path):
    """migrate() runs after schema.sql, so a new database must be a no-op."""
    from butler import SCHEMA_SQL

    path = str(tmp_path / "fresh.db")
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    assert migrate(con, path) is False
    assert not (tmp_path / "fresh.db.pre-identity.bak").exists()
