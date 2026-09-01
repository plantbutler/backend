"""The fake device's two pure halves; the loop itself is run by hand."""

from fake_device import build_report, parse_response


def test_a_report_looks_like_the_boards():
    body = build_report("fake1", 61000, [8123, 7902], ack=17, flow_ml=48)
    assert body == "c=fake1 t=61000 ch0=8123 ch1=7902 ack=17 flow_ml=48\n"


def test_it_understands_a_water_command():
    next_s, cmd = parse_response("next=60\ncmd=17 water=3 ml=50 cap_s=30\n")
    assert next_s == 60
    assert cmd == {"id": 17, "kind": "water", "outlet": 3, "ml": 50, "cap_s": 30}


def test_it_understands_stop_and_silence():
    assert parse_response("next=60\ncmd=17 stop=1\n") == (
        60,
        {"id": 17, "kind": "stop"},
    )
    assert parse_response("next=60\n") == (60, None)
