"""A picture of the plant, over time.

Bytes on the volume, rows in SQLite, and the row is the truth. The tests
that matter here are the ones about the two coming apart: a file with no
row, a row with no file, and which of the two a crash or a half-restored
backup is allowed to leave behind.
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import butler
from butler import PHOTO_CAP, create_app

TOKEN = "test-token"

# A one-pixel JPEG, near enough: the backend checks the first three bytes
# and never opens the image, so the rest only has to be bytes.
JPEG = b"\xff\xd8\xff\xe0" + b"plant" * 20 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"plant" * 20


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def store(tmp_path):
    return tmp_path / "pictures"


@pytest.fixture
def client(db, store):
    return TestClient(
        create_app(db_path=str(db), token=TOKEN, photos_dir=str(store))
    )


def auth(token=TOKEN):
    return {} if token is None else {"X-Token": token}


def make_pot(client, body="name=basil"):
    answer = client.post("/pot", content=body, headers=auth())
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def upload(client, pot_id, blob=JPEG, token=TOKEN, query=""):
    return client.post(
        f"/photo?pot={pot_id}{query}", content=blob, headers=auth(token)
    )


def photo_id(answer):
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("photo=")


def strip(client, pot_id, token=TOKEN, query=""):
    return client.get(f"/photos?pot={pot_id}{query}", headers=auth(token))


def fetch(client, pid, token=TOKEN):
    return client.get(f"/photo/{pid}", headers=auth(token))


def forget(client, pid, token=TOKEN):
    return client.post("/photo/delete", content=f"photo={pid}", headers=auth(token))


# --------------------------------------------------------------------------- #
# The ordinary path


def test_a_photograph_goes_up_and_comes_back_byte_for_byte(client):
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    got = fetch(client, pid)
    assert got.status_code == 200
    assert got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg"
    # The store holds JPEGs only, checked on the way in, and this says so:
    # no browser gets to sniff a picture into markup.
    assert got.headers["x-content-type-options"] == "nosniff"


def test_the_strip_lists_what_was_uploaded(client):
    pot = make_pot(client, "name=basil species=Ocimum_basilicum")
    pid = photo_id(upload(client, pot, query="&w=1600&h=1200"))
    answer = strip(client, pot).json()
    assert answer["pot"] == pot
    assert answer["more"] is False
    (row,) = answer["photos"]
    assert row["id"] == pid
    assert row["bytes"] == len(JPEG)
    assert (row["w"], row["h"]) == (1600, 1200)
    assert row["species"] == "Ocimum_basilicum"
    assert row["missing"] is False
    assert row["ts"] > 0


def test_the_newest_picture_is_first(client):
    pot = make_pot(client)
    first = photo_id(upload(client, pot))
    second = photo_id(upload(client, pot))
    listed = [row["id"] for row in strip(client, pot).json()["photos"]]
    # Both land in the same second, so this is the tiebreak doing the work,
    # not the clock: without the rowid the order would be arbitrary and the
    # strip would shuffle itself between refreshes.
    assert listed == [second, first]


def test_a_pot_sees_only_its_own_pictures(client):
    basil = make_pot(client, "name=basil")
    fern = make_pot(client, "name=fern")
    mine = photo_id(upload(client, basil))
    upload(client, fern)
    assert [row["id"] for row in strip(client, basil).json()["photos"]] == [mine]


def test_the_file_lands_under_the_pot_s_own_directory(client, store):
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    assert (store / pot / f"{pid}.jpg").read_bytes() == JPEG
    # And nothing half-written is left beside it.
    assert list((store / pot).glob("*.part")) == []


def test_the_species_of_the_day_is_stamped_on_the_picture(client):
    """A pot outlives its plant. The strip draws the break where one plant
    ended and the next began, and this is what it draws it from — no
    replant event, just what the pot said it was at the time."""
    pot = make_pot(client, "name=windowsill species=Ocimum_basilicum")
    upload(client, pot)
    client.post(
        "/pot", content=f"id={pot} species=Monstera_deliciosa", headers=auth()
    )
    upload(client, pot)
    species = [row["species"] for row in strip(client, pot).json()["photos"]]
    assert species == ["Monstera_deliciosa", "Ocimum_basilicum"]


# --------------------------------------------------------------------------- #
# The token. These are the only gated reads in the service.


@pytest.mark.parametrize("token", [None, "wrong"])
def test_every_photo_route_wants_the_token(client, token):
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    assert upload(client, pot, token=token).status_code == 401
    assert strip(client, pot, token=token).status_code == 401
    assert fetch(client, pid, token=token).status_code == 401
    assert forget(client, pid, token=token).status_code == 401
    # And a refused delete really did not delete.
    assert fetch(client, pid).status_code == 200


def test_a_wrong_token_cannot_tell_a_real_photo_id_from_a_made_up_one(client):
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    assert fetch(client, pid, token="wrong").text == fetch(
        client, "photo-00000000", token="wrong"
    ).text


# --------------------------------------------------------------------------- #
# Refusals


def test_a_photograph_of_a_pot_that_does_not_exist_is_refused(client, store):
    answer = upload(client, "pot-abcdef")
    assert answer.status_code == 400
    assert "no such pot" in answer.text
    # Refused before anything was written: an orphan file is harmless but
    # this one would never even be an accident.
    assert list(store.rglob("*.jpg")) == []


def test_only_a_jpeg_gets_in(client):
    pot = make_pot(client)
    answer = upload(client, pot, blob=PNG)
    assert answer.status_code == 400
    assert "not a JPEG" in answer.text
    assert strip(client, pot).json()["photos"] == []


def test_an_empty_body_is_not_a_photograph(client):
    pot = make_pot(client)
    assert upload(client, pot, blob=b"").status_code == 400


def test_a_picture_bigger_than_the_cap_is_refused(client):
    pot = make_pot(client)
    fat = JPEG + b"\x00" * PHOTO_CAP
    answer = upload(client, pot, blob=fat)
    assert answer.status_code == 413
    assert strip(client, pot).json()["photos"] == []


def test_a_report_sized_body_still_fits_the_report_cap(client):
    """The photo cap is the photo route's own; raising it must not have
    quietly raised everybody else's."""
    answer = client.post(
        "/report", content="c=0 " + "x" * butler.BODY_CAP, headers=auth()
    )
    assert answer.status_code == 413


