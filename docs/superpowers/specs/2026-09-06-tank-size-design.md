# The tank has a size — backend and app design

2026-09-06. Supersedes D6 of `2026-09-05-trust-the-tank-design.md` and the stuck-float half of
DECISIONS #29. Jacopo's rule, from the conversation that replaced them: a float that never
changes while the plants get watered and nobody taps "refilled" is presumed stuck; a person who
taps daily is looking at the tank and is never second-guessed. The meter counts what left the
tank, so the tank's size is *measured*, and the float is judged against volume, never a clock.

Decisions and traps only. The code says the rest.

## 1. What is already on the wire (no firmware change)

- `float=` 1/0 is the board's **word**, not the raw switch: the debounced float AND not the
  contra latch AND not the flap limit (`report.cpp:118`). A sample (D4) closes on that word. A
  contra-forced 0 means the pump ran dry, so the tank really was empty: a true, slightly high,
  sample. A flap-forced 0 means the level is at the line: a true sample.
- `ack=<id> flow_ml=<n>` is the measured millilitres per dose. The firmware drops D6 the moment
  the float goes empty mid-dose, so the ack carries what flowed before the line.
- `ch204` stays in every report and stays stored as a channel. Nothing reads it after this.

## 2. Constants, exact

`TANK_SAMPLES_TO_ARM = 2`, `TANK_MEDIAN_OF = 5`, `TANK_TOLERANCE_PCT = 10`, `TANK_DRIFT_PCT = 25`,
`PERSIST_S = 180` (existing, reused), `VERSION = "0.19.0"`. `REFILL_SLACK_S` and
`FLOAT_AGE_CHANNEL` go.

## 3. Decisions

**D1 — The tank is the board's.** One tank per controller. Samples, the counter and every page
are keyed by controller. A retired board learns nothing and pages nothing; the rules skip it
already, and D4's insert skips it too: its reports still land, and a dose that was with the
board when it was retired still acks.

**D2 — The tap means "full to the top".** `POST /refill` is unchanged on the wire
(`refill=<ts>`). The row gains `float_ok INTEGER`: `status.float_ok` at the tap, NULL when the
board has never sent `float=`. Schema: column in the `CREATE` **and** in `ADDED_COLUMNS`; the
rows already on the NAS get NULL, which D7 judges as nothing. The app's copy says what the tap
means (D9).

**D3 — The counter.** `pumped_since(con, controller, since_ts) -> int`: `SUM(COALESCE(flow_ml,
ml))` over `commands` with `kind = 'water'`, this controller, `acked_ts IS NOT NULL` and
`sent_ts > since_ts`. The daily cap's expression: acked water, as the cap counts it. `since_ts`
is the latest refill's `ts`; with no refill the counter is 0 and nothing below arms. Trap: a dose
lost without an ack (`resetmid`) pumped something uncounted, so the rule fires late; the contra
latch stands behind it, as it does today. Accepted.

**D4 — Learning a sample.** In the report transaction, before the status upsert, read the
previous `float_ok`. When it was 1 and this report says 0, and a latest refill `r` exists, and
`pumped_since(r.ts) > 0`, insert `tank_samples(ts = now, controller, refill_ts = r.ts, ml)`.
`UNIQUE(controller, refill_ts)`: one sample per tap, so a float bouncing at the line adds nothing
after the first crossing (`INSERT OR IGNORE`). Zero pumped stores nothing: a tank drained by
something the meter never saw (evaporation, a tap that was not a fill) is not a measurement.
Trap: `status` rows exist only once a controller has reported; a first report has no previous
`float_ok` and closes nothing. Trap: a report that omits `float=` blanks `status.float_ok`
(its vanishing is its own alarm), so the edge is read off `status.float_word`, the board's
last word, kept across such a report — in the `CREATE` and in `ADDED_COLUMNS`, carried from
`float_ok` at the upgrade so a tank at full through it still closes its sample.

**D5 — The size.** `tank_ml(con, controller) -> int | None`: the median of the last
`TANK_MEDIAN_OF` samples by `ts`, `None` while fewer than `TANK_SAMPLES_TO_ARM` exist. Median,
not mean: one tap that was not a fill must not move the number by much. The median of two is
their mean; that is fine.

