"""The chart's wire: bucketed raw counts for one pot, on the server clock."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from butler import create_app, parse_history

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def client(db):
    return TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )


def plant(db, rows):
    """Readings stated outright — (ts, controller, channel, raw, pot_id) —
    instead of posted and aged, so the test says its timestamps instead of
    computing them. The pot stamp is written here because the chart reads it;
    what puts it there in production is handle_report (see test_report)."""
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO readings (ts, controller, channel, raw, pot_id) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def test_readings_are_bucketed_with_average_extremes_and_count(client, db):
    now = int(time.time())
    b = (now // 300) * 300 - 600  # a bucket boundary ten minutes back
    plant(
        db,
        [
            (b + 10, "b1", 0, 8000, "pot-aaaaaa"),
            (b + 70, "b1", 0, 8100, "pot-aaaaaa"),
            (b + 130, "b1", 0, 8300, "pot-aaaaaa"),
            (b + 310, "b1", 0, 7000, "pot-aaaaaa"),  # next bucket
            (b + 320, "b1", 1, 1, "pot-bbbbbb"),  # another pot
            (b + 330, "b1", 0, 2, None),  # the same socket, nobody on it
            (b + 340, "b2", 0, 2, "pot-bbbbbb"),  # the other pot, elsewhere
        ],
    )
    answer = client.get("/history?pot=pot-aaaaaa")
    assert answer.status_code == 200
    body = answer.json()
    assert (body["pot"], body["bucket_s"]) == ("pot-aaaaaa", 300)
    assert body["since"] % 300 == 0  # a bucket boundary: the first bucket is whole
    assert 0 <= body["to"] - body["since"] - 24 * 3600 < 300
    assert all(p["ts"] >= body["since"] for p in body["points"])
    assert body["points"] == [
        {"ts": b, "raw": 8133, "lo": 8000, "hi": 8300, "n": 3},
        {"ts": b + 300, "raw": 7000, "lo": 7000, "hi": 7000, "n": 1},
    ]


def test_the_window_and_the_bucket_size_are_knobs(client, db):
    now = int(time.time())
    plant(
        db,
        [
            (now - 3 * 3600, "b1", 0, 5000, "pot-aaaaaa"),
            (now - 30 * 60, "b1", 0, 6000, "pot-aaaaaa"),
        ],
    )
    hour = client.get("/history?pot=pot-aaaaaa&hours=1&bucket_s=60").json()
    assert [p["raw"] for p in hour["points"]] == [6000]
    assert hour["bucket_s"] == 60
    day = client.get("/history?pot=pot-aaaaaa&hours=24&bucket_s=3600").json()
    assert [p["raw"] for p in day["points"]] == [5000, 6000]


def test_a_pot_nobody_reported_for_is_an_empty_list(client):
    """And a 200, not a 404: a pot with no readings yet and a pot that never
    existed look the same from here on purpose, so an unauthenticated caller
    learns nothing from the status code."""
    body = client.get("/history?pot=pot-aaaaaa").json()
    assert body["points"] == []
    assert body["to"] >= body["since"]


def test_a_reading_nobody_was_mapped_for_belongs_to_no_chart(client, db):
    """An environment channel, or a socket nobody has claimed. The row is
    kept — raw counts always are — and no pot's chart shows it."""
    plant(db, [(int(time.time()) - 60, "b1", 0, 5000, None)])
    assert client.get("/history?pot=pot-aaaaaa").json()["points"] == []


def test_history_needs_no_token(client, db):
    plant(db, [(int(time.time()) - 60, "b1", 0, 5000, "pot-aaaaaa")])
    assert client.get("/history?pot=pot-aaaaaa").status_code == 200


@pytest.mark.parametrize(
    "query, why",
    [
        ("hours=24", "no pot="),
        ("pot=", "no pot="),
        ("pot=pot-aaaaaa&hours=0", "hours= out of range"),
        ("pot=pot-aaaaaa&hours=745", "hours= out of range"),  # a month is the ceiling
        ("pot=pot-aaaaaa&bucket_s=59", "bucket_s= out of range"),
        ("pot=pot-aaaaaa&hours=168&bucket_s=60", "too many buckets"),
        ("pot=pot-a&pot=pot-b", "pot= given twice"),  # last-wins would chart pot-b
        ("pot=pot-aaaaaa&hours=24&hours=1", "hours= given twice"),
        ("pot=pot-aaaaaa&bucket_s=60&bucket_s=3600", "bucket_s= given twice"),
    ],
)
def test_a_malformed_history_request_is_refused_in_plain_text(client, query, why):
    answer = client.get(f"/history?{query}")
    assert answer.status_code == 400
    assert answer.text.startswith("refused: " + why)


def test_parse_history_defaults_and_cap():
    assert parse_history(QueryParams({"pot": "pot-aaaaaa"})) == ("pot-aaaaaa", 24, 300)
    assert parse_history(
        QueryParams({"pot": "pot-aaaaaa", "hours": "168", "bucket_s": "3600"})
    ) == ("pot-aaaaaa", 168, 3600)
    assert parse_history(
        QueryParams({"pot": "pot-aaaaaa", "hours": "24", "bucket_s": "60"})
    )[1:] == (24, 60)
    with pytest.raises(ValueError, match="too many buckets"):
        parse_history(
            QueryParams({"pot": "pot-aaaaaa", "hours": "168", "bucket_s": "60"})
        )  # 10080


def test_the_whole_window_fits_at_the_default_bucket():
    # 168 h at 300 s is exactly the cap; a finer bucket over the same window is over it.
    assert parse_history(QueryParams({"pot": "pot-aaaaaa", "hours": "168"}))[1:] == (
        168,
        300,
    )
    with pytest.raises(ValueError, match="too many buckets"):
        parse_history(
            QueryParams({"pot": "pot-aaaaaa", "hours": "168", "bucket_s": "299"})
        )


def test_a_month_back_is_askable_at_a_sane_bucket(client, db):
    """The app's widest chart window. A month at hourly buckets is 744
    points; the bucket cap, not the hours cap, is what still bounds this."""
    now = int(time.time())
    plant(
        db,
        [
            (now - 25 * 24 * 3600, "b1", 0, 8000, "pot-aaaaaa"),
            (now - 60, "b1", 0, 9000, "pot-aaaaaa"),
        ],
    )
    answer = client.get(
        "/history", params={"pot": "pot-aaaaaa", "hours": 744, "bucket_s": 3600}
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["bucket_s"] == 3600
    assert [p["raw"] for p in body["points"]] == [8000, 9000]
    assert body["to"] - body["since"] >= 743 * 3600


def test_past_a_month_is_still_refused(client):
    answer = client.get("/history", params={"pot": "pot-aaaaaa", "hours": 745, "bucket_s": 3600})
    assert answer.status_code == 400
    assert "hours" in answer.text


def test_a_month_at_five_minute_buckets_is_still_too_many(client):
    """Raising the hours cap must not let the bucket cap be walked past."""
    answer = client.get("/history", params={"pot": "pot-aaaaaa", "hours": 744, "bucket_s": 300})
    assert answer.status_code == 400
    assert "too many buckets" in answer.text
