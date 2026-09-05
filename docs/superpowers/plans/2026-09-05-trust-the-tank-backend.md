# Trust the tank — backend implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The backend holds the durable half of the board's contradiction latch, records refills and raises on a float that never moved across one, retires controllers, stores `err=`, and takes the three small rules corrections that came with the bench sketch.

**Architecture:** Everything is in `butler.py` (one file, `create_app` factory) and `schema.sql` (additive DDL plus `ADDED_COLUMNS`), tested through FastAPI's `TestClient` against a temp SQLite file. New endpoints follow the `/interval` shape exactly: token, `slurp`, a module-level parser, a closure that writes under `BEGIN IMMEDIATE`, a `k=v` answer. New alert rules go in `evaluate()` beside the float/pos loop and use its `raised`/`floor_ok`/`mark`/`clear` helpers.

**Tech Stack:** Python 3.12, FastAPI, stdlib sqlite3, pytest (`uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-09-05-trust-the-tank-design.md` — read it first; every task below cites its decision (D1–D13).

## Global Constraints

- Board 0 is a real board and is falsy: every controller check is `is None`, never truthiness.
- Every write runs under `con.execute("BEGIN IMMEDIATE")` inside `with connect() as con:`; reads in the ticker are bare SELECTs.
- Every new column is in the `CREATE` in `schema.sql` **and** in `ADDED_COLUMNS` (AGENTS.md: a column appended to a CREATE that already ran never reaches an existing database).
- The `status` upsert must leave `latched_ts`, `latch_reason`, `err`, `err_ts` alone unless this report sets them.
- Constants, exact: `CONTRA_CHANNEL = 207`, `FLOAT_AGE_CHANNEL = 204`, `MAX_DOSE_ML = 250`, `VERSION = "0.18.0"`, `ERR_TOKEN = re.compile(r"\A[a-z_]{1,16}\Z")`. `PERSIST_S` (180) is reused as the float's grace after a refill.
- Latch triggers, exact: `ch207 == 1` or `err=contra` → reason `contra`; `err=resetmid` → reason `resetmid`; nothing else (D2: the float going empty does not latch).
- Wire answers, exact: `POST /controller` → `controller=<c> retired=<0|1>`; `POST /refill` → `refill=<ts>`; `POST /resume` → `resumed=<c>`; a latched `POST /command water=` → 409 `refused: board <c> stopped watering (<reason> since <HH:MM>): check the tank, type clear contra on the board, then resume`.
- Alert keys, exact: `latch:<c>` (high, no `floor_ok` — a re-latch after a resume must page even inside the hour), `stale:<c>` (high, with `floor_ok`).
- Unknown keys in any `k=v` body are ignored, as everywhere else in this file.
- Tests are real behaviour through `TestClient`, no mocks of butler internals; the ticker is driven with `app.state.tick(now)`.
- New tests live in `tests/test_tank.py`; existing tests that encode a changed number are updated, never deleted.
- Commit messages end with `🤖 Written by an agent on behalf of @jcanton` as the last line, and carry no other trailer.

---

### Task 1: Foundation — schema, `err=`, `pos_ok_seen`, `/health`

**Files:**
- Modify: `schema.sql` (controllers, status, new `refills`)
- Modify: `butler.py` (`ADDED_COLUMNS`, `Report`, `parse_report`, the status upsert in `handle_report`, `/health`)
- Modify: `tests/test_commands.py:205-216` (the `/health` entry shape)
- Create: `tests/test_tank.py`

**Interfaces:**
- Produces: `Report.err: str | None` (last field); `status.err`, `status.err_ts`, `status.pos_ok_seen`, `status.latched_ts`, `status.latch_reason`; `controllers.retired`; table `refills(ts, controller)`; `/health` controller entries carrying `err`, `err_ts`, `pos_ok_seen`, `retired`, `latched`, `last_refill`.

- [ ] **Step 1: Create `tests/test_tank.py` with its fixtures and the first tests**

```python
"""Trust the tank: err=, the durable latch, refills, the stuck-float rule,
retirement, and the rules corrections that came with them."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import butler
from butler import PERSIST_S, create_app, parse_report

TOKEN = "test-token"
DRY = 11000  # pct 12 with make_pot's calibration
WET = 8000  # pct 50


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


@pytest.fixture
def sent():
    return []


@pytest.fixture
def app(db, sent):
    return create_app(
        db_path=str(db),
        token=TOKEN,
        next_s=60,
        cmd_ttl_s=900,
        quiet="0-0",
        send=lambda alert: sent.append(alert) or True,
        ping=lambda: True,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def post(client, path, body):
    return client.post(path, content=body, headers={"X-Token": TOKEN})


def report(client, body):
    answer = post(client, "/report", body)
    assert answer.status_code == 200, answer.text
    return answer


def health(client, controller=0):
    entries = client.get("/health").json()["controllers"]
    return next(c for c in entries if c["controller"] == controller)


def tick(app, now=None):
    return app.state.tick(now)


def keys(sent):
    return [a.key for a in sent if a.message is not None]


def run_sql(db, sql, *params):
    with sqlite3.connect(db) as con:
        return con.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# err= and pos_ok_seen (spec D8, D11's column)
# --------------------------------------------------------------------------- #


def test_err_is_parsed_once_and_as_a_short_token():
    assert parse_report("c=0 ch0=1 err=contra").err == "contra"
    assert parse_report("c=0 ch0=1").err is None
    with pytest.raises(ValueError, match="err= given twice"):
        parse_report("c=0 ch0=1 err=range err=heap")
    with pytest.raises(ValueError, match="err="):
        parse_report("c=0 ch0=1 err=Contra")
    with pytest.raises(ValueError, match="err="):
        parse_report("c=0 ch0=1 err=" + "x" * 17)


def test_the_last_err_is_kept_until_the_board_sends_another(client):
    report(client, "c=0 ch0=1 err=heap")
    first = health(client)
    assert first["err"] == "heap" and first["err_ts"] > 0
    report(client, "c=0 ch0=1")
    assert health(client)["err"] == "heap"
    report(client, "c=0 ch0=1 err=range")
    assert health(client)["err"] == "range"


def test_pos_ok_seen_remembers_that_the_board_once_knew_its_position(client):
    report(client, "c=0 ch0=1 pos=unknown")
    assert health(client)["pos_ok_seen"] is None
    report(client, "c=0 ch0=1 pos=ok")
    assert health(client)["pos_ok_seen"] > 0
    report(client, "c=0 ch0=1 pos=unknown")
    assert health(client)["pos_ok_seen"] > 0


def test_health_carries_the_new_fields_with_their_defaults(client):
    report(client, "c=0 ch0=1")
    entry = health(client)
    for key in ("err", "err_ts", "pos_ok_seen", "latched", "last_refill"):
        assert entry[key] is None, key
    assert entry["retired"] == 0


def test_an_old_database_grows_the_columns_at_startup(db):
    # The shape 0.17.0 left behind for the two tables that change.
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE controllers (
              controller INTEGER PRIMARY KEY, last_seen INTEGER NOT NULL, next_s INTEGER);
            CREATE TABLE status (
              controller INTEGER PRIMARY KEY, ts INTEGER NOT NULL, float_ok INTEGER,
              float_since INTEGER, pos TEXT, pos_since INTEGER, float_seen INTEGER,
              pos_seen INTEGER, float_bad INTEGER, float_bad_prev INTEGER,
              pos_bad INTEGER, pos_bad_prev INTEGER);
            INSERT INTO controllers VALUES (0, 5, NULL);
            INSERT INTO status VALUES (0, 5, 1, 5, 'ok', 5, 5, 5, NULL, NULL, NULL, NULL);
            """
        )
    client = TestClient(
        create_app(db_path=str(db), token=TOKEN, next_s=60, cmd_ttl_s=900)
    )
    entry = health(client)
    assert entry["retired"] == 0 and entry["latched"] is None and entry["err"] is None
    report(client, "c=0 ch0=1 err=noflow")
    assert health(client)["err"] == "noflow"
```

- [ ] **Step 2: Run them, expect failures** — `uv run pytest tests/test_tank.py -q` → `AttributeError: 'Report' object has no attribute 'err'` and `KeyError: 'err'`.

- [ ] **Step 3: `schema.sql`** — in `controllers` add `retired INTEGER NOT NULL DEFAULT 0  -- 1: a board that is gone; reports still land, nothing pages or waters` after `next_s`. In `status` add, after `pos_bad_prev`:

```sql
  err            TEXT,              -- err= in the latest report that carried one:
  err_ts         INTEGER,           -- the board's last safety error, and when
  latched_ts     INTEGER,           -- the durable half of the board's contradiction
  latch_reason   TEXT,              -- latch: 'contra' | 'resetmid', NULL when not
  pos_ok_seen    INTEGER            -- last pos=ok ever seen; pos: pages only after one
```

(Mind the comma on the line before.) Then, after the `status` table, the new table:

```sql
-- A refill is a human event (pitch "Trust the tank"): the app says so, the
-- board cannot. The stuck-float rule reads the latest one per controller
-- against ch204, every tick, hence the index.
CREATE TABLE IF NOT EXISTS refills (
  ts         INTEGER NOT NULL,  -- server time when the human said so
  controller INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS refills_by_controller ON refills (controller, ts);
```

- [ ] **Step 4: `ADDED_COLUMNS`** — append, before the closing paren:

```python
    # Trust the tank (0.18.0): a board's own last error, its durable latch,
    # whether it ever knew its position, and whether it is retired.
    ("controllers", "retired", "INTEGER NOT NULL DEFAULT 0", None, None),
    ("status", "err", "TEXT", None, None),
    ("status", "err_ts", "INTEGER", None, None),
    ("status", "latched_ts", "INTEGER", None, None),
    ("status", "latch_reason", "TEXT", None, None),
    ("status", "pos_ok_seen", "INTEGER", None, None),
```

- [ ] **Step 5: `Report` and `parse_report`** — add `err: str | None  # the board's last safety error token, when it sent one` as the last field of `Report`. Beside `SAFE_ID` add:

```python
# err= is the board's last safety error: one of its own short lowercase
# tokens (contra, resetmid, range, heap, ...). Bounded like every other
# field, so a stray value cannot become an unbounded TEXT on status.
ERR_TOKEN = re.compile(r"\A[a-z_]{1,16}\Z")
```

In `parse_report`: initialise `err = None` alongside the others; add the branch

```python
        elif key == "err":
            if err is not None:
                raise ValueError("err= given twice")
            if not ERR_TOKEN.match(value):
                raise ValueError(f"err= must be a short lowercase token, got {value!r}")
            err = value
```

and return `Report(controller, channels, t, ack, flow_ml, float_ok, pos, err)`.

- [ ] **Step 6: the status upsert in `handle_report`** — extend the INSERT's column list with `err, err_ts, pos_ok_seen`, its VALUES with three more `?`, and the `ON CONFLICT ... DO UPDATE SET` with:

```sql
err = COALESCE(excluded.err, status.err),
err_ts = CASE WHEN excluded.err IS NOT NULL THEN excluded.ts ELSE status.err_ts END,
pos_ok_seen = CASE WHEN excluded.pos = 'ok' THEN excluded.ts ELSE status.pos_ok_seen END
```

with the three new parameters `r.err`, `now if r.err is not None else None`, `now if r.pos == "ok" else None`. `latched_ts`/`latch_reason` are NOT in the upsert: a fresh row gets NULL from the table, and Task 6 writes them in their own statement.

- [ ] **Step 7: `/health`** — `entry()` gains `"err": None, "err_ts": None, "pos_ok_seen": None, "retired": 0, "latched": None, "last_refill": None`. The controllers loop selects `retired` too and sets `e["retired"] = retired`. The status loop becomes:

```python
                for (
                    controller, float_ok, pos, err, err_ts, pos_ok_seen, latched_ts, reason,
                ) in con.execute(
                    "SELECT controller, float_ok, pos, err, err_ts, pos_ok_seen, "
                    "latched_ts, latch_reason FROM status"
                ):
                    e = known.setdefault(controller, entry(controller))
                    e["float"] = float_ok
                    e["pos"] = pos
                    e["err"] = err
                    e["err_ts"] = err_ts
                    e["pos_ok_seen"] = pos_ok_seen
                    e["latched"] = (
                        {"since": latched_ts, "reason": reason}
                        if latched_ts is not None
                        else None
                    )
                for controller, ts in con.execute(
                    "SELECT controller, MAX(ts) FROM refills GROUP BY controller"
                ):
                    known.setdefault(controller, entry(controller))["last_refill"] = ts
```

Update `tests/test_commands.py::test_health_lists_a_configured_but_never_seen_controller`'s expected dict with the six new keys at their defaults.

- [ ] **Step 8: Run the whole suite** — `uv run pytest -q`: all green, output pristine.

- [ ] **Step 9: Commit** — `git add schema.sql butler.py tests/test_tank.py tests/test_commands.py` and commit as "The board's err= is stored, and status learns whether it ever knew its position".

---

### Task 2: One dose ceiling — `MAX_DOSE_ML` is the board's 250

**Files:**
- Modify: `butler.py:123` (the constant), `tests/test_commands.py:224-232, 321-325`, `tests/test_tank.py`

- [ ] **Step 1: Tests** — append to `tests/test_tank.py`:

```python
# --------------------------------------------------------------------------- #
# The dose ceiling is the board's (spec D10)
# --------------------------------------------------------------------------- #


def test_a_dose_above_the_rig_ceiling_is_refused_before_it_is_queued(client):
    assert butler.MAX_DOSE_ML == 250
    assert post(client, "/command", "c=0 water=3 ml=250").status_code == 200
    answer = post(client, "/command", "c=1 water=3 ml=251")
    assert answer.status_code == 400 and "ml=" in answer.text


def test_a_pot_cannot_be_saved_with_a_dose_the_board_would_refuse(client):
    answer = post(client, "/pot", "name=basil dose_ml=251")
    assert answer.status_code == 400 and "dose_ml" in answer.text
    assert post(client, "/pot", "name=basil dose_ml=250").status_code == 200
```

- [ ] **Step 2: Run, expect the first to fail** on `MAX_DOSE_ML == 250`.

- [ ] **Step 3: The constant** —

```python
# The board's own PB_DOSE_RIG_MAX_ML, and the two move together: a pot
# above it is refused by the firmware with err=range, acked with flow_ml=0,
# charged nothing, cooled down and paged high once per cooldown, forever,
# and never watered. Refusing here, at /command and at pot save, is what
# keeps that loop unreachable. (DECISIONS #7: a full dump is a mop-up; a
# quarter of the bench reservoir per dose is the number that makes it one.)
MAX_DOSE_ML = 250
```

- [ ] **Step 4: Adapt the two existing tests whose numbers change** — in `test_a_dose_without_a_cap_gets_the_rules_own_cap` use `ml=250` and expect `cap_s=17` (`250 // 20 + 5`); in `test_cap_for_stays_under_the_firmware_cap_after_a_retune` expect `cap_for(butler.MAX_DOSE_ML) == 17` and retune `FLOW_FLOOR_ML_S` to `1` (not 10) so the clamp to `MAX_CAP_S` is still what the second assertion exercises. The claims stay; only the numbers move.

- [ ] **Step 5: Full suite green; commit** — "One dose ceiling, and it is the board's 250".

---

### Task 3: The daily cap charges acked water only

**Files:**
- Modify: `butler.py` (`water_rules`, the two `SUM(...)` queries), `tests/test_tank.py`

- [ ] **Step 1: Test** — append (this needs a pot helper; add it above the test):

```python
# --------------------------------------------------------------------------- #
# The daily cap counts water the board acknowledged (spec D9)
# --------------------------------------------------------------------------- #


def make_pot(client, **over):
    fields = {
        "name": "basil",
        "controller": 0,
        "channel": 0,
        "outlet": 3,
        "dry_raw": 12000,
        "wet_raw": 4000,
        "target_low_pct": 30,
        "target_high_pct": 60,
        "dose_ml": 100,
        "mode": "auto",
    } | over
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    answer = post(client, "/pot", body)
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def dry_reports(client, n=5, extra=""):
    """n dry reports with the safety fields the rules need; no t=, so none
    is a retry of another. Returns the last response text."""
    text = ""
    for _ in range(n):
        text = report(client, f"c=0 ch0={DRY} float=1 pos=ok {extra}".strip()).text
    return text


def commands(db):
    return run_sql(db, "SELECT id, state, flow_ml FROM commands ORDER BY id")


def test_a_dose_the_board_never_acknowledged_is_not_charged_to_the_day(client, db):
    make_pot(client, cooldown_h=0, daily_cap_ml=150)
    assert "cmd=1 water=3 ml=100" in dry_reports(client)  # handed on the fifth
    report(client, f"c=0 ch0={DRY} float=1 pos=ok")  # no ack: expired, never acked
    assert commands(db) == [(1, "expired", None)]
    # Before this change the phantom 100 ml counted, 100 + 100 > 150, and the
    # pot went thirsty for the day on a response that never arrived.
    assert "cmd=2 water=3 ml=100" in dry_reports(client)
    report(client, f"c=0 ch0={DRY} float=1 pos=ok ack=2 flow_ml=100")
    dry_reports(client)
    assert [row[0] for row in commands(db)] == [1, 2]  # 100 acked + 100 > 150: the cap holds
```

- [ ] **Step 2: Run, expect failure** at the `cmd=2` assertion.

- [ ] **Step 3: The two sums** — in `water_rules`, both `SELECT COALESCE(SUM(COALESCE(flow_ml, ml)), 0) FROM commands ...` become

```sql
SELECT COALESCE(SUM(CASE WHEN acked_ts IS NOT NULL THEN COALESCE(flow_ml, ml) ELSE 0 END), 0) FROM commands ...
```

with the comment above the pair updated: "Acked water only. A handed command the board never acked is far likelier a response that never arrived than an ack that was lost — the firmware never retries once any response bytes came back — and charging its full dose starved the pot for the day on nothing. The cooldown above still counts it: spacing errs dry, the cap counts water. (Jacopo, 2026-09-05.)"

- [ ] **Step 4: Full suite green** — `test_rules.py::test_the_daily_cap_counts_what_actually_flowed` must still pass (acked doses are unchanged). **Commit** — "The daily cap counts the water the board acknowledged".

---

### Task 4: No `pos:` page before a first `pos=ok`

**Files:**
- Modify: `butler.py` (the float/pos loop in `evaluate`), `tests/test_tank.py`

- [ ] **Step 1: Test** — append:

```python
# --------------------------------------------------------------------------- #
# pos: waits for a board that has ever known its position (spec D11)
# --------------------------------------------------------------------------- #


def test_no_pos_page_before_a_board_has_ever_said_pos_ok(app, client, sent):
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    tick(app)
    assert "pos:0" not in keys(sent)  # PB_REPORT_POS_UNKNOWN=1 ships this way
    report(client, "c=0 ch0=1 float=1 pos=ok")
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    report(client, "c=0 ch0=1 float=1 pos=unknown")
    tick(app)
    assert "pos:0" in keys(sent)
```

- [ ] **Step 2: Run, expect failure** at the first `not in`.

- [ ] **Step 3: The gate** — the loop's SELECT adds `pos_ok_seen` (and the tuple unpack a `pos_ok_seen`). In the `for kind, value, ... in (...)` tuple add a ninth element `armed`: `True` for `float`, `pos_ok_seen is not None` for `pos`; the raise branch becomes `if flapped and armed:` (the clear branch is unchanged: a stale raise from before can still clear). Comment: "A board that has never said pos=ok is one shipped with the flag that forces pos=unknown; paging on it would raise once, two minutes after first boot, and then stand deaf for the whole bench programme."

- [ ] **Step 4: Full suite green; commit** — "pos: waits for a board that has ever known its position".

---

### Task 5: Retirement

**Files:**
- Modify: `butler.py` (new `parse_controller`, `set_retired`, `POST /controller`, the silence loop, `water_rules`), `tests/test_tank.py`

**Interfaces:**
- Produces: `parse_controller(text) -> tuple[int, int]`; `POST /controller` `c=<n> retired=0|1` → `controller=<n> retired=<0|1>`.

- [ ] **Step 1: Tests** — append:

```python
# --------------------------------------------------------------------------- #
# Retirement (spec D7)
# --------------------------------------------------------------------------- #


def age_controller(db, seconds):
    with sqlite3.connect(db) as con:
        con.execute("UPDATE controllers SET last_seen = last_seen - ?", (seconds,))


def test_parse_controller_wants_both_fields_once():
    assert butler.parse_controller("c=0 retired=1") == (0, 1)
    assert butler.parse_controller("retired=0 c=9 later=1") == (9, 0)
    for body in ("retired=1", "c=0", "c=0 retired=2", "c=0 c=0 retired=1"):
        with pytest.raises(ValueError):
            butler.parse_controller(body)


def test_a_retired_board_is_quiet_and_never_waters(app, client, db, sent):
    make_pot(client, cooldown_h=0)
    report(client, "c=0 ch0=1")
    answer = post(client, "/controller", "c=0 retired=1")
    assert answer.status_code == 200 and answer.text == "controller=0 retired=1\n"
    assert health(client)["retired"] == 1
    age_controller(db, 7200)
    app.state.observed["since"] = 0
    tick(app)
    assert "silent:0" not in keys(sent)
    dry_reports(client)
    assert commands(db) == []  # readings landed, nothing queued
    assert post(client, "/controller", "c=0 retired=0").text == "controller=0 retired=0\n"
    age_controller(db, 7200)
    tick(app)
    assert "silent:0" in keys(sent)


def test_retiring_a_paged_board_clears_its_silence_page(app, client, db, sent):
    report(client, "c=0 ch0=1")
    age_controller(db, 7200)
    app.state.observed["since"] = 0
    tick(app)
    assert keys(sent) == ["silent:0"]
    post(client, "/controller", "c=0 retired=1")
    assert run_sql(db, "SELECT cleared_ts IS NOT NULL FROM alerts WHERE key = 'silent:0'") == [(1,)]
    assert client.get("/health").json()["alerts"] == []
```

- [ ] **Step 2: Run, expect** `AttributeError: module 'butler' has no attribute 'parse_controller'`.

- [ ] **Step 3: The parser** — beside `parse_interval`:

```python
def parse_controller(text: str) -> tuple[int, int]:
    """`c=<controller> retired=0|1`: the POST /controller body. Both fields,
    each once; unknown keys ignored like everywhere else on this wire."""
    controller = retired = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, MAX_CONTROLLER + 1)
        elif key == "retired":
            if retired is not None:
                raise ValueError("retired= given twice")
            retired = _int_in(value, "retired", 0, 2)
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the body")
    if retired is None:
        raise ValueError("no retired= in the body")
    return controller, retired
```

- [ ] **Step 4: The write and the route** — beside `set_interval`:

```python
    def set_retired(controller: int, flag: int) -> None:
        """A retired board is a quiet one, not a rejected one: its reports
        still land, but nothing pages for it and nothing waters from it.
        Retiring also clears a standing silence page — that rule skips the
        board from now on, so nobody else would ever clear it."""
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO controllers (controller, last_seen, retired) "
                "VALUES (?, 0, ?) "
                "ON CONFLICT(controller) DO UPDATE SET retired = excluded.retired",
                (controller, flag),
            )
            if flag:
                con.execute(
                    "UPDATE alerts SET cleared_ts = ? "
                    "WHERE key = ? AND cleared_ts IS NULL",
                    (now, f"silent:{controller}"),
                )
```

and after `/interval`:

```python
    @app.post("/controller")
    async def controller_knob(request: Request):
        if bad_token(request):
            return PlainTextResponse("bad token\n", status_code=401)
        body = await slurp(request)
        if isinstance(body, PlainTextResponse):
            return body
        try:
            controller, retired = parse_controller(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as why:
            return PlainTextResponse(f"refused: {why}\n", status_code=400)
        try:
            await run_in_threadpool(set_retired, controller, retired)
        except sqlite3.OperationalError as why:
            return PlainTextResponse(f"try again: {why}\n", status_code=503)
        return PlainTextResponse(f"controller={controller} retired={retired}\n")
```

- [ ] **Step 5: The two readers** — the silence loop's query becomes `SELECT controller, last_seen, next_s FROM controllers WHERE last_seen > 0 AND retired = 0` (a retired board then has no `heartbeat` entry, so the sensor rule skips its pots on its own — say so in a comment). In `water_rules`, right after the proposal-TTL sweep:

```python
        retired = con.execute(
            "SELECT retired FROM controllers WHERE controller = ?", (r.controller,)
        ).fetchone()
        if retired and retired[0]:
            return  # a retired board keeps its readings and never waters
```

- [ ] **Step 6: Full suite green; commit** — "A controller can be retired: quiet, not rejected".

---

### Task 6: The durable latch and `POST /resume`

**Files:**
- Modify: `butler.py` (constants, `Latched`, `latch_reason`, `latch_of`, `handle_report`, `water_rules`, `enqueue` + `/command`, `evaluate`, `parse_board`, `resume`, `POST /resume`), `tests/test_tank.py`

**Interfaces:**
- Produces: `CONTRA_CHANNEL = 207`; `class Latched(Exception)`; `latch_reason(r: Report) -> str | None`; `latch_of(con, controller) -> tuple[int, str] | None`; `parse_board(text) -> int` (a `c=`-only body, shared with Task 7's `/refill`); `POST /resume`.

- [ ] **Step 1: Tests** — append:

```python
# --------------------------------------------------------------------------- #
# The durable latch (spec D2-D4)
# --------------------------------------------------------------------------- #


def test_the_board_latches_the_backend_through_ch207_or_err(client):
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    latched = health(client)["latched"]
    assert latched["reason"] == "contra" and latched["since"] > 0
    report(client, "c=1 ch0=1 float=1 pos=ok err=contra")
    assert health(client, 1)["latched"]["reason"] == "contra"
    report(client, "c=2 ch0=1 float=1 pos=ok err=resetmid")
    assert health(client, 2)["latched"]["reason"] == "resetmid"
    # An empty tank is not a latch (D2's deviation): the rules refuse on it.
    report(client, "c=3 ch0=1 float=1 pos=ok")
    report(client, "c=3 ch0=1 float=0 pos=ok")
    assert health(client, 3)["latched"] is None


def test_the_latch_outlives_the_board_forgetting_it(client, db):
    make_pot(client, cooldown_h=0)
    report(client, f"c=0 ch0={DRY} float=1 pos=ok ch207=1")
    since = health(client)["latched"]["since"]
    # A power cycle: the board comes back clean and keeps saying so.
    dry_reports(client, extra="ch207=0")
    assert health(client)["latched"] == {"since": since, "reason": "contra"}
    assert commands(db) == []  # the rules stayed dry for the whole window


def test_a_latch_expires_what_was_waiting_and_refuses_new_water(client, db):
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1 ack=99")  # the queued one is NOT handed
    assert commands(db) == [(1, "expired", None)]
    answer = post(client, "/command", "c=0 water=3 ml=50")
    assert answer.status_code == 409
    assert answer.text.startswith("refused: board 0 stopped watering (contra since ")
    assert answer.text.rstrip().endswith("check the tank, type clear contra on the board, then resume")
    assert post(client, "/command", "c=0 stop=1").status_code == 200


def test_the_latch_pages_once_and_resume_clears_row_and_page(app, client, db, sent):
    report(client, "c=0 ch0=1 float=1 pos=ok err=contra")
    tick(app)
    tick(app)
    assert keys(sent) == ["latch:0"]
    (alert,) = [a for a in sent if a.key == "latch:0"]
    assert alert.priority == "high" and "float said full and the meter saw nothing" in alert.message
    assert {"key": "latch:0"} <= {k: v for k, v in client.get("/health").json()["alerts"][0].items() if k == "key"}
    answer = post(client, "/resume", "c=0")
    assert answer.status_code == 200 and answer.text == "resumed=0\n"
    assert health(client)["latched"] is None
    assert client.get("/health").json()["alerts"] == []
    assert post(client, "/command", "c=0 water=3 ml=50").status_code == 200
    # A re-latch inside the hour pages again: no re-alert floor on this one.
    post(client, "/command", "c=0 stop=1") if False else None
    report(client, "c=0 ch0=1 float=1 pos=ok ch207=1")
    tick(app)
    assert keys(sent) == ["latch:0", "latch:0"]
    assert post(client, "/resume", "c=0").text == "resumed=0\n"  # idempotent on a clean board
    assert post(client, "/resume", "c=0").text == "resumed=0\n"
```

Note for the implementer: the line `post(client, "/command", "c=0 stop=1") if False else None` is a placeholder you must delete — the test needs no stop; keep everything else.

- [ ] **Step 2: Run, expect** the first test to fail on `latched` being `None`.

- [ ] **Step 3: Constants and helpers** (module level, near `MAX_CONTROLLER`):

```python
# The board's diagnostic channels this file reads. ch207 is its contradiction
# latch (float said full, meter saw nothing), 0 or 1 in every report while it
# stands; it lives in .noinit on the board and a power cycle erases it, which
# is why the durable half is here.
CONTRA_CHANNEL = 207
LATCH_TEXT = {
    "contra": "the float said full and the meter saw nothing",
    "resetmid": "it reset with the pump running",
}


class Latched(Exception):
    """POST /command's refusal while a board's durable latch stands."""

    def __init__(self, controller: int, since: int, reason: str):
        super().__init__(
            f"board {controller} stopped watering ({reason} since {hhmm(since)}): "
            "check the tank, type clear contra on the board, then resume"
        )


def latch_reason(r: Report) -> str | None:
    """What in this report latches the backend, if anything. The float going
    empty is deliberately not here: the rules already refuse on float=0, and
    a refill is the human event for that (spec D2)."""
    if r.channels.get(CONTRA_CHANNEL) == 1 or r.err == "contra":
        return "contra"
    if r.err == "resetmid":
        return "resetmid"
    return None


def latch_of(con: sqlite3.Connection, controller: int) -> tuple[int, str] | None:
    row = con.execute(
        "SELECT latched_ts, latch_reason FROM status WHERE controller = ?",
        (controller,),
    ).fetchone()
    return (row[0], row[1]) if row and row[0] is not None else None


def parse_board(text: str) -> int:
    """A body that names a board and nothing else: `c=<controller>`."""
    controller = None
    for token in text.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"not a k=v token: {token!r}")
        if key == "c":
            if controller is not None:
                raise ValueError("c= given twice")
            controller = _int_in(value, "c", 0, MAX_CONTROLLER + 1)
    if controller is None:  # board 0 is a real board
        raise ValueError("no c= in the body")
    return controller
```

(`hhmm` is defined above `Latched`'s first use only if `Latched` sits below it — place `Latched` after `hhmm`.)

- [ ] **Step 4: Setting it** — in `handle_report`, right after the status upsert and before the ack step:

```python
            reason = latch_reason(r)
            if reason is not None:
                # Set once, never refreshed while it stands: `since` is when
                # the trouble began. A dose still waiting would pour into a
                # tank nobody has looked at, so it goes the way burial sends
                # one; a 'sent' one is with the board, whose own latch holds.
                con.execute(
                    "UPDATE status SET latched_ts = COALESCE(latched_ts, ?), "
                    "latch_reason = COALESCE(latch_reason, ?) WHERE controller = ?",
                    (now, reason, r.controller),
                )
                con.execute(
                    "UPDATE commands SET state = 'expired' WHERE controller = ? "
                    "AND kind = 'water' AND state IN ('proposed', 'queued')",
                    (r.controller,),
                )
```

- [ ] **Step 5: The two readers** — in `water_rules`, after the retired check: `if latch_of(con, r.controller) is not None: return  # the durable half of the board's latch: dry until a human resumes`. In `enqueue`, before the `busy` query:

```python
            if c.kind == "water":
                standing = latch_of(con, c.controller)
                if standing is not None:
                    raise Latched(c.controller, *standing)
```

and in the `/command` route, before the `OperationalError` clause: `except Latched as why: return PlainTextResponse(f"refused: {why}\n", status_code=409)`.

- [ ] **Step 6: The page** — in `evaluate`, after the float/pos loop:

```python
        # The durable latch. High, and without the re-alert floor: a board
        # that latches again ten minutes after a human resumed it is exactly
        # the repeat that must not wait an hour to be heard.
        for controller, latched_ts, reason in con.execute(
            "SELECT controller, latched_ts, latch_reason FROM status "
            "WHERE latched_ts IS NOT NULL"
        ):
            key = f"latch:{controller}"
            if not raised(key):
                found.append(
                    Alert(
                        key,
                        "high",
                        "warning",
                        f"board {controller} stopped watering: "
                        f"{LATCH_TEXT.get(reason, reason)} — check the tank, type "
                        "clear contra on the board, then resume in the app",
                        mark(key),
                    )
                )
```

- [ ] **Step 7: Resume** — beside `set_retired`:

```python
    def resume(controller: int) -> None:
        """The human's half: the tank was checked. Clears the row and the
        page together, so /health and the phone agree the moment it answers;
        idempotent on a board that was not latched."""
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE status SET latched_ts = NULL, latch_reason = NULL "
                "WHERE controller = ?",
                (controller,),
            )
            con.execute(
                "UPDATE alerts SET cleared_ts = ? WHERE key = ? AND cleared_ts IS NULL",
                (now, f"latch:{controller}"),
            )
```

and the route, after `/controller`, in the `/interval` shape with `parse_board`, `run_in_threadpool(resume, controller)`, answering `resumed={controller}\n`.

- [ ] **Step 8: Full suite green; commit** — "The backend holds the durable half of the board's latch, until a human resumes".

---

### Task 7: Refills and the stuck-float rule

**Files:**
- Modify: `butler.py` (`FLOAT_AGE_CHANNEL`, `float_frozen`, `record_refill`, `POST /refill`, `water_rules`, `evaluate`), `tests/test_tank.py`

**Interfaces:**
- Consumes: `parse_board` (Task 6).
- Produces: `FLOAT_AGE_CHANNEL = 204`; `float_frozen(con, controller, now) -> int | None`; `POST /refill` `c=<n>` → `refill=<ts>`; alert `stale:<c>`.

- [ ] **Step 1: Tests** — append:

```python
# --------------------------------------------------------------------------- #
# Refills and the stuck float (spec D5, D6)
# --------------------------------------------------------------------------- #


def plant_float_history(db, *, refill_ago, read_ago, age, controller=0):
    """A refill and one ch204 reading, timestamps controlled: the float last
    moved `age` seconds before the reading."""
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO refills (ts, controller) VALUES (?, ?)",
            (now - refill_ago, controller),
        )
        con.execute(
            "INSERT INTO readings (ts, controller, channel, raw) VALUES (?, ?, ?, ?)",
            (now - read_ago, controller, butler.FLOAT_AGE_CHANNEL, age),
        )


def test_a_refill_is_recorded_and_shown(client, db):
    before = int(time.time())
    answer = post(client, "/refill", "c=0")
    assert answer.status_code == 200
    ts = int(answer.text.removeprefix("refill=").strip())
    assert ts >= before
    assert run_sql(db, "SELECT controller FROM refills") == [(0,)]
    assert health(client)["last_refill"] == ts
    assert post(client, "/refill", "retired=1").status_code == 400


def test_a_float_that_never_moved_across_a_refill_pages_and_holds_the_water(
    app, client, db, sent
):
    make_pot(client, cooldown_h=0)
    # Refilled ten minutes ago; the float last moved an hour before that.
    plant_float_history(db, refill_ago=600, read_ago=60, age=4200)
    tick(app)
    assert keys(sent) == ["stale:0"]
    (alert,) = sent
    assert alert.priority == "high" and "has not moved since before the refill" in alert.message
    # Each report carries ch204 itself: still frozen, so the rules stay dry.
    dry_reports(client, extra="ch204=5000")
    assert commands(db) == []
    # Then the float moves: cleared, and the next window waters.
    dry_reports(client, extra="ch204=5")
    tick(app)
    assert keys(sent) == ["stale:0", "stale:0"]
    assert sent[-1].priority == "default"
    assert client.get("/health").json()["alerts"] == []
    assert len(commands(db)) == 1


def test_the_float_gets_its_grace_after_a_refill_and_needs_a_refill_at_all(app, client, db, sent):
    plant_float_history(db, refill_ago=100, read_ago=10, age=4000)  # 90 s < PERSIST_S
    tick(app)
    assert keys(sent) == []
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM refills")
        con.execute("INSERT INTO readings (ts, controller, channel, raw) VALUES (?, 0, 204, 99999)", (int(time.time()),))
    tick(app)
    assert keys(sent) == []
```

- [ ] **Step 2: Run, expect** 404 on `/refill` (`assert answer.status_code == 200`).

- [ ] **Step 3: Constant and helper** — beside `CONTRA_CHANNEL`:

```python
# ch204 = seconds since the float last changed state, a bare count that
# restarts at boot: after a reboot `ts - ch204` is the boot time, later than
# any refill, and the rule below reads "moved" until the float really does.
# A false negative after a reboot, never a page.
FLOAT_AGE_CHANNEL = 204


def float_frozen(con: sqlite3.Connection, controller: int, now: int) -> int | None:
    """The refill the float has not moved across, or None. One reader for the
    ticker and the rules, so the page and the refusal cannot disagree: the
    latest ch204 reading says when the float last moved; if that is before
    the latest refill and the reading is PERSIST_S past the refill — the
    float had its minutes to settle — the float is presumed stuck."""
    row = con.execute(
        "SELECT ts, raw FROM readings WHERE controller = ? AND channel = ? "
        "ORDER BY ts DESC, rowid DESC LIMIT 1",
        (controller, FLOAT_AGE_CHANNEL),
    ).fetchone()
    if row is None:
        return None
    read_ts, age = row
    (refill,) = con.execute(
        "SELECT MAX(ts) FROM refills WHERE controller = ?", (controller,)
    ).fetchone()
    if refill is None:
        return None
    if read_ts - age < refill and read_ts - refill >= PERSIST_S:
        return refill
    return None
```

- [ ] **Step 4: The write and the route** —

```python
    def record_refill(controller: int) -> int:
        now = int(time.time())
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO refills (ts, controller) VALUES (?, ?)", (now, controller)
            )
        return now
```

and `POST /refill` in the `/interval` shape with `parse_board`, answering `refill={ts}\n`.

- [ ] **Step 5: The two readers** — in `water_rules`, after the latch check: `if float_frozen(con, r.controller, now) is not None: return  # presumed stuck: the wiring README's rule, enforced here`. In `evaluate`, after the latch loop:

```python
        # A float that has not moved across a refill is presumed stuck — the
        # one float fault the wiring cannot catch (the magnet off the float,
        # or stuck to the hall). Only the backend knows both the refill and
        # ch204, so the rule lives here and nowhere on the board.
        for (controller,) in con.execute("SELECT DISTINCT controller FROM refills"):
            key = f"stale:{controller}"
            refill = float_frozen(con, controller, now)
            if refill is not None:
                if not raised(key) and floor_ok(key):
                    found.append(
                        Alert(
                            key,
                            "high",
                            "warning",
                            f"the float on board {controller} has not moved since "
                            f"before the refill at {hhmm(refill)}: presumed stuck, "
                            "the rules will not water until it moves",
                            mark(key),
                        )
                    )
            elif raised(key):
                found.append(
                    Alert(
                        key,
                        "default",
                        "white_check_mark",
                        f"the float on board {controller} moved",
                        clear(key),
                    )
                )
```

- [ ] **Step 6: Full suite green; commit** — "Refills are recorded, and a float that never moved across one is presumed stuck".

---

### Task 8: Version, docs, fake device

**Files:**
- Modify: `butler.py:95` (`VERSION = "0.18.0"`), `pyproject.toml` (`version = "0.18.0"`), `AGENTS.md`, `README.md`, `fake_device.py`

- [ ] **Step 1: Version** — both files; `uv run pytest tests/test_hello.py -q` green.

- [ ] **Step 2: `AGENTS.md` "What is here"** — add, in the endpoint list, `POST /controller` (`c= retired=`), `POST /refill` (`c=`), `POST /resume` (`c=`), and to `GET /health`'s description the six new per-controller fields. Add one bullet after "The command slot" bullet:

> - **The tank (0.18.0, pitch "Trust the tank").** The board's `err=` is stored on `status` (last value and when). `ch207=1` or `err=contra` latches the backend (`resetmid` too): `water_rules` goes dry, `POST /command water=` answers 409, the queued dose expires, and `latch:<c>` pages high without the re-alert floor — until `POST /resume`, the human's half, which the app offers beside the words "type `clear contra` on the board". The float going empty does not latch: the rules already refuse on it. `POST /refill` records a human refill; `float_frozen()` — one reader for the ticker and the rules — presumes the float stuck when the latest `ch204` says it last moved before the latest refill and `PERSIST_S` has passed: `stale:<c>` pages and the rules stay dry, manual water is not gated. `POST /controller c= retired=1` retires a board: reports land, nothing pages or waters, a standing silence page is cleared. `MAX_DOSE_ML` is 250, the board's own ceiling, at `/command` and at pot save. The daily cap charges acked water only (a lost response is likelier than a lost ack; the cooldown still counts the handed dose). `pos:<c>` pages only once a board has ever said `pos=ok`.

and the version history sentence: "0.18.0 on 2026-09-05 with the tank".

- [ ] **Step 3: `README.md`** — after the `/interval` curl at line 73, three more curls (`/controller`, `/refill`, `/resume`) with one line each on what they answer, and in "When something is wrong" (line ~238) two clauses: a latched board, and a float that never moved across a refill.

- [ ] **Step 4: `fake_device.py`** — read it first. Add three flags that ride on every report: `--err TOKEN` (adds `err=TOKEN`), `--contra` (adds `ch207=1`), `--float-age N` (adds `ch204=N`), following how `--float`/`--pos` are added. Nothing else.

- [ ] **Step 5: Full suite green; commit** — "0.18.0: the tank, in the docs and the fake board".
