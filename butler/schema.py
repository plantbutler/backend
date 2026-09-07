"""The tables, the ids they are keyed on, and the two ways they change.

`schema.sql` is additive by rule: a CREATE that has already run is never
re-run, so a column added to one never reaches an existing database and
`add_columns` is what carries it in. `migrate` is the single exception — a
one-time rebuild, because a primary key cannot be retyped in place.
"""

import contextlib
import os
import secrets
import shutil
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import wire


SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


def new_pot_id() -> str:
    """`pot-3f9a21`. Random, not sequential, so it can be minted anywhere."""
    return "pot-" + secrets.token_hex(3)


def new_photo_id() -> str:
    """`photo-3f9a21b4`, which is also the filename.

    Four bytes rather than a pot's three: it is all that stands between a
    guessed id and somebody else's picture.
    """
    return "photo-" + secrets.token_hex(4)


# The pre-rebuild pots table's columns, in its own order, so migrate() reads
# an old database without guessing.
_OLD_POT_COLUMNS = (
    "name",
    "controller",
    "channel",
    "outlet",
    "plant_type",
    "plant_size",
    "pot_size",
    "soil",
    "dry_raw",
    "wet_raw",
    "target_low_pct",
    "target_high_pct",
    "dose_ml",
    "mode",
    "cooldown_h",
    "daily_cap_ml",
    "enabled",
)


def _pots_ddl() -> list[str]:
    """The CREATE for `pots` and for `pots_now`, taken from schema.sql itself.

    The rebuild drops both and has to put them back inside its own
    transaction, where executescript() cannot go (it commits first). A second
    copy of the DDL here would drift the day a column is added, so let sqlite
    parse schema.sql in a scratch database and hand back what it made.
    """
    scratch = sqlite3.connect(":memory:")
    scratch.executescript(SCHEMA_SQL)
    ddl = [
        sql
        for (sql,) in scratch.execute(
            "SELECT sql FROM sqlite_master WHERE name IN ('pots', 'pots_now') "
            "ORDER BY type"  # 'table' before 'view': the view reads the table
        )
    ]
    scratch.close()
    return ddl


class Added(NamedTuple):
    """A column schema.sql grew after its CREATE had already run somewhere.

    `source` is the old column its value carries over from (None: nothing to
    carry), read through `convert` on the rows where `gate` — SQL over the
    old row — holds.
    """

    table: str
    column: str
    kind: str
    source: str | None = None
    convert: Callable = lambda v: v
    gate: str = "1"


# Append-only, like the schema itself. The converters that reach for
# cm_from_text are lambdas because it is defined further down and this
# tuple is built at import.
ADDED_COLUMNS = (
    Added("pots", "plant_height_cm", "REAL", "plant_size", lambda v: wire.cm_from_text(v)),
    Added("pots", "pot_diameter_cm", "REAL", "pot_size", lambda v: wire.cm_from_text(v)),
    Added("species_names", "family", "TEXT"),
    # The carry sets the value and cannot repair the wiring: a pot carried
    # over as `graveyard` keeps its open mapping window, where a graveyarding
    # through POST /pot would have closed it.
    Added(
        "pots",
        "status",
        "TEXT NOT NULL DEFAULT 'alive'",
        "enabled",
        lambda flag: "alive" if flag else "graveyard",
    ),
    Added("readings", "pot_id", "TEXT"),
    Added("commands", "pot_id", "TEXT"),
    Added("controllers", "retired", "INTEGER NOT NULL DEFAULT 0"),
    Added("status", "err", "TEXT"),
    Added("status", "err_ts", "INTEGER"),
    Added("status", "latched_ts", "INTEGER"),
    Added("status", "latch_reason", "TEXT"),
    Added("status", "pos_ok_seen", "INTEGER"),
    # A refill's snapshot of the float, and the first drop after it: NULL on
    # rows that predate them, and a NULL snapshot is no origin — a tap that
    # never meant "full to the top" cannot start a tank measurement. The
    # float word and its clocks carry only where the last report actually
    # said float=, a clock without its word reading as a float that never
    # moved; the rise carries only under a word of full, float_since under a
    # word of empty being its fall.
    Added("refills", "float_ok", "INTEGER"),
    Added("refills", "drop_ts", "INTEGER"),
    Added("status", "float_word", "INTEGER", "float_ok"),
    Added(
        "status", "float_word_since", "INTEGER", "float_since", gate="float_ok IS NOT NULL"
    ),
    Added("status", "float_rise", "INTEGER", "float_since", gate="float_ok = 1"),
    Added("status", "contra", "INTEGER NOT NULL DEFAULT 0"),
    Added("status", "float_firm", "INTEGER"),
    Added("status", "float_forced", "INTEGER NOT NULL DEFAULT 0"),
    Added("status", "flap", "INTEGER NOT NULL DEFAULT 0"),
    Added("status", "flap_since", "INTEGER"),
    Added("status", "dry", "INTEGER NOT NULL DEFAULT 0"),
)