**D6 — Stuck at full, the dangerous one.** `tank_state(con, controller, tapped) -> "unknown" |
"ok" | tuple`: `"unknown"` while `tank_ml` is None or there is no refill; `("over", pumped, tank,
refill_ts)` when `pumped_since(refill_ts) > tank * (100 + TANK_TOLERANCE_PCT) // 100` **and**
`status.float_ok == 1`; `"ok"` otherwise (a float that reads 0 is a float that works, and
`float=0` already refuses). `tapped` is `latest_refill`'s answer, read once by the caller and
handed to D6 and D7 alike; the comparison itself is one predicate, `is_over(tank, pumped,
float_ok)`, which `/health` applies to the three numbers it already carries rather than reading
them again. `water_rules` skips the controller on `"over"` (dry, decision #5),
after the retired and latch checks. The ticker raises `over:<c>` (high, with `floor_ok`): "board N
pumped X ml since the refill at HH:MM, more than its tank holds (Y ml), and the float still says
full: presumed stuck, the rules will not water until the next refill". It clears when raised and
the state is no longer `"over"`: "the tank on board N was refilled, watering resumes". The tap is
the clear: the counter restarts at it, and a person who tapped looked at the tank. No resume
flow. `POST /command` is **not** gated, as D6 of the old spec chose: a human is at the phone, the
board's own float check still runs, and the no-flow abort is beneath both.

**D7 — Stuck at empty, the harmless one.** `float_dead(con, controller, tapped, now) -> int |
None`: the latest refill `r` (`tapped`, as in D6) has `float_ok = 0`, `now - r.ts >= PERSIST_S`,
and `status` still says `float_ok = 0` with `float_since <= r.ts` → `r.ts`. Trap that the
`float_since` clause exists for: a float that went 0 → 1 after the tap and, days later,
legitimately back to 0 must not read as dead; its `float_since` is after the tap. The ticker
raises `stale:<c>` (high, with `floor_ok`; the key is kept so a `stale:` standing from 0.18.0
clears through the same path): "the float on board N still says empty M min after the refill at
HH:MM: presumed stuck at empty, look at the magnet".
It clears when raised and `status.float_ok == 1`: "the float on board N moved". Page only: the
rules are already dry on `float=0`, and this rule is not in `water_rules`. A refill with
`float_ok` NULL judges nothing.

**D8 — Every sample is announced, drift is warned.** The ticker pages each `tank_samples` row
once, keyed `tank:<c>:<refill_ts>` and marked like `dose:<id>` (a one-shot, never cleared): with
at least `TANK_SAMPLES_TO_ARM` *earlier* samples and `abs(ml - m) > m * TANK_DRIFT_PCT // 100`,
where `m` is the median of the earlier last five — priority default, tag `warning`, "board N's
tank measured X ml this run, not the Y ml it knew: a different tank, a clogging meter, or a tap
that was not a fill"; otherwise priority default, tag `droplet`, "board N ran its tank down: X ml
since the refill at HH:MM (tank Y ml over K samples)" — with fewer than two samples, "(tank size
learning, K of 2)". Trap: `/health`'s raised list must exclude `tank:%` as it excludes `dose:%`,
or the app shows every announcement for ever. Retired boards: skipped, like `dose:`.

**D9 — What the app sees and says.** `/health` controller entries gain `tank_ml` (int | null),
`tank_samples` (int), `pumped_ml` (int, since the latest refill, 0 without one), `over` (0 | 1).
App (`ControllerHealth`: `tankMl`, `tankSamples`, `pumpedMl`, `over`):

- `controllerLine`, after `pos`: `tank ≈4.2 L, 1.1 L pumped` when `tankMl` is known, else
  `tank learning 1/2`; and, after `STOPPED`'s slot, `OVER` when `over == 1`. Volumes through one
  helper `mlText`: below 1000 → `850 ml`, else one decimal → `4.2 L`.
- Under a row that is not retired: while `tankSamples < 2`, a plain `bodySmall` line "Let the
  tank run empty twice without topping up, and tap refilled when you fill it to the top, so the
  butler learns its size."; when `over == 1`, an error-coloured line "board N pumped more than
  its tank holds and the float still says full: check the float, refill, then tap refilled."
- `problems()`: `"board N pumped more than its tank holds, float still says full"` when `over ==
  1` and `over:<c>` is not raised. `describeAlert`: `over` → "board N pumped more than its tank
  holds$since"; `stale` → "the float on board N still says empty after the refill$since"; `tank`
  → "board N measured its tank$since" (should never arrive; renders instead of echoing a key).
- `cannotWater` is not gated on `over`, mirroring the backend.
- The refilled chip keeps its label; the hint line is where the meaning lives.

**D10 — What goes.** `float_state`, `REFILL_SLACK_S`, `FLOAT_AGE_CHANNEL` and the old `stale:`
rule; the tests that encode them are rewritten to D6–D8, never deleted. `fake_device.py` needs
no new flag: `--float` and a dose ack are enough to run a tank down.

**D11 — Records.** DECISIONS #30 in the umbrella (append; #29 is not edited). The wiring running
note "A float input that never changes state across a refill is presumed dead: refuse, do not
assume OK." becomes "A float that still says full after more water than the tank holds has been
pumped since the last refill is presumed stuck: refuse and page. A float still saying empty
minutes after a refill is presumed dead: page (the rules are dry on empty already). The tank's
size is what the meter counted between a refill and the float going empty, the median of the
last five runs, armed after two." — in `nets.py`, README regenerated.

## 4. Global constraints (unchanged from the 0.18.0 spec)

Board 0 is falsy: every controller check is `is None`. Every write runs under `BEGIN IMMEDIATE`
inside `with connect() as con:`. Every new column is in the `CREATE` and in `ADDED_COLUMNS`.
Tests are real behaviour through `TestClient`, the ticker driven with `app.state.tick(now)`.
Commit messages end with `🤖 Written by an agent on behalf of @jcanton`, no other trailer.
