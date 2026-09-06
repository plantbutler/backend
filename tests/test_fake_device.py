"""The fake device's two pure halves; the loop itself is run by hand."""

from fake_device import build_report, parse_response


def test_a_report_looks_like_the_boards():
    body = build_report("fake1", 61000, [8123, 7902], ack=17, flow_ml=48)
    assert body == "c=fake1 t=61000 ch0=8123 ch1=7902 ack=17 flow_ml=48\n"


def test_it_reports_reservoir_and_position():
    body = build_report("fake1", 5, [7], float_ok=1, pos="ok")
    assert body == "c=fake1 t=5 ch0=7 float=1 pos=ok\n"


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


def test_it_reports_the_tank_s_own_fields():
    # What --err, --contra and --float-age put on every report: the last
    # safety error, the board's contradiction latch on ch207, and the seconds
    # since the float last moved on ch204.
    body = build_report("fake1", 5, [7], err="contra", contra=True, float_age=42)
    assert body == "c=fake1 t=5 ch0=7 ch204=42 ch207=1 err=contra\n"