def add_columns(con: sqlite3.Connection) -> list[str]:
    """ALTER in whatever of ADDED_COLUMNS this database has not got yet.

    `CREATE TABLE IF NOT EXISTS` is additive about TABLES and nothing else:
    a column appended to a CREATE that has already run on a database never
    reaches it, and the table quietly keeps the shape it was born with. This
    is the additive answer to that, and it is deliberately an ALTER rather
    than a second rebuild — the one rebuild this project has (migrate) stays
    the only one.

    Runs BEFORE schema.sql, because `pots_now` is recreated from that script
    and a view over a column the table has not got yet parses fine and then
    fails on every read.

    The carry-over is best-effort by design: a converter that answers None
    leaves the new column NULL rather than inventing a value.
    """
    added = []
    with con:
        for table, column, kind, source, convert, gate in ADDED_COLUMNS:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            if not cols or column in cols:
                continue  # no such table here yet, or nothing to do
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            added.append(f"{table}.{column}")
            if source is None or source not in cols:
                continue
            for rowid, text in con.execute(
                f"SELECT rowid, {source} FROM {table} "
                f"WHERE {source} IS NOT NULL AND ({gate})"
            ).fetchall():
                value = convert(text)
                if value is not None:
                    con.execute(
                        f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                        (value, rowid),
                    )
    return added


def name_standing_latches(con: sqlite3.Connection) -> int:
    """Put its reason on a standing `latch:<c>` row that carries none.

    A row whose reason differs from the latch's is the tick's cue to page
    again, so a row left with no reason would page every latched board once
    on the first tick after an upgrade, for a fault nobody touched. The
    reason written is the one the latch has now. Runs at every startup and
    touches nothing twice: a named row is left alone, and so is a cleared
    one, which the next latch overwrites with its own reason. Returns how
    many rows it named.
    """
    with con:
        return con.execute(
            "UPDATE alerts SET detail = (SELECT latch_reason FROM status "
            "WHERE 'latch:' || controller = alerts.key) "
            "WHERE key LIKE 'latch:%' AND cleared_ts IS NULL AND detail IS NULL"
        ).rowcount