def test_no_pot_at_all_is_refused(client):
    assert client.post("/photo", content=JPEG, headers=auth()).status_code == 400


def test_a_pot_given_twice_is_refused(client):
    pot = make_pot(client)
    answer = client.post(
        f"/photo?pot={pot}&pot={pot}", content=JPEG, headers=auth()
    )
    assert answer.status_code == 400
    assert "given twice" in answer.text


@pytest.mark.parametrize("query", ["&w=0", "&w=99999", "&w=big", "&h=-1"])
def test_an_impossible_size_is_refused(client, query):
    pot = make_pot(client)
    assert upload(client, pot, query=query).status_code == 400


@pytest.mark.parametrize("limit", ["0", "501", "lots", "-1"])
def test_an_impossible_limit_is_refused(client, limit):
    pot = make_pot(client)
    assert strip(client, pot, query=f"&limit={limit}").status_code == 400


def test_the_biggest_limit_the_refusal_names_can_actually_be_asked_for(client):
    """`_int_in`'s top is exclusive, so every named maximum in this service
    needs a +1 to mean what its own refusal says it means. Without this
    test the off-by-one is invisible: 501 is refused either way."""
    pot = make_pot(client)
    assert strip(client, pot, query="&limit=500").status_code == 200


def test_the_limit_pages_and_says_there_is_more(client):
    pot = make_pot(client)
    for _ in range(3):
        upload(client, pot)
    answer = strip(client, pot, query="&limit=2").json()
    assert len(answer["photos"]) == 2
    assert answer["more"] is True
    assert strip(client, pot, query="&limit=3").json()["more"] is True  # exactly full
    assert strip(client, pot).json()["more"] is False


