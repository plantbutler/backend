"""The chart's wire: bucketed raw counts per (controller, channel), server clock."""

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
    """Readings stated outright — (ts, controller, channel, raw) — instead of
    posted and aged, so the test says its timestamps instead of computing them."""
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO readings (ts, controller, channel, raw) VALUES (?, ?, ?, ?)",
            rows,
        )


def test_readings_are_bucketed_with_average_extremes_and_count(client, db):
    now = int(time.time())
    b = (now // 300) * 300 - 600  # a bucket boundary ten minutes back
    plant(
        db,
        [
            (b + 10, "b1", 0, 8000),
            (b + 70, "b1", 0, 8100),
            (b + 130, "b1", 0, 8300),
            (b + 310, "b1", 0, 7000),  # next bucket
            (b + 320, "b1", 1, 1),  # another channel
            (b + 330, "b2", 0, 2),  # another controller
        ],
    )
    answer = client.get("/history?c=b1&ch=0")
    assert answer.status_code == 200
    body = answer.json()
    assert (body["controller"], body["channel"], body["bucket_s"]) == ("b1", 0, 300)
    assert body["since"] % 300 == 0  # a bucket boundary: the first bucket is whole
    assert 0 <= body["to"] - body["since"] - 24 * 3600 < 300
    assert all(p["ts"] >= body["since"] for p in body["points"])
    assert body["points"] == [
        {"ts": b, "raw": 8133, "lo": 8000, "hi": 8300, "n": 3},
        {"ts": b + 300, "raw": 7000, "lo": 7000, "hi": 7000, "n": 1},
    ]


def test_the_window_and_the_bucket_size_are_knobs(client, db):
    now = int(time.time())
    plant(db, [(now - 3 * 3600, "b1", 0, 5000), (now - 30 * 60, "b1", 0, 6000)])
    hour = client.get("/history?c=b1&ch=0&hours=1&bucket_s=60").json()
    assert [p["raw"] for p in hour["points"]] == [6000]
    assert hour["bucket_s"] == 60
    day = client.get("/history?c=b1&ch=0&hours=24&bucket_s=3600").json()
    assert [p["raw"] for p in day["points"]] == [5000, 6000]


def test_a_channel_nobody_reported_is_an_empty_list(client):
    body = client.get("/history?c=b1&ch=0").json()
    assert body["points"] == []
    assert body["to"] >= body["since"]


def test_history_needs_no_token(client, db):
    plant(db, [(int(time.time()) - 60, "b1", 0, 5000)])
    assert client.get("/history?c=b1&ch=0").status_code == 200


@pytest.mark.parametrize(
    "query, why",
    [
        ("ch=0", "no c="),
        ("c=b1", "no ch="),
        ("c=b1&ch=x", "ch= is not an integer"),
        ("c=b1&ch=0&hours=0", "hours= out of range"),
        ("c=b1&ch=0&hours=169", "hours= out of range"),
        ("c=b1&ch=0&bucket_s=59", "bucket_s= out of range"),
        ("c=b1&ch=0&hours=168&bucket_s=60", "too many buckets"),
        ("c=b1&c=b2&ch=0", "c= given twice"),  # last-wins would chart b2
        ("c=b1&ch=0&ch=7", "ch= given twice"),
        ("c=b1&ch=x&ch=0", "ch= given twice"),  # the first value is not skipped
        ("c=b1&ch=0&hours=24&hours=1", "hours= given twice"),
        ("c=b1&ch=0&bucket_s=60&bucket_s=3600", "bucket_s= given twice"),
    ],
)
def test_a_malformed_history_request_is_refused_in_plain_text(client, query, why):
    answer = client.get(f"/history?{query}")
    assert answer.status_code == 400
    assert answer.text.startswith("refused: " + why)


def test_parse_history_defaults_and_cap():
    assert parse_history(QueryParams({"c": "b1", "ch": "0"})) == ("b1", 0, 24, 300)
    assert parse_history(
        QueryParams({"c": "b1", "ch": "5", "hours": "168", "bucket_s": "3600"})
    ) == ("b1", 5, 168, 3600)
    assert parse_history(
        QueryParams({"c": "b1", "ch": "0", "hours": "24", "bucket_s": "60"})
    )[2:] == (24, 60)
    with pytest.raises(ValueError, match="too many buckets"):
        parse_history(
            QueryParams({"c": "b1", "ch": "0", "hours": "168", "bucket_s": "60"})
        )  # 10080


def test_the_whole_window_fits_at_the_default_bucket():
    # 168 h at 300 s is exactly the cap; a finer bucket over the same window is over it.
    assert parse_history(QueryParams({"c": "b1", "ch": "0", "hours": "168"}))[2:] == (
        168,
        300,
    )
    with pytest.raises(ValueError, match="too many buckets"):
        parse_history(
            QueryParams({"c": "b1", "ch": "0", "hours": "168", "bucket_s": "299"})
        )
