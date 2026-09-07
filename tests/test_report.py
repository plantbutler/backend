"""The report endpoint's contract, spelled as the board will exercise it."""

import time

import pytest
from fastapi.testclient import TestClient

from butler import create_app, parse_report
from conftest import TOKEN, count, minted, post, run_sql


REPORT = "c=0 t=123456\nch0=8123 ch1=7902 ch2=15\n"


def rows(db):
    return run_sql(db, "SELECT controller, channel, raw FROM readings ORDER BY ts, channel")


def stamps(db):
    """(channel, pot_id) per reading, oldest first: whose reading each one is."""
    return run_sql(db, "SELECT channel, pot_id FROM readings ORDER BY ts, channel")


def reported(client, body, token=TOKEN):
    """One report, and the answer left unjudged: most of this file is about
    what the endpoint refuses, so conftest's `report` and its 200 will not do."""
    return post(client, "/report", body, token)


def pot(client, body):
    return minted(post(client, "/pot", body))


def test_the_controller_is_an_integer_and_zero_is_a_real_board(client, db):
    """A typo in a free-text `c=` could open a second controller row with its
    own heartbeat and alerts, silently. Board 0 is the trap inside the trap:
    it is falsy, and the app fills it in by default, so `if not controller`
    would refuse the commonest board there is."""
    assert reported(client, "c=0 ch0=8000").status_code == 200
    assert run_sql(db, "SELECT controller FROM readings") == [(0,)]


@pytest.mark.parametrize(
    "body, why",
    [
        ("c=bench1 ch0=1", "c= is not an integer"),
        ("c= ch0=1", "c= is not an integer"),
        ("c=-1 ch0=1", "c= is not an integer"),  # the minus is not a digit
        ("c=256 ch0=1", "c= out of range"),
        ("c=0x10 ch0=1", "c= is not an integer"),
        ("c=٣ ch0=1", "c= is not an integer"),  # Arabic-Indic 3: int() would take it
    ],
)
def test_a_controller_that_is_not_a_number_is_refused(client, db, body, why):
    answer = reported(client, body)
    assert answer.status_code == 400
    assert answer.text.startswith("refused: " + why), answer.text
    assert count(db, "readings") == 0


# --------------------------------------------------------------------------- #
# Whose reading it is: stamped as the row lands
# --------------------------------------------------------------------------- #


def test_a_reading_carries_the_pot_that_was_on_that_channel(client, db):
    basil = pot(client, "name=basil controller=0 channel=0")
    reported(client, REPORT)
    # ch1 and ch2 are sockets nobody has claimed: NULL is the honest answer
    assert stamps(db) == [(0, basil), (1, None), (2, None)]


def test_a_remap_changes_what_is_stamped_next_and_leaves_the_past_alone(client, db):
    """A plant moved to another socket takes its old readings with it, and
    the pot that arrives on the socket it left does not inherit them."""
    basil = pot(client, "name=basil controller=0 channel=0")
    reported(client, "c=0 t=1\nch0=8000\n")
    pot(client, f"id={basil} channel=1")
    mint = pot(client, "name=mint controller=0 channel=0")
    reported(client, "c=0 t=2\nch0=7000 ch1=6000\n")

    assert stamps(db) == [(0, basil), (0, mint), (1, basil)]


def test_a_buried_pots_channel_stamps_nobody(client, db):
    """Burying a pot closes its window, so a reading that arrives on that
    socket afterwards belongs to no plant."""
    basil = pot(client, "name=basil controller=0 channel=0")
    reported(client, "c=0 t=1\nch0=8000\n")
    client.post(
        "/pot", content=f"id={basil} status=graveyard", headers={"X-Token": TOKEN}
    )
    reported(client, "c=0 t=2\nch0=8000\n")

    assert stamps(db) == [(0, basil), (0, None)]


def test_a_retry_still_dedups_when_nothing_is_mapped(client, db):
    """The dedup probe is on (controller, t), not on pots: an unmapped
    board must not write its readings twice."""
    reported(client, REPORT)
    reported(client, REPORT)
    assert len(stamps(db)) == 3


# --------------------------------------------------------------------------- #
# The good path
# --------------------------------------------------------------------------- #


def test_a_report_lands_whole_and_answers_the_next_interval(client, db):
    answer = reported(client, REPORT)

    assert answer.status_code == 200
    assert answer.text == "next=60\n"
    assert rows(db) == [(0, 0, 8123), (0, 1, 7902), (0, 2, 15)]


def test_the_server_stamps_arrival_time_itself(client, db):
    reported(client, REPORT)

    ((ts,),) = run_sql(db, "SELECT DISTINCT ts FROM readings")
    assert abs(time.time() - ts) < 5