# --------------------------------------------------------------------------- #
# Ids that are really paths


@pytest.mark.parametrize("nasty", ["../../etc", "a/b", "..", "pot%2F..%2Fx"])
def test_a_pot_id_cannot_climb_out_of_the_store(client, store, nasty, tmp_path):
    answer = client.post(f"/photo?pot={nasty}", content=JPEG, headers=auth())
    assert answer.status_code in (400, 404), answer.text
    assert list(tmp_path.rglob("*.jpg")) == []


@pytest.mark.parametrize("nasty", ["..", "%2e%2e"])
def test_a_photo_id_cannot_climb_out_of_the_store(client, nasty):
    assert fetch(client, nasty).status_code in (400, 404)


def test_a_photo_id_that_is_not_an_id_is_refused_before_the_database(client):
    answer = fetch(client, "photo id with spaces")
    assert answer.status_code == 400


def test_a_delete_of_something_that_is_not_an_id_is_refused(client):
    answer = client.post("/photo/delete", content="photo=../x", headers=auth())
    assert answer.status_code == 400


# --------------------------------------------------------------------------- #
# The two ways bytes and rows come apart


def test_a_row_whose_file_has_gone_is_listed_as_missing_not_served(client, store):
    """What a database restored from a newer backup than the volume looks
    like. It cannot be hidden, so it is said out loud rather than served as
    a picture that will not load."""
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    (store / pot / f"{pid}.jpg").unlink()
    (row,) = strip(client, pot).json()["photos"]
    assert row["id"] == pid
    assert row["missing"] is True
    gone = fetch(client, pid)
    assert gone.status_code == 404
    assert "its file is gone" in gone.text


def test_a_file_no_row_knows_about_is_invisible(client, store):
    """The other direction — a crash between the two writes, or a volume
    restored from a newer backup than the database. Nothing lists it and
    nothing serves it: the strip is built from rows, never from the
    directory."""
    pot = make_pot(client)
    orphan = store / pot
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "photo-deadbeef.jpg").write_bytes(JPEG)
    assert strip(client, pot).json()["photos"] == []
    assert fetch(client, "photo-deadbeef").status_code == 404


def test_a_failed_row_write_takes_its_file_with_it(client, db, store, monkeypatch):
    """The bytes are written first, so this is the window where an orphan
    could be minted deliberately rather than by a crash. The table is taken
    away in that very window: the file is on disk, and the row that was
    about to point at it never lands."""
    pot = make_pot(client)
    real = butler.write_new_file

    def then_break(path, blob):
        real(path, blob)
        with sqlite3.connect(db) as con:
            con.execute("DROP TABLE photos")

    monkeypatch.setattr(butler, "write_new_file", then_break)
    answer = upload(client, pot)
    monkeypatch.undo()
    assert answer.status_code == 503, answer.text
    assert list(store.rglob("*.jpg")) == []


# --------------------------------------------------------------------------- #
# Deleting


def test_deleting_takes_the_row_and_the_file(client, store):
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    keep = photo_id(upload(client, pot))
    assert forget(client, pid).status_code == 200
    assert [row["id"] for row in strip(client, pot).json()["photos"]] == [keep]
    assert not (store / pot / f"{pid}.jpg").exists()
    assert (store / pot / f"{keep}.jpg").exists()
    assert fetch(client, pid).status_code == 404


def test_deleting_twice_is_refused_rather_than_pretended(client):
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    assert forget(client, pid).status_code == 200
    again = forget(client, pid)
    assert again.status_code == 400
    assert "no such photo" in again.text


