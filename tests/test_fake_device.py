"""The fake device's two pure halves; the loop itself is run by hand."""

import pytest

from fake_device import build_report, parse_response


@pytest.mark.parametrize(
    "args, kwargs, line",
    [
        pytest.param(
            ("fake1", 61000, [8123, 7902]),
            {"ack": 17, "flow_ml": 48},
            "c=fake1 t=61000 ch0=8123 ch1=7902 ack=17 flow_ml=48\n",
            id="a_report_looks_like_the_boards",
        ),
        pytest.param(
            ("fake1", 5, [7]),
            {"float_ok": 1, "pos": "ok"},
            "c=fake1 t=5 ch0=7 float=1 pos=ok\n",
            id="it_reports_reservoir_and_position",
        ),
        # ch204 is seconds since the float last moved; ch207 is the contra latch.
        pytest.param(
            ("fake1", 5, [7]),
            {"err": "contra", "contra": True, "float_age": 42},
            "c=fake1 t=5 ch0=7 ch204=42 ch207=1 err=contra\n",
            id="it_reports_the_tanks_own_fields",
        ),
        # ch210 is the flap latch, ch211 is the dry latch.
        pytest.param(
            ("fake1", 5, [7]),
            {"contra": True, "flap": True, "dry": True},
            "c=fake1 t=5 ch0=7 ch207=1 ch210=1 ch211=1\n",
            id="it_reports_the_boards_other_two_latches",
        ),
        pytest.param(
            ("fake1", 5, [7]),
            {"flap": True},
            "c=fake1 t=5 ch0=7 ch210=1\n",
            id="the_flap_latch_alone",
        ),
    ],
)
def test_build_report_writes_the_line_the_board_would(args, kwargs, line):
    assert build_report(*args, **kwargs) == line


@pytest.mark.parametrize(
    "response, expected",
    [
        pytest.param(
            "next=60\ncmd=17 water=3 ml=50 cap_s=30\n",
            (60, {"id": 17, "kind": "water", "outlet": 3, "ml": 50, "cap_s": 30}),
            id="it_understands_a_water_command",
        ),
        pytest.param(
            "next=60\ncmd=17 stop=1\n",
            (60, {"id": 17, "kind": "stop"}),
            id="it_understands_stop",
        ),
        pytest.param("next=60\n", (60, None), id="it_understands_silence"),
    ],
)
def test_parse_response_reads_the_interval_and_the_one_command(response, expected):
    assert parse_response(response) == expected