@pytest.mark.parametrize(
    "body, landed",
    [
        pytest.param(
            "c=0 float=1 pos=ok last=ok zz=9 ch0=8123\n",
            [(0, 0, 8123)],
            id="keys_this_version_does_not_know",
        ),
        pytest.param(
            "c=0 ch\u0667=7 ch0=1\n",  # Arabic-Indic seven: an unknown key
            [(0, 0, 1)],
            id="unicode_digits_do_not_alias_onto_ascii_channels",
        ),
    ],
)
def test_a_key_this_version_cannot_read_is_skipped_and_the_rest_lands(client, db, body, landed):
    answer = reported(client, body)

    assert answer.status_code == 200
    assert rows(db) == landed


def test_reports_append_and_health_counts_them(client, db):
    reported(client, "c=0 t=60000 ch0=1\n")
    reported(client, "c=0 t=120000 ch0=2\n")

    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["readings"] == 2
    assert [c["controller"] for c in health["controllers"]] == [0]
    assert health["last_ts"] is not None


# --------------------------------------------------------------------------- #
# The board retries once when a response is lost
# --------------------------------------------------------------------------- #


def test_health_reports_the_default_interval_not_an_override(db):
    # a non-default next_s, to tell the configured value from the code's own literal
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=45, cmd_ttl_s=900)
    )
    reported(client, "c=0 ch0=1\n")
    knob = client.post("/interval", content="c=0 next=120", headers={"X-Token": TOKEN})
    assert knob.status_code == 200
    health = client.get("/health").json()
    assert health["next_default"] == 45
    assert (
        health["controllers"][0]["next_s"] == 120
    )  # the override stays per-controller


def test_an_identical_retry_is_answered_200_and_stored_once(client, db):
    first = reported(client, REPORT)
    retry = reported(client, REPORT)

    assert first.status_code == retry.status_code == 200
    assert len(rows(db)) == 3


def test_a_report_after_a_reboot_reuses_old_uptimes_and_still_lands(client, db):
    reported(client, "c=0 t=60000 ch0=1\n")
    run_sql(db, "UPDATE readings SET ts = ts - 3600")  # past the window
    answer = reported(client, "c=0 t=60000 ch0=2\n")

    assert answer.status_code == 200
    assert [r[2] for r in rows(db)] == [1, 2]


def test_a_report_without_t_never_dedups(client, db):
    reported(client, "c=0 ch0=1\n")
    reported(client, "c=0 ch0=1\n")

    assert len(rows(db)) == 2


# --------------------------------------------------------------------------- #
# Refusals: the whole report or nothing, and always the right status
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("nope", id="a_wrong_token"),
        pytest.param(None, id="a_missing_token_header"),
        # h11 lets obs-text header bytes through and the ASGI layer decodes
        # them latin-1, so the handler sees a non-ASCII str; compare_digest
        # on that would 500 rather than 401.
        pytest.param("sécret".encode("latin-1"), id="a_non_ascii_token"),
    ],
)
def test_a_report_the_token_does_not_open_stores_nothing(client, db, token):
    assert reported(client, REPORT, token=token).status_code == 401
    assert rows(db) == []


@pytest.mark.parametrize(
    "body",
    [
        "ch0=8123\n",  # no controller
        "c=0\n",  # no channels: a silent 200 would hide a dead board
        "c=0 ch0=8123 ch1=garbage\n",  # non-integer value
        "c=0 ch0=9223372036854775808\n",  # 2**63: would overflow sqlite
        "c=0 ch999=1\n",  # channel index out of range
        "c=0 ch0=1 ch0=2\n",  # duplicate channel: last-wins hides bugs
        "c=0 c=2 ch0=1\n",  # two controllers in one report
        "c=0 ch0=1 notakv\n",  # not k=v
        "c=0 t=abc ch0=1\n",  # t= not an integer
        "c= c=evil ch0=1\n",  # empty c= must not open the door to a second
        "c=0 ch0=1 =5\n",  # empty key
        "c=0 ch0=١٢٣\n",  # Unicode digits: refusal, not repair
        "c=0 t=1_000 ch0=5\n",  # int() underscores are not board output
        "c=0 ch0=+5\n",  # neither is a leading +
    ],
)
def test_a_malformed_report_is_refused_whole(client, db, body):
    answer = reported(client, body)

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
    body = "c=0 " + " ".join(f"ch{i % 200}=1" for i in range(2000))
    answer = reported(client, body)

    assert answer.status_code == 413
    assert rows(db) == []


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
    # a container whose bind mount was forgotten must refuse rather than
    # quietly keep readings in a layer that dies with the container
    with pytest.raises(ValueError, match="/data"):
        create_app(db_path="/data/butler.db", token=TOKEN)


# --------------------------------------------------------------------------- #
# The parser on its own
# --------------------------------------------------------------------------- #


def test_parse_is_strict_about_shape_and_silent_about_unknowns():
    report = parse_report("c=7 unknown=1 t=99 ch7=99")
    assert report[:3] == (7, {7: 99}, 99)
    with pytest.raises(ValueError):
        parse_report("c=7 notakv")
