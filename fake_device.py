#!/usr/bin/env python3
"""Pretend to be the board.

Reports like the firmware will — `c= t= chN=` once per interval — obeying
the `next=` the backend answers, executing the one command a response may
carry, and acking it on the FOLLOWING report, the same offbeat the real
board uses. On a lost exchange it retries the IDENTICAL report once (same
`t=`, like the firmware) and then discards it, so the backend's dedup and
expiry rules get exercised for real. Standard library only; point it at a
local `uv run uvicorn butler:create_app --factory` or at the NAS:

    python fake_device.py --token dev [--url http://localhost:8000]
        [--controller 9] [--channels 5] [--cycles 0]
"""

import argparse
import http.client
import os
import random
import time
import urllib.error
import urllib.request

FULL_SCALE = 16383  # 14-bit, like the real ADC


def build_report(
    controller, t_ms, values, ack=None, flow_ml=None, float_ok=None, pos=None
):
    """One report body, exactly as the board would write it."""
    tokens = [f"c={controller}", f"t={t_ms}"]
    tokens += [f"ch{i}={v}" for i, v in enumerate(values)]
    if float_ok is not None:
        tokens.append(f"float={float_ok}")
    if pos is not None:
        tokens.append(f"pos={pos}")
    if ack is not None:
        tokens += [f"ack={ack}", f"flow_ml={flow_ml}"]
    return " ".join(tokens) + "\n"


def parse_response(text):
    """The backend's `k=v` lines back: (next_s, command dict or None)."""
    fields = {}
    for token in text.split():
        key, _, value = token.partition("=")
        fields[key] = value
    next_s = int(fields["next"]) if "next" in fields else None
    cmd = None
    if "cmd" in fields:
        cmd = {"id": int(fields["cmd"])}
        if fields.get("stop") == "1":
            cmd["kind"] = "stop"
        else:
            cmd["kind"] = "water"
            cmd["outlet"] = int(fields["water"])
            cmd["ml"] = int(fields["ml"])
            cmd["cap_s"] = int(fields["cap_s"])
    return next_s, cmd


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--token", default=os.environ.get("BUTLER_TOKEN", ""))
    # An integer, like the firmware's PB_CONTROLLER: butler refuses anything
    # else. 9 rather than 0, so a fake board cannot be mistaken for the real
    # one the app fills in by default.
    ap.add_argument("--controller", type=int, default=9)
    ap.add_argument("--channels", type=int, default=5)
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    ap.add_argument(
        "--float",
        type=int,
        choices=[0, 1],
        default=1,
        dest="float_ok",
        help="reservoir float switch (0 simulates an empty tank)",
    )
    ap.add_argument(
        "--pos",
        choices=["ok", "unknown"],
        default="ok",
        help="manifold position status",
    )
    args = ap.parse_args()
    if not args.token:
        ap.error("--token or BUTLER_TOKEN is required")

    start = time.monotonic()
    values = [random.randint(6000, 10000) for _ in range(args.channels)]
    next_s = 60
    pending = None  # (command id, flow_ml) to ack on the NEXT report
    body = None  # an undelivered report survives one retry, verbatim
    attempts = 0
    n = 0
    while True:
        n += 1
        if body is None:
            values = [
                max(0, min(FULL_SCALE, v + random.randint(-150, 150))) for v in values
            ]
            t_ms = int((time.monotonic() - start) * 1000)
            ack, flow_ml = pending if pending else (None, None)
            pending = None
            body = build_report(
                args.controller, t_ms, values, ack, flow_ml, args.float_ok, args.pos
            )
            attempts = 0
        attempts += 1
        try:
            req = urllib.request.Request(
                args.url.rstrip("/") + "/report",
                data=body.encode(),
                headers={"X-Token": args.token},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                answer = resp.read().decode()
        except urllib.error.HTTPError as why:
            # The backend answered: a refusal, not a drop. Retrying the same
            # body cannot get better — show the reason and move on.
            detail = why.read().decode(errors="replace").strip()
            print(f"[{n}] refused ({why.code}): {detail or why.reason}")
            body = None
            if args.cycles and n >= args.cycles:
                return
            time.sleep(next_s)
            continue
        except (OSError, http.client.HTTPException) as why:
            # Like the board on a WiFi drop (URLError, resets mid-body,
            # truncated responses): retry the identical report once — same
            # t=, so the backend's dedup sees what the firmware would send —
            # then discard it.
            if attempts >= 2:
                print(f"[{n}] report failed again ({why}); discarding it")
                body = None
            else:
                print(f"[{n}] report failed ({why}); retrying in {next_s}s")
            if args.cycles and n >= args.cycles:
                return
            time.sleep(next_s)
            continue
        print(f"[{n}] > {body.strip()}")
        print(f"[{n}] < {answer.strip()}")
        body = None
        got_next, cmd = parse_response(answer)
        if got_next:
            next_s = got_next
        if cmd and cmd["kind"] == "water":
            flow = round(cmd["ml"] * random.uniform(0.9, 1.1))
            print(
                f"[{n}]   watering outlet {cmd['outlet']}: {cmd['ml']} ml "
                f"(cap {cmd['cap_s']} s) -> counted {flow} ml; acking next report"
            )
            pending = (cmd["id"], flow)
        elif cmd:
            print(f"[{n}]   stop -> acking next report")
            pending = (cmd["id"], 0)
        if args.cycles and n >= args.cycles:
            return
        time.sleep(next_s)


if __name__ == "__main__":
    main()