def test_a_row_whose_file_is_already_gone_can_still_be_deleted(client, store):
    """The listing is what the person is looking at, so the listing is what
    has to go. A volume that will not give up the bytes is not a reason to
    keep showing a picture somebody asked to remove."""
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    (store / pot / f"{pid}.jpg").unlink()
    assert forget(client, pid).status_code == 200
    assert strip(client, pot).json()["photos"] == []


def test_a_delete_without_a_photo_is_refused(client):
    assert client.post("/photo/delete", content="", headers=auth()).status_code == 400
    assert (
        client.post("/photo/delete", content="pot=x", headers=auth()).status_code == 400
    )


# --------------------------------------------------------------------------- #
# The lock. A photograph is megabytes over a NAS volume.


def test_the_write_lock_is_not_held_across_the_disk_write(client, db, monkeypatch):
    """Uploading a picture must not stop the garden reporting. The bytes go
    to disk between two short connections, not inside one: a write
    transaction held for the length of a multi-megabyte write to a NAS
    volume would answer every board report in that window with "try
    again"."""
    wrote = []
    real = butler.write_new_file

    def slow(path, blob):
        # Stands in for POST /report landing mid-upload: a writer with a
        # short timeout, which fails outright if the lock is held.
        with sqlite3.connect(db, timeout=0.2) as other:
            other.execute(
                "INSERT INTO readings (ts, controller, channel, raw) "
                "VALUES (1, 0, 0, 8000)"
            )
        wrote.append(len(blob))
        real(path, blob)

    pot = make_pot(client)
    monkeypatch.setattr(butler, "write_new_file", slow)
    answer = upload(client, pot)
    monkeypatch.undo()
    assert answer.status_code == 200, answer.text
    assert wrote == [len(JPEG)]


def test_two_uploads_at_once_both_land(client):
    """Two phones, or one impatient thumb. Ids are minted per upload, so
    neither may overwrite the other's file or row."""
    pot = make_pot(client)
    answers = []
    lock = threading.Lock()

    def go():
        answer = upload(client, pot)
        with lock:
            answers.append(answer)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [a.status_code for a in answers] == [200] * 4
    ids = {photo_id(a) for a in answers}
    assert len(ids) == 4
    assert {row["id"] for row in strip(client, pot).json()["photos"]} == ids


# --------------------------------------------------------------------------- #
# Where the store lives


def test_the_store_sits_beside_the_database_by_default(tmp_path):
    db = tmp_path / "here" / "butler.db"
    client = TestClient(create_app(db_path=str(db), token=TOKEN))
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    assert (tmp_path / "here" / "photos" / pot / f"{pid}.jpg").exists()


def test_a_photo_store_under_an_unmounted_data_is_refused(tmp_path, monkeypatch):
    """The same refusal the database gets, for the same reason: a forgotten
    bind mount would store photographs in the container's own layer and
    lose every one of them on the next recreate, while looking healthy."""
    monkeypatch.setattr(butler.os.path, "ismount", lambda path: False)
    with pytest.raises(ValueError, match="not a mounted volume"):
        create_app(
            db_path=str(tmp_path / "butler.db"),
            token=TOKEN,
            photos_dir="/data/photos",
        )


# --------------------------------------------------------------------------- #
# Two photographs that want the same id


def test_a_taken_id_is_given_up_rather_than_overwritten(client, store, monkeypatch):
    """The dangerous half of a collision. Writing the file first and finding
    out from the INSERT would destroy the picture already at that path — an
    earlier photograph, already committed, whose row would then be left
    pointing at nothing."""
    pot = make_pot(client)
    minted = iter(["photo-aaaaaaaa", "photo-aaaaaaaa", "photo-bbbbbbbb"])
    monkeypatch.setattr(butler, "new_photo_id", lambda: next(minted))
    first = photo_id(upload(client, pot, blob=JPEG))
    second_bytes = JPEG + b"different"
    second = photo_id(upload(client, pot, blob=second_bytes))
    monkeypatch.undo()
    assert first == "photo-aaaaaaaa"
    assert second == "photo-bbbbbbbb"
    # The first photograph is untouched, which is the whole point.
    assert fetch(client, first).content == JPEG
    assert fetch(client, second).content == second_bytes
    assert not any(row["missing"] for row in strip(client, pot).json()["photos"])


