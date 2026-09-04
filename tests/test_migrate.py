"""The one-time rebuild: pots get a random id, their wiring gets a window."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import butler
from butler import create_app, migrate, new_pot_id

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
        "INSERT INTO pots (name, controller, channel, outlet, dry_raw, wet_raw, mode, "
        "plant_size, pot_size) "
        "VALUES ('basil', 'butler1', 3, 1, 13000, 4200, 'auto', '40', '14cm')"
    )
    con.execute(
        "INSERT INTO pots (name, plant_size, pot_size) "
        "VALUES ('unmapped', 'tall', 'small')"
    )
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


def test_the_rebuild_carries_the_sizes_it_can_read(tmp_path):
    """The old table's two sizes were free text and took anything. The
    rebuild reads them through the same reader `add_columns` uses, so a
    database that arrives here and one that arrives there cannot end up
    disagreeing about what "14cm" meant."""
    path, con = old_db(tmp_path)
    assert migrate(con, path) is True
    got = dict(
        (name, (h, d))
        for name, h, d in con.execute(
            "SELECT name, plant_height_cm, pot_diameter_cm FROM pots"
        )
    )
    assert got["basil"] == (40.0, 14.0)
    assert got["unmapped"] == (None, None)  # "tall" and "small" are not cm


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


def test_the_backup_carries_what_is_still_in_the_wal(tmp_path):
    """The live database runs in WAL mode, so the main file lags behind.

    The rebuild DROPs the pots table, and the backup is the only thing
    standing between that and the garden. A plain copy of the main file
    would omit every commit not yet checkpointed — which, for a container
    killed between a save and a restart, is exactly the rows at risk.
    """
    path = str(tmp_path / "wal.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(OLD_POTS)
    con.execute(
        "INSERT INTO pots (name, controller, channel) VALUES ('basil', 'butler1', 3)"
    )
    con.commit()

    assert migrate(con, path) is True

    backup = sqlite3.connect(str(tmp_path / "wal.db.pre-identity.bak"))
    assert backup.execute(
        "SELECT name, controller, channel FROM pots"
    ).fetchall() == [("basil", "butler1", 3)]


def test_a_rebuild_killed_half_way_leaves_the_old_table_intact(tmp_path, monkeypatch):
    """The whole rebuild is one transaction, or a killed container loses the garden.

    The container is restarted by a NAS reboot, an OOM kill or a power cut
    like any other. If the DROP commits on its own and the refill does not,
    `pots` comes back empty AND in the new shape — which is exactly what
    the idempotence guard reads as "already migrated", so the next start
    does nothing and the loss is permanent and silent.
    """
    path, con = old_db(tmp_path)
    real = butler.new_pot_id
    minted = []

    def killed_on_the_second_pot():
        minted.append(1)
        if len(minted) == 2:
            raise RuntimeError("the container was killed mid-rebuild")
        return real()

    monkeypatch.setattr(butler, "new_pot_id", killed_on_the_second_pot)
    with pytest.raises(RuntimeError):
        migrate(con, path)

    assert [r[1] for r in con.execute("PRAGMA table_info(pots)")][:3] == [
        "id",
        "name",
        "controller",
    ]
    assert con.execute("SELECT COUNT(*) FROM pots").fetchone() == (2,)
    assert con.execute("SELECT COUNT(*) FROM pot_mappings").fetchone() == (0,)

    # And the next start simply retries, as if nothing had happened.
    monkeypatch.setattr(butler, "new_pot_id", real)
    assert migrate(con, path) is True
    assert sorted(n for (n,) in con.execute("SELECT name FROM pots")) == [
        "basil",
        "unmapped",
    ]


def test_the_rebuild_says_what_it_did(tmp_path, capsys):
    """An irreversible one-off that runs unannounced is one nobody can audit.

    It rewrites every pot id, so the operator has to be told it happened,
    how much it moved and where the copy of the old database is.
    """
    path, con = old_db(tmp_path)
    migrate(con, path)

    said = capsys.readouterr().err
    assert "2 pots" in said
    assert "old.db.pre-identity.bak" in said


def test_starting_on_a_live_old_database_rebuilds_it(tmp_path):
    """The rebuild is wired into startup, and the garden survives it.

    Every test above drives migrate() on a hand-built table; this one is
    the only one that exercises the line that actually calls it, and the
    startup ordering it depends on — schema.sql runs first, and creates
    the pots_now view over the OLD pots table, before migrate() drops it.
    Take the call out of create_app and everything above still passes,
    while `GET /pots` answers 503 no such column: p.species and /report
    keeps returning 200, so nothing pages and the garden is simply gone.
    """
    path, con = old_db(tmp_path)
    con.close()

    client = TestClient(
        create_app(db_path=path, token="test-token", next_s=60, cmd_ttl_s=900)
    )
    pots = client.get("/pots").json()["pots"]

    assert [p["name"] for p in pots] == ["basil", "unmapped"]
    basil = pots[0]
    assert basil["id"].startswith("pot-")
    assert (basil["controller"], basil["channel"], basil["outlet"]) == ("butler1", 3, 1)
    assert (basil["dry_raw"], basil["wet_raw"], basil["mode"]) == (13000, 4200, "auto")
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT controller, channel, outlet, from_ts, to_ts FROM pot_mappings "
            "WHERE pot_id = ?",
            (basil["id"],),
        ).fetchall() == [("butler1", 3, 1, 0, None)]
    assert (tmp_path / "old.db.pre-identity.bak").exists()

    # The restart that matters in production: a second container start on
    # the same file must change nothing, ids included.
    again = TestClient(
        create_app(db_path=path, token="test-token", next_s=60, cmd_ttl_s=900)
    )
    assert again.get("/pots").json()["pots"] == pots


# --------------------------------------------------------------------------- #
# Two container starts on one /data
# --------------------------------------------------------------------------- #


def overlapping_start(path, at):
    """A connection standing in for the second of two overlapping starts.

    Container Manager can leave two starts on the same /data briefly. The
    idempotence guard is `PRAGMA table_info(pots)`, read outside any lock,
    so both starts can pass it before either has committed; the WAL-busy
    check does not stop that either, because it only fires while another
    connection is actually holding a read transaction open.

    This connection is the one that passed the guard and lost the race:
    the FIRST container runs its whole rebuild, to completion, the moment
    this one reaches `at` — "backup" just before it takes its own copy,
    "rebuild" once it has its rows and is on its way to BEGIN IMMEDIATE.
    """
    first = sqlite3.connect(path, timeout=5)
    ran = []

    class SecondContainer(sqlite3.Connection):
        def execute(self, sql, *args):
            if at == "backup" and not ran and sql.startswith("PRAGMA wal_checkpoint"):
                ran.append(migrate(first, path))
            return super().execute(sql, *args)

        def executescript(self, sql):
            if at == "rebuild" and not ran:
                ran.append(migrate(first, path))
            return super().executescript(sql)

    return sqlite3.connect(path, timeout=5, factory=SecondContainer), ran


def test_an_overlapping_start_does_not_replace_the_backup(tmp_path):
    """The backup is the only copy of the pre-identity garden. Written once.

    The loser of the race copies the database AFTER the winner has rebuilt
    it, so `<db>.pre-identity.bak` ends up holding an already-migrated
    file and the garden as it was is gone — irreversibly, since the
    rebuild rewrites every pot id.

    Keep, then, rather than overwrite. An existing backup is always a
    valid pre-identity snapshot: it is only ever written while `pots`
    still has its `controller` column, and a rebuild that dies rolls back
    to exactly that shape. Refusing to start would be wrong for the same
    reason — the retry after a killed rebuild finds its own backup there.
    """
    path, seed = old_db(tmp_path)
    seed.close()
    con, ran = overlapping_start(path, at="backup")

    with pytest.raises(sqlite3.OperationalError):
        migrate(con, path)  # its own SELECT finds no `controller` any more
    assert ran == [True]  # the other container really did rebuild

    backup = sqlite3.connect(str(tmp_path / "old.db.pre-identity.bak"))
    assert "controller" in [r[1] for r in backup.execute("PRAGMA table_info(pots)")]
    assert backup.execute("SELECT name FROM pots ORDER BY name").fetchall() == [
        ("basil",),
        ("unmapped",),
    ]


def test_an_overlapping_start_does_not_rebuild_what_was_just_rebuilt(tmp_path):
    """The guard has to be re-read with the write lock held, or it is a
    suggestion.

    This one takes its backup and reads the old rows before the winner
    commits, so both are honest — and then it blocks on BEGIN IMMEDIATE,
    wakes up, and rebuilds the winner's fresh table AGAIN from the rows it
    read. Every pot gets a second id, and the winner's pot_mappings rows
    are left pointing at ids that no longer exist in `pots`: the wiring is
    orphaned and the garden loses its hoses.
    """
    path, seed = old_db(tmp_path)
    seed.close()
    con, ran = overlapping_start(path, at="rebuild")

    assert migrate(con, path) is False  # the work was already done
    assert ran == [True]

    check = sqlite3.connect(path)
    live = {i for (i,) in check.execute("SELECT id FROM pots")}
    assert len(live) == 2
    mapped = [p for (p,) in check.execute("SELECT pot_id FROM pot_mappings")]
    assert mapped and [p for p in mapped if p not in live] == []
