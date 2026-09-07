# The tank has a size — backend and app design

2026-09-06. Supersedes D6 of `2026-09-05-trust-the-tank-design.md` and the stuck-float half of
DECISIONS #29. Jacopo's rule, from the conversation that replaced them: a float that never
changes while the plants get watered and nobody taps "refilled" is presumed stuck; a person who
taps daily is looking at the tank and is never second-guessed. The meter counts what left the
tank, so the tank's size is *measured*, and the float is judged against volume, never a clock.

Decisions and traps only. The code says the rest.

## 1. What is already on the wire (no firmware change)

- `float=` 1/0 is the board's **word**, not the raw switch: the debounced float AND not the
  contra latch AND not the flap limit (`report.cpp:118`). A sample (D4) closes on that word,
  except on a contra report. *Amended 2026-09-07:* a contra (`ch207=1`) is "float OK, zero
  pulses", and zero pulses is also a dead meter, a kinked tube, a dead pump or 12 V absent
  (`safety.cpp:119-126`, the wiring truth table): a fault, not a measurement, so it closes no
  sample and the board's standing latch silences D6 and D7. A flap-forced 0 (three consecutive
  float refusals, `config.h:148-153`) means the level is at the line when it happens — a true
  sample — but the forced 0 **outlives a refill**: the firmware resets it only on a granted
  dose (`safety.cpp:105`), and the rules never grant while the word is 0. Only a manual dose
  from the phone gets out. D7's page says so. Trap for the firmware, filed as an issue: the wire
  cannot tell a flap latch from an empty tank; a channel for it would let the backend say which.
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
(`refill=<ts>`). The row gains `float_ok INTEGER`: the board's last real word at the tap —
`status.float_word`, *amended 2026-09-07, thrice*, not `status.float_ok`, which one report that
omits `float=` blanks, so that a tap made under a row reading "float ?" is not silently a tap
that counts for nothing — NULL only when the board has never sent `float=`. Schema: column in the `CREATE` **and** in `ADDED_COLUMNS`; the
rows already on the NAS get NULL, which D7 judges as nothing. Likewise `refills.drop_ts INTEGER`
(D3/D4), `status.float_rise INTEGER` and `status.contra INTEGER NOT NULL DEFAULT 0`. *Amended 2026-09-07, four times:* no
carry of `drop_ts` at the upgrade — every 0.18.0 tap has a NULL snapshot and is no origin, so
there is nothing to stamp; the first post-upgrade tap starts everything. (A carry written for an
intermediate build's shape was unreachable on the NAS and went.) The app's copy says what the tap means (D9).

**D3 — The counter.** `pumped_since(con, controller, since_ts) -> int`: `SUM(COALESCE(flow_ml,
ml))` over `commands` with `kind = 'water'`, this controller, `acked_ts IS NOT NULL` and
`sent_ts > since_ts`. The daily cap's expression: acked water, as the cap counts it. Trap: a dose
lost without an ack (`resetmid`) pumped something uncounted, so the rule fires late; the contra
latch stands behind it, as it does today. Accepted.

*Amended 2026-09-07.* `since_ts` is the **origin**, `counter_origin(con, controller) -> (ts, kind) |
None`: the later of the latest tap whose snapshot is not NULL (`kind = "tap"`, its `refills` row)
and the float's latest rise, `status.float_rise`: when the board's word last went 0 → 1, kept by
the status upsert beside `float_word_since` (`kind = "rise"`) — the word and its own clocks, not
`float_ok`/`float_since`, which a report that omits `float=` blanks and restarts (the `fields:`
rule counts from that): a float that said nothing once neither rose nor fell, so the rise it had
stands and none is invented when it speaks again. `float_word_since` moves on 1 → 0 and 0 → 1
only; `ADDED_COLUMNS` carries it from `float_since` under the same `float_ok IS NOT NULL` gate as
`float_word`, so a last pre-upgrade report that omitted `float=` cannot carry a clock without its
word. *Amended 2026-09-07, twice:* the rise is the origin **only after a drop that followed the
tap** — the tap's `refills` row carries `drop_ts`, the first time the word went 1 → 0 after it (D4
sets it, sample or not), and the origin is the rise iff `drop_ts` is set and `float_rise >
drop_ts` — `float_rise` and `drop_ts` both from the firm word's edges (D4), so a one-report
glitch moves neither. A 0 → 1 with no drop after the tap is the tap's own refill arriving on the wire after a
tap made at empty, a `clear contra` after a tap, the manual dose lifting the flap after a tap: all
leave the tap as the origin (the second review found the first wording lost the ordinary "tank
empty, fill, tap" run's sample to exactly this). Sticky: a second drain after an untapped refill
keeps the rise as the origin — `float_rise` stays where it was — rather than falling back to the
tap and resurrecting water already sampled; and a rise out of a forced 0 (D4, `float_forced`)
stamps no `float_rise`, so `clear contra` never becomes the origin. Same second: the tap. Two reasons. A tap from 0.18.0 (NULL snapshot) never meant "full to the top" — a month of untapped
top-ups behind it would have become a 12 L sample and a threshold no stuck float ever reaches — so
it is no origin for anything. And a float that went 1 → 0 → 1 since the tap is a tank that ran down
and was refilled by someone who forgot to tap: the float demonstrably moved, so the counter restarts
at the rise instead of calling it stuck twenty millilitres later. With no origin the counter is 0
and nothing below arms. `/health`'s `pumped_ml` is this counter. And `sent_ts >= since_ts`, not
`>`: a dose pumps after it is handed and a tank is filled before its tap, so one handed in the
origin's own second left the full tank — and the report that raises the word hands its queued
command with the same `now`, so the strict comparison lost the resuming dose for ever.

**D4 — Learning a sample.** In the report transaction, before the status upsert, read the
previous float word (`status.float_word`, the board's last real `float=`, so a report that omits
the field hides no edge). When it was 1 and this report says 0, and the origin (D3) is a tap
`r`, and `pumped_since(r.ts) > 0`, insert `tank_samples(ts = now, controller, refill_ts = r.ts,
ml)`. `UNIQUE(controller, refill_ts)`: one sample per tap, so a float bouncing at the line adds
nothing after the first crossing (`INSERT OR IGNORE`). Zero pumped stores nothing: a tank
drained by something the meter never saw (evaporation, a tap that was not a fill) is not a
measurement. *Amended 2026-09-07, twice, then thrice:* on a 1 → 0 of the **firm** word — `status.float_firm`,
the word once two consecutive reports that carry `float=` agree — its clocks are this drop
(`drop_ts`) and the rise (`float_rise`), and no other: *2026-09-07, from the review*, a
`float_firm_since` beside it had no reader and went; the
wire's word is one glitch to 0 by design (`safety.cpp:35-41` fails any of three samples), and a
slosh at report time must not close a sample early and hand the origin to the glitch's recovery
— the latest non-NULL-snapshot tap's `drop_ts` is set if it was NULL, **unless the report carries
`ch207=1`** (*four times:* the contra latch is what forces the word; a `resetmid` board's float is
real, and a drain under that latch is a drop like any other — the latch blocks the sample below,
not the stamp): a forced 0 is a fault, not a drop, and stamping it let `clear contra` read as a
rise that laundered the counter and lost the run's sample. A forced 0 is remembered:
`status.float_forced INTEGER NOT NULL DEFAULT 0`, set on a firm 1 → 0 arriving with `ch207=1`,
and the firm 0 → 1 that ends it (`clear contra`) sets **no** `float_rise` and clears the flag —
the second review found the rise out of a forced 0 became the origin whenever `drop_ts` was
already set (an untapped refill, then a contra). *Four times, too:* the duplicate check on
`(controller, t)` runs **before** the status upsert and this edge step, not only before the
readings insert — the firmware retries a lost response with the body kept (`netfsm.cpp:161`),
and a one-glitch report delivered twice confirmed itself as a firm word. A sample is
inserted only when that `drop_ts` **was** NULL (the first drop after this tap — a second drain after an untapped refill
stores nothing, which is the rise-origin case: nobody said that refill was full), `pumped_since(tap)
> 0`, the report does not carry `ch207=1`, no latch stands, and the board is not retired (§1, D1). Trap: `status`
rows exist only once a controller has reported; a first report has no previous word and closes
nothing.

**D5 — The size.** `tank_ml(con, controller) -> int | None`: the median of the last
`TANK_MEDIAN_OF` samples by `ts`, `None` while fewer than `TANK_SAMPLES_TO_ARM` exist. Median,
not mean: one tap that was not a fill must not move the number by much. The median of two is
their mean; that is fine.

**D6 — Stuck at full, the dangerous one.** `tank_state(con, controller, origin) -> "unknown" | "ok"
| tuple`: `"unknown"` while `tank_ml` is None or there is no origin; `("over", pumped, tank,
origin_ts)` when `pumped_since(origin_ts) > tank * (100 + TANK_TOLERANCE_PCT) // 100` **and**
`status.float_ok == 1`; `"ok"` otherwise (a float that reads 0 is a float that works, and `float=0`
already refuses). `origin` is `counter_origin`'s answer, read once by the caller and handed to D6
and D7 alike (D7 needs the latest tap too); the comparison itself is one predicate, `is_over(tank,
pumped, float_ok)`, which `/health` applies to the numbers it already carries. `water_rules` skips
the controller on `"over"` (dry, decision #5), after the retired and latch checks — and, *amended
2026-09-07*, while `over:<c>` stands (`over_stands`: raised, not cleared, **and no non-NULL-snapshot tap
later than its `raised_ts`** — *amended 2026-09-07, thrice*: the ticker clears the row only
after ntfy accepts the clear message, and the rules and `/health` must honour the tap the
moment it lands, as `/resume` does for the latch; the ticker still sends "was refilled" when it
can. Trap, accepted: the ticker exists only with `BUTLER_NTFY_TOPIC` set, so without ntfy the
page is never raised and the live predicate is all there is): the live predicate
lets go on a float bouncing 0 → 1 with nobody tapping (a rise, a fresh origin, a counter at 0), and
the page was raised on a pump presumed stuck at full; the page is the fact until a tap answers it,
for the rules as for `/health`. The ticker raises `over:<c>` (high, with `floor_ok`; skipped while
the board's latch stands, while the latest report carried `ch207=1` — `status.contra`, kept by
the upsert from the report's `ch207`, absent means 0 — or while it is retired): "board N pumped X ml since HH:MM, more than its tank
holds (Y ml), and the float still says full: presumed stuck, the rules will not water until the next
refill". *Amended 2026-09-07:* the tap is the **only** clear — the page clears when raised and a tap
with a non-NULL snapshot is later than the alert's `raised_ts`: "the tank on board N was refilled".
Not on the float word dropping to 0, which is a contra, a flap or an omitted `float=` as often as an
empty tank, and "watering resumes" was untrue then. `/health`'s `over` is 1 while the live predicate
holds **or** `over:<c>` stands, and 0 for a retired board. No resume flow. `POST /command` is
**not** gated, as D6 of the old spec chose: a human is at the phone, the board's own float check
still runs, and the no-flow abort is beneath both.

**D7 — Stuck at empty, the harmless one.** `float_dead(con, controller, tapped) -> int | None`: the
latest refill `r` (`tapped`) has `float_ok = 0`, the board has said `float=` at least `PERSIST_S`
after the tap (`status.float_seen >= r.ts + PERSIST_S`), and `status` still says `float_ok = 0`
(this report's word: one that said nothing is not one that said empty) with `float_word_since <=
r.ts` → `r.ts`. *Amended 2026-09-07:* a reading, not the wall clock — a board on a five-minute beat
or behind a WiFi drop has said nothing yet and is judged on nothing, as the 0.18.0 rule's "waiting"
did. Trap that the `float_word_since` clause exists for: a float that went 0 → 1 after the tap and,
days later, legitimately back to 0 must not read as dead; its word last changed after the tap. Not
`float_since`: a report that omits `float=` restarts that one, and a float that said nothing once
has not moved. Skipped while the board's latch stands or the latest report carried `ch207=1`
(`status.contra`: a `/resume` before `clear contra` must not page the forced 0 as dead; contra
forces the word to 0, and the latch page already says what to do) and for a retired board. The ticker raises `stale:<c>` (high, with
`floor_ok`; the key is kept so a `stale:` standing from 0.18.0 clears through the same path): "the
float on board N still says empty M min after the refill at HH:MM: a stuck float, or the board's own
float check tripped — look at the magnet, or water once from the phone (a granted dose resets the
board's check)". It clears when raised and `status.float_ok == 1`: "the float on board N moved".
Page only: the rules are already dry on `float=0`, and this rule is not in `water_rules`. A refill
with `float_ok` NULL judges nothing.

**D8 — Every sample is announced, drift is warned.** The ticker pages each `tank_samples` row once,
keyed `tank:<c>:<refill_ts>` and marked like `dose:<id>` (a one-shot, never cleared): with at least
`TANK_SAMPLES_TO_ARM` *earlier* samples and `abs(ml - m) > m * TANK_DRIFT_PCT // 100`, where `m` is
the median of the earlier last five — priority default, tag `warning`, "board N's tank measured X ml
this run, not the Y ml it knew: a different tank, a clogging meter, or a tap that was not a fill";
otherwise priority default, tag `droplet`, "board N ran its tank down: X ml since the refill at
HH:MM (tank Y ml over K samples)" — with fewer than two samples, "(tank size learning, K of 2)".
Trap: `/health`'s raised list must exclude `tank:%` as it excludes `dose:%`, or the app shows every
announcement for ever. Retired boards: skipped, like `dose:`. *Amended 2026-09-07:* the pending rows
are found board by board (`unannounced_samples`), walking the index back from the newest to the
latest announced one and forward from there — the tick announces in order and stops at its first
failed send, so the announced ones are always the oldest — never by scanning every sample a board
has closed in its life on every tick.

**D9 — What the app sees and says.** `/health` controller entries gain `tank_ml` (int | null),
`tank_samples` (int), `pumped_ml` (int, since the origin, 0 without one), `over` (0 | 1; 0 for a
retired board). App (`ControllerHealth`: `tankMl`, `tankSamples`, `pumpedMl`, `over`).
*Amended 2026-09-07:* `tankSamples` is `Int?`, null when the key is absent — a 0.18.0 backend
must not read as "learning 0/2" and nag; the tank part of the line, the hint and `OVER` render
only when `tankSamples` is not null. A retired row shows none of the tank part, the hint or
`OVER`: retired is the last word and a quiet one. `learningGaps` names an over board ("the board's tank
not being over: refill to the top and tap refilled"), so a mode flip on one explains itself.

- `controllerLine`, after `pos`: `tank ≈4.2 L, 1.1 L pumped` when `tankMl` is known, else
  `tank learning 1/2`; and, after `STOPPED`'s slot, `OVER` when `over == 1`. Volumes through one
  helper `mlText`: below 1000 → `850 ml`, else one decimal → `4.2 L`.
- Under a row that is not retired and whose `tankSamples` is not null: while `tankSamples < 2`, a plain `bodySmall` line "Let the
  tank run empty twice without topping up, and tap refilled when you fill it to the top, so the
  butler learns its size."; when `over == 1`, an error-coloured line "board N pumped more than
  its tank holds while the float said full: check the float, refill to the top, then tap
  refilled." (*thrice*: past tense — `over` outlives the float word). *Four times:* no separate text
  when `float` is null — the tap snapshots the board's last real word (D2), so a tap under
  "float ?" counts; the app cannot tell a board that never sent `float=` from one that skipped
  it once, and must not tell the person a tap is blind.
- `problems()`: `"board N pumped more than its tank holds, float still says full"` when `over ==
  1` and `over:<c>` is not raised. `describeAlert`: `over` → "board N pumped more than its tank
  holds$since"; `stale` → "the float on board N still says empty after the refill$since: look at the magnet, or
  water once from the phone" (*thrice*: the flap latch's only way out must reach the phone); `tank`
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

**D12 — The latch step names the board's word for the reason.** *2026-09-07, from the review:*
a `resetmid` board latched **dry** on the firmware (`noinit.cpp:43`), and only `dry off` clears
that (`cli.cpp:478`); `clear contra` clears the contradiction latch only. The 409 text, the
`latch:<c>` page, `LATCH_TEXT` and the app's `LATCH_STEPS` (the card, the Resume dialog, the water
refusal) say "type clear contra on the board" for `contra` and "type dry off on the board" for
`resetmid`; one map in each repo, keyed by the reason, with the `contra` words for a reason it
does not know (the backend and the app alike). *Four times:* a new reason arriving while the
latch stands **overwrites** `latch_reason` (`latched_ts` stays): the newest fault is the one to
fix, and after a resume the remaining latch re-pages with its own words. Both on one report — a
standing `ch207=1` (it lives in `.noinit`, through a reset) and `err=` turning to `resetmid` —
name `resetmid`: the edge is seen this once and the level repeats until `clear contra`, so after
`dry off` and the resume the contra re-latches with its own words; contra first hid the reset for
ever behind a step already taken (the fix round's review). *Five times, from the second fix
round's review:* a repeat is not a new fault — the level re-asserts a standing latch under the
name it has and names `contra` only when it starts one (`latch_reason` is handed the standing
reason); left level-triggered, the board's next report after the edge, still carrying both,
renamed the reset `contra` before anyone had looked. Pre-existing in 0.18.0, fixed here
because D6/D7's pages send a person down the same steps.

**D13 — `ERR_TOKEN` accepts digits.** *2026-09-07, from the review:* the firmware's
`DOSE_REFUSED_I2C` token is `i2c` (`safety.cpp:150`); `[a-z_]{1,16}` refused it, and since `err=`
is the board's sticky last error, one I2C refusal made every later report a 400 until reboot.
`ERR_TOKEN = re.compile(r"\A[a-z0-9_]{1,16}\Z")`. Pre-existing in 0.18.0.

## 4. Global constraints (unchanged from the 0.18.0 spec)

Board 0 is falsy: every controller check is `is None`. Every write runs under `BEGIN IMMEDIATE`
inside `with connect() as con:`. Every new column is in the `CREATE` and in `ADDED_COLUMNS`.
Tests are real behaviour through `TestClient`, the ticker driven with `app.state.tick(now)`.
Commit messages end with `🤖 Written by an agent on behalf of @jcanton`, no other trailer.
