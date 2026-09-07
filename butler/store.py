"""The two things on the volume: the database handle, and the photographs.

A picture is a row and a file, and the row is the truth — nothing here ever
reads the directory to decide what exists. That is what fixes the direction a
crash or a half-restored backup fails in: keeping writes the file then the
row, deleting removes the row then the file, so what is left over either way
is a file nobody knows about rather than a row nobody can show.

No connection is held across a disk write. A photograph is megabytes over a
NAS volume and sqlite's write transaction opens on the first write, so holding
one for that long is the board's reports blocked.
"""

import contextlib
import os
import sqlite3
from pathlib import Path

from . import constants, schema


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def write_new_file(path: Path, blob: bytes) -> None:
    """Create `path` with `blob`, refusing to overwrite it.

    O_EXCL, so claiming a name and finding it taken is one atomic step: a
    photograph's id is also its filename, and overwriting would destroy an
    earlier picture whose row would then point at nothing.
    """
    with open(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY), "wb") as f:
        f.write(blob)


def photo_path(photos: Path, pot_id: str, photo_id: str) -> Path:
    """Where one picture's bytes live. One directory per pot, so the
    volume stays readable by a person with a file browser and a backup
    of one pot is a directory.

    Both halves are re-checked here rather than trusted from wherever
    they came: this is the only function that turns an id into a path,
    so it is the only place a traversal could get in.
    """
    if not constants.SAFE_ID.fullmatch(pot_id) or not constants.SAFE_ID.fullmatch(
        photo_id
    ):
        raise ValueError("not an id")
    return photos / pot_id / f"{photo_id}.jpg"


def keep_photo(
    db: Path,
    photos: Path,
    pot_id: str,
    blob: bytes,
    w: int | None,
    h: int | None,
    now: int,
) -> str:
    """The bytes, then the row. Returns the new photograph's id.

    A crash between the two leaves a file no row knows about, which
    nothing lists and nothing serves; the other order would leave a row
    whose picture never existed and which the strip would show as missing
    for ever. Neither connection is held across the disk write — a
    photograph is megabytes over a NAS volume, and a write transaction
    held that long is the board's reports blocked.

    The id is claimed by creating its file exclusively, and a taken one is
    tried again. Overwriting first and finding out from the INSERT would
    destroy the picture already at that path, leaving its committed row
    pointing at nothing. A collision comes two ways — the file is there,
    or only the row is — and both must fall out the same way.
    """
    with connect(db) as con:
        row = con.execute(
            "SELECT species FROM pots WHERE id = ?", (pot_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"no such pot: {pot_id}")
    for _ in range(constants.PHOTO_ID_TRIES):
        photo_id = schema.new_photo_id()
        path = photo_path(photos, pot_id, photo_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_new_file(path, blob)
        except FileExistsError:
            continue
        try:
            with connect(db) as con:
                con.execute(
                    "INSERT INTO photos (id, pot_id, ts, bytes, w, h, species) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (photo_id, pot_id, now, len(blob), w, h, row[0]),
                )
        except sqlite3.IntegrityError:
            # The row is there and its file was not: the id belongs to
            # a photograph whose bytes were lost. Leave that row alone
            # and take another id.
            path.unlink(missing_ok=True)
            continue
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return photo_id
    raise sqlite3.IntegrityError(
        f"could not mint a free photograph id in {constants.PHOTO_ID_TRIES} tries"
    )


def photo_rows(db: Path, photos: Path, pot_id: str, limit: int) -> list[dict]:
    """One pot's strip, newest first, straight from the rows.

    `missing` is the one thing the disk is asked: a row whose file has
    gone — a half-restored backup, a volume that came back empty — is
    listed and said to be missing rather than served as a picture that
    will not load.
    """
    with connect(db) as con:
        rows = con.execute(
            "SELECT id, ts, bytes, w, h, species FROM photos "
            "WHERE pot_id = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (pot_id, limit),
        ).fetchall()
    return [
        {
            "id": photo_id,
            "ts": ts,
            "bytes": size,
            "w": w,
            "h": h,
            "species": species,
            "missing": not photo_path(photos, pot_id, photo_id).exists(),
        }
        for photo_id, ts, size, w, h, species in rows
    ]


def photo_blob(db: Path, photos: Path, photo_id: str) -> bytes:
    """The picture itself, found through its row and never through the
    directory: a file nothing here minted is not reachable by guessing
    its name."""
    with connect(db) as con:
        row = con.execute(
            "SELECT pot_id FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"no such photo: {photo_id}")
    try:
        return photo_path(photos, row[0], photo_id).read_bytes()
    except OSError:
        raise ValueError(f"{photo_id} is listed but its file is gone") from None


def forget_photo(db: Path, photos: Path, photo_id: str) -> None:
    """The row, then the file — the opposite order to keeping one, and for
    the same reason: whichever way a crash lands, what is left over is a
    file nobody knows about rather than a row nobody can show. The person
    said the picture is gone, so it leaves the listing even if the volume
    refuses to give up the bytes."""
    with connect(db) as con:
        row = con.execute(
            "SELECT pot_id FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such photo: {photo_id}")
        # The DELETE decides, not the SELECT before it: two deletes of one
        # photograph can both see the row — a bare SELECT takes no lock —
        # and only the one that removed it may answer ok.
        if con.execute("DELETE FROM photos WHERE id = ?", (photo_id,)).rowcount == 0:
            raise ValueError(f"no such photo: {photo_id}")
    with contextlib.suppress(OSError):
        photo_path(photos, row[0], photo_id).unlink(missing_ok=True)