def migrate(con: sqlite3.Connection, db_path: str) -> bool:
    """The one-time rebuild of `pots`, run at startup. Returns True if it ran.

    `schema.sql` is additive by rule and cannot retype a primary key, which
    is what turning the integer id into `pot-xxxxxx` needs. This is that
    exception, taken once and deliberately: copy into the new shape, move
    the wiring into pot_mappings with from_ts 0 so no history is orphaned,
    then swap. Idempotent — an already-migrated database is recognised by
    the pots table having no `controller` column.
    """
    cols = [r[1] for r in con.execute("PRAGMA table_info(pots)")]
    if not cols or "controller" not in cols:
        return False  # fresh database, or already rebuilt
    backup = None
    if db_path != ":memory:":
        # The live database is WAL, so recent commits sit in the -wal file and
        # a plain copy of the main file would back up all but what is most at
        # risk. Checkpoint first, and refuse the rebuild if anything holds the
        # log open: a deferred migration is recoverable, a DROP behind a short
        # backup is not.
        busy, *_ = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise sqlite3.OperationalError(
                "cannot checkpoint the WAL before rebuilding pots: another "
                "connection is holding it open, and the backup would be short"
            )
        backup = db_path + ".pre-identity.bak"
        # Created exclusively, and kept if it is already there. A backup is
        # only written while `pots` still has its `controller` column and a
        # dying rebuild rolls back to that shape, so an existing one is always
        # a good pre-identity copy — where overwriting could put an ALREADY
        # REBUILT file over the only copy of the garden. Refusing instead of
        # keeping would lose the other way: a retry after a kill must finish.
        try:
            with open(backup, "xb") as copy, open(db_path, "rb") as live:
                shutil.copyfileobj(live, copy)
        except FileExistsError:
            print(
                f"a backup is already at {backup}: keeping it, not rewriting it",
                file=sys.stderr,
            )
        except BaseException:
            # A half-written backup would be kept by every later run.
            with contextlib.suppress(OSError):
                os.unlink(backup)
            raise
    rows = con.execute(
        f"SELECT {', '.join(_OLD_POT_COLUMNS)} FROM pots ORDER BY id"
    ).fetchall()
    # Everything the new shape needs except `pots` and its view arrives here,
    # before the rebuild opens its transaction, because executescript()
    # commits and would otherwise split that transaction in two.
    con.executescript(SCHEMA_SQL)
    con.execute("BEGIN IMMEDIATE")
    with con:
        # The guard again, with the write lock held this time. The one at the
        # top of the function reads outside any lock, so two overlapping
        # container starts on the same /data both pass it; the one that waits
        # here would then rebuild the winner's fresh table from the rows it
        # read before, minting new ids and orphaning the winner's
        # pot_mappings rows against ids that no longer exist.
        if "controller" not in [r[1] for r in con.execute("PRAGMA table_info(pots)")]:
            return False
        # One transaction, DDL included (sqlite rolls that back like any other
        # statement). A container killed mid-rebuild must come back with the
        # old table intact and retry: an empty `pots` in the NEW shape reads
        # to the guard above as "already migrated", and the garden is gone.
        con.execute("DROP VIEW IF EXISTS pots_now")
        con.execute("DROP TABLE pots")
        for ddl in _pots_ddl():
            con.execute(ddl)
        for row in rows:
            old = dict(zip(_OLD_POT_COLUMNS, row))
            # add_columns() does the same for a database past this rebuild;
            # both go through one reader so they cannot disagree.
            old["plant_height_cm"] = wire.cm_from_text(old.pop("plant_size"))
            old["pot_diameter_cm"] = wire.cm_from_text(old.pop("pot_size"))
            old["status"] = "alive" if old.pop("enabled") else "graveyard"
            pot_id = new_pot_id()
            keys = [k for k in old if k not in ("controller", "channel", "outlet")]
            con.execute(
                f"INSERT INTO pots (id, {', '.join(keys)}) "
                f"VALUES (?, {', '.join('?' * len(keys))})",
                [pot_id, *(old[k] for k in keys)],
            )
            if any(old[k] is not None for k in ("controller", "channel", "outlet")):
                con.execute(
                    "INSERT INTO pot_mappings "
                    "(pot_id, controller, channel, outlet, from_ts, to_ts) "
                    "VALUES (?, ?, ?, ?, 0, NULL)",
                    (pot_id, old["controller"], old["channel"], old["outlet"]),
                )
    # Say so: this runs unattended and rewrites every pot id the app and the
    # operator were using. Nobody would find the backup otherwise.
    print(
        f"rebuilt {len(rows)} pots onto random ids and moved their wiring "
        "into pot_mappings"
        + (f"; the database as it was is at {backup}" if backup else ""),
        file=sys.stderr,
    )
    return True