def test_an_id_whose_row_outlived_its_file_is_also_given_up(client, store, monkeypatch):
    """The other way two ids collide: the file is gone but the row is not,
    which is what a half-restored backup leaves. The new picture must not
    take that row's id and must not touch that row."""
    pot = make_pot(client)
    minted = iter(["photo-aaaaaaaa", "photo-aaaaaaaa", "photo-bbbbbbbb"])
    monkeypatch.setattr(butler, "new_photo_id", lambda: next(minted))
    first = photo_id(upload(client, pot))
    (store / pot / f"{first}.jpg").unlink()  # the file, not the row
    second = photo_id(upload(client, pot))
    monkeypatch.undo()
    assert second == "photo-bbbbbbbb"
    rows = {row["id"]: row["missing"] for row in strip(client, pot).json()["photos"]}
    assert rows == {"photo-aaaaaaaa": True, "photo-bbbbbbbb": False}


def test_an_id_that_cannot_be_minted_is_a_try_again_and_not_a_500(client, monkeypatch):
    pot = make_pot(client)
    monkeypatch.setattr(butler, "new_photo_id", lambda: "photo-aaaaaaaa")
    photo_id(upload(client, pot))
    answer = upload(client, pot)
    monkeypatch.undo()
    assert answer.status_code == 503, answer.text
    assert "try again" in answer.text


def test_only_one_of_two_racing_deletes_says_ok(client):
    """A bare SELECT takes no lock, so both callers can see the row. The
    DELETE decides, or "deleting twice is refused rather than pretended"
    would hold only when nobody is in a hurry."""
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    codes = []
    lock = threading.Lock()

    def go():
        answer = forget(client, pid)
        with lock:
            codes.append(answer.status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] + [400] * 5, codes
    assert strip(client, pot).json()["photos"] == []


def test_an_empty_photos_dir_falls_back_like_an_empty_db_path(tmp_path):
    """`db_path=""` falls back to the default; this has to do the same, or
    the store lands in the process's working directory and skips the
    unmounted-/data refusal on the way."""
    db = tmp_path / "here" / "butler.db"
    client = TestClient(create_app(db_path=str(db), token=TOKEN, photos_dir=""))
    pot = make_pot(client)
    pid = photo_id(upload(client, pot))
    assert (tmp_path / "here" / "photos" / pot / f"{pid}.jpg").exists()


def garden(client):
    answer = client.get("/pots", headers=auth())
    assert answer.status_code == 200, answer.text
    return answer.json()["pots"]


def test_the_garden_carries_the_newest_picture_for_the_thumbnail(client, db):
    """The list shows a small picture beside each name, so /pots has to say
    which one — the id only, since the bytes come from GET /photo/<id> and
    the app caches those. Newest, because a plant's most recent portrait is
    the one that says what it looks like now."""
    pot_id = make_pot(client)
    assert garden(client)[0]["photo"] is None, "nothing photographed yet"

    first = photo_id(upload(client, pot_id))
    assert garden(client)[0]["photo"] == first

    second = photo_id(upload(client, pot_id))
    assert garden(client)[0]["photo"] == second, "the newest one"

    # Forgetting it falls back to the one before, rather than to a broken
    # picture: the row is the truth, and there is another row.
    client.post("/photo/delete", content=f"photo={second}", headers=auth())
    assert garden(client)[0]["photo"] == first


def test_a_thumbnail_belongs_to_its_own_pot(client, db):
    basil = make_pot(client)
    mint = make_pot(client, "name=mint")
    shot = photo_id(upload(client, basil))

    by_name = {p["id"]: p["photo"] for p in garden(client)}
    assert by_name[basil] == shot
    assert by_name[mint] is None
