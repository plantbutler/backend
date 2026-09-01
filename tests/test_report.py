"""The report endpoint's contract, spelled as the board will exercise it."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from butler import create_app, parse_report

TOKEN = "test-token"

REPORT = "c=butler1 t=123456\nch0=8123 ch1=7902 ch2=15\n"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    app = create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    return TestClient(app)


def rows(db):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT controller, channel, raw FROM readings ORDER BY ts, channel"
        ).fetchall()


def post(client, body, token=TOKEN):
    return client.post("/report", content=body, headers={"X-Token": token})


# --------------------------------------------------------------------------- #
# The good path
# --------------------------------------------------------------------------- #


def test_a_report_lands_whole_and_answers_the_next_interval(client, db):
    answer = post(client, REPORT)

    assert answer.status_code == 200
    assert answer.text == "next=60\n"
    assert rows(db) == [("butler1", 0, 8123), ("butler1", 1, 7902), ("butler1", 2, 15)]


def test_the_server_stamps_arrival_time_itself(client, db):
    post(client, REPORT)

    with sqlite3.connect(db) as con:
        (ts,) = con.execute("SELECT DISTINCT ts FROM readings").fetchone()
    assert abs(time.time() - ts) < 5


def test_keys_this_version_does_not_know_are_ignored(client, db):
    body = "c=butler1 float=1 pos=ok last=ok zz=9 ch0=8123\n"
    answer = post(client, body)

    assert answer.status_code == 200
    assert rows(db) == [("butler1", 0, 8123)]


def test_reports_append_and_health_counts_them(client, db):
    post(client, "c=butler1 t=60000 ch0=1\n")
    post(client, "c=butler1 t=120000 ch0=2\n")

    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["readings"] == 2
    assert [c["controller"] for c in health["controllers"]] == ["butler1"]
    assert health["last_ts"] is not None


# --------------------------------------------------------------------------- #
# The board retries once when a response is lost
# --------------------------------------------------------------------------- #


def test_an_identical_retry_is_answered_200_and_stored_once(client, db):
    first = post(client, REPORT)
    retry = post(client, REPORT)

    assert first.status_code == retry.status_code == 200
    assert len(rows(db)) == 3


def test_a_report_after_a_reboot_reuses_old_uptimes_and_still_lands(client, db):
    post(client, "c=butler1 t=60000 ch0=1\n")
    with sqlite3.connect(db) as con:  # age the first report past the window
        con.execute("UPDATE readings SET ts = ts - 3600")
    answer = post(client, "c=butler1 t=60000 ch0=2\n")

    assert answer.status_code == 200
    assert [r[2] for r in rows(db)] == [1, 2]


def test_a_report_without_t_never_dedups(client, db):
    post(client, "c=butler1 ch0=1\n")
    post(client, "c=butler1 ch0=1\n")

    assert len(rows(db)) == 2


# --------------------------------------------------------------------------- #
# Refusals: the whole report or nothing, and always the right status
# --------------------------------------------------------------------------- #


def test_a_wrong_token_stores_nothing(client, db):
    assert post(client, REPORT, token="nope").status_code == 401
    assert rows(db) == []


def test_a_missing_token_header_stores_nothing(client, db):
    answer = client.post("/report", content=REPORT)

    assert answer.status_code == 401
    assert rows(db) == []


def test_a_non_ascii_token_is_a_401_not_a_500(client, db):
    # Bytes, because that is what actually happens: h11 lets obs-text header
    # bytes through and the ASGI layer decodes them latin-1, so the handler
    # sees a non-ASCII str. compare_digest on str would 500 on it.
    answer = client.post(
        "/report", content=REPORT, headers={"X-Token": "sécret".encode("latin-1")}
    )
    assert answer.status_code == 401
    assert rows(db) == []


@pytest.mark.parametrize(
    "body",
    [
        "ch0=8123\n",  # no controller
        "c=butler1\n",  # no channels: a silent 200 would hide a dead board
        "c=butler1 ch0=8123 ch1=garbage\n",  # non-integer value
        "c=butler1 ch0=9223372036854775808\n",  # 2**63: would overflow sqlite
        "c=butler1 ch999=1\n",  # channel index out of range
        "c=butler1 ch0=1 ch0=2\n",  # duplicate channel: last-wins hides bugs
        "c=butler1 c=butler2 ch0=1\n",  # two controllers in one report
        "c=butler1 ch0=1 notakv\n",  # not k=v
        "c=butler1 t=abc ch0=1\n",  # t= not an integer
        "c= c=evil ch0=1\n",  # empty c= must not open the door to a second
        "c=butler1 ch0=1 =5\n",  # empty key
        "c=butler1 ch0=١٢٣\n",  # Unicode digits: refusal, not repair
        "c=butler1 t=1_000 ch0=5\n",  # int() underscores are not board output
        "c=butler1 ch0=+5\n",  # neither is a leading +
    ],
)
def test_a_malformed_report_is_refused_whole(client, db, body):
    answer = post(client, body)

    assert answer.status_code == 400
    assert answer.text.startswith("refused: ")
    assert rows(db) == []


def test_invalid_utf8_is_refused_not_repaired(client, db):
    answer = client.post(
        "/report", content=b"c=butl\xffer1 ch0=5", headers={"X-Token": TOKEN}
    )

    assert answer.status_code == 400
    assert rows(db) == []


def test_an_oversized_body_is_cut_off_with_413(client, db):
    body = "c=butler1 " + " ".join(f"ch{i % 200}=1" for i in range(2000))
    answer = post(client, body)

    assert answer.status_code == 413
    assert rows(db) == []


def test_unicode_digits_do_not_alias_onto_ascii_channels(client, db):
    body = "c=butler1 ch٧=7 ch0=1\n"  # Arabic-Indic seven: unknown key, skipped
    answer = post(client, body)

    assert answer.status_code == 200
    assert rows(db) == [("butler1", 0, 1)]


# --------------------------------------------------------------------------- #
# Refusals to start
# --------------------------------------------------------------------------- #


def test_a_missing_token_setting_refuses_to_serve(db):
    with pytest.raises(ValueError, match="BUTLER_TOKEN"):
        create_app(db_path=str(db), token="")


def test_a_malformed_interval_refuses_with_its_name_not_a_traceback(db, monkeypatch):
    monkeypatch.setenv("BUTLER_NEXT_S", "sixty")
    with pytest.raises(ValueError, match="BUTLER_NEXT_S"):
        create_app(db_path=str(db), token=TOKEN)


def test_a_data_path_without_a_data_mount_refuses_to_serve():
    # On any machine where /data is not a mountpoint — every laptop, and a
    # container whose bind mount was forgotten — the default path must refuse
    # rather than quietly keep readings in a layer that dies with the container.
    with pytest.raises(ValueError, match="/data"):
        create_app(db_path="/data/butler.db", token=TOKEN)


# --------------------------------------------------------------------------- #
# The parser on its own
# --------------------------------------------------------------------------- #


def test_parse_is_strict_about_shape_and_silent_about_unknowns():
    report = parse_report("c=x unknown=1 t=99 ch7=99")
    assert report[:3] == ("x", {7: 99}, 99)
    with pytest.raises(ValueError):
        parse_report("c=x notakv")
