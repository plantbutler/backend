# Trust the tank — backend and app design

2026-09-05. Pitches: "Trust the tank" (pitch-d155fe, backend) and "I refilled the tank"
(pitch-85d502, app). Issue plantbutler/backend#24, items 1–5, 7, 8 and 10. Item 9 was already
done (`schema.sql` has `AUTOINCREMENT`); item 6 waits for bring-up 7b's measured ml/s.

Decisions and traps only. The code says the rest.

## 1. What the board sends (firmware `main` at 775af92)

- `ch204` = seconds since the float (D5) last changed state. A bare non-negative integer, `0`
  before it ever moved. **It restarts at boot** (the board cannot know across a reset), so after a
  reboot `ts − ch204` is the boot time, later than any refill, and the stale rule below reads
  "moved" until the float really moves. Accepted: a false negative after a reboot, never a page.
- `ch207` = the board's contradiction latch, 0 or 1, **in every report** while it stands. It lives
  in `.noinit`: survives a warm reset, lost on a power cycle. That loss is why the backend holds
  the durable half.
- `err=<token>` = the board's last safety error, one of `contra dry float goto heap noflow none
  range recv resetmid stop txcap` (`report.cpp:146`; the tokens come from `safety.cpp` and
  `exec.cpp`). An unknown key today, so `parse_report` discards it.
- `float=` 1 floats / 0 empty; `pos=` ok | unknown; `ack=<id> flow_ml=<n>` on the report after a
  command. A dose above `PB_DOSE_RIG_MAX_ML` (250) is refused by the board with `err=range` and
  acked with `flow_ml=0`.

## 2. Decisions

**D1 — Schema, additive, through `ADDED_COLUMNS`.** A new table `refills(ts, controller)` with an
index on `(controller, ts)`. `controllers.retired INTEGER NOT NULL DEFAULT 0` — configuration, so it
sits with the interval knob. On `status`: `err TEXT`, `err_ts INTEGER`, `latched_ts INTEGER`,
`latch_reason TEXT`, `pos_ok_seen INTEGER` — state the board's reports set, so it sits with
`float_ok`/`pos`. Every column goes in the `CREATE` **and** in `ADDED_COLUMNS` (AGENTS.md: a column
appended to a CREATE that already ran never reaches an existing database). Trap: `status` rows
exist only once a controller has reported.

**D2 — What latches.** `ch207=1` or `err=contra` → reason `contra`; `err=resetmid` → reason
`resetmid` (the board reset with the pump on and latched itself dry; that deserves a human look
too, and a power cycle would erase it). `latched_ts` is set once and never overwritten while it
stands. Setting the latch expires that controller's `proposed` and `queued` water commands, as
burying a pot does — a dose still waiting would pour into a tank nobody has looked at.

*Deviation, recorded:* the firmware spec §2.7 also lists "`float=0` persisting from a controller
that was reporting `float=1`". Not a trigger here. The rules already refuse on `float=0`; a float
that goes 0 → 1 across a refill is demonstrably moving, which is the opposite of the fault this
latch exists for; and the refill button is the human event for an empty tank. Latching on it would
demand a "resume" tap after every ordinary empty tank.

**D3 — What a latch does.** `water_rules` returns before it looks at any pot (dry). `POST /command`
with `water=` answers 409 `refused: board N stopped watering (contra since HH:MM): check the tank,
type clear contra on the board, then resume`; `stop=1` still passes. `/health` carries
`latched: {"since": ts, "reason": "contra"}` or `null` per controller. The ticker raises
`latch:<c>` (high) the tick after it is set, with the reason in the text, and clears it on resume.

**D4 — `POST /resume`**, body `c=<controller>`, token. Clears `latched_ts`/`latch_reason` and the
`latch:<c>` alert. Answers `resumed=<c>`; idempotent on a board that is not latched. The app's card
tells the operator to type `clear contra` on the board as well — the backend cannot do that.

**D5 — `POST /refill`**, body `c=<controller>`, token. Inserts `refills(now, c)`; answers
`refill=<ts>`. `/health` carries `last_refill` per controller.

**D6 — The stuck-float rule.** One helper, `float_frozen(con, controller, now) -> int | None`,
used by the ticker and by `water_rules` so the two cannot disagree: take the controller's latest
`ch204` reading `(ts, v)` and its latest refill `r`; the float last moved at `ts − v`; if `r`
exists, `ts − v < r` and `ts − r >= PERSIST_S` (the float had its three minutes), answer `r`. The
ticker raises `stale:<c>` (high): "the float on board N has not moved since before the refill at
HH:MM: presumed stuck, the rules will not water", and clears it once the float has moved after the
refill. `water_rules` skips the controller while it answers (dry, decision #5). `POST /command` is
**not** gated: a human is at the phone, and the board's own float check still runs. This is the
whole enforcement of the wiring README's rule; the firmware never refuses on staleness.

**D7 — Retirement.** `POST /controller`, body `c=<controller> retired=0|1`, token; answers
`controller=<c> retired=<0|1>`. The silence rule, the sensor rule and `water_rules` skip a
retired controller; its reports are still accepted and stored (a retired board is a quiet one,
not a rejected one). `/health` carries `retired`.

**D8 — `err=` is stored.** `parse_report` accepts `err=<token>`, `[a-z_]{1,16}`, at most once. When
present the report writes `status.err`/`err_ts`; when absent they are left alone (it is a
last-error field). `/health` carries `err` and `err_ts`. No alert of its own: `latch:` covers
`contra`/`resetmid`, and the rest is for the report view.

**D9 — The daily cap charges acked water only.** Both sums in `water_rules` become
`SUM(CASE WHEN acked_ts IS NOT NULL THEN COALESCE(flow_ml, ml) ELSE 0 END)`. Jacopo's call
(2026-09-05): a lost response is likelier than a lost ack, since the firmware never retries once
any response bytes arrived, and the cooldown — unchanged, still counted from `sent_ts` — keeps
spacing the doses. The "never acknowledged" page stays.

**D10 — One dose ceiling, in two places.** `MAX_DOSE_ML` becomes 250, the firmware's
`PB_DOSE_RIG_MAX_ML`, and bounds `ml=` on `/command` and `dose_ml` on `/pot`. The comment names the
macro: the two numbers move together. A pot saved above it before this is refused at its next
save; check the NAS database at deploy. The board keeps its own copy as the backstop — decision #5
says the firmware holds the invariants that must survive a wrong backend, and this is one.

**D11 — `pos:` waits for a first `pos=ok`.** `status.pos_ok_seen` is set by any report carrying
`pos=ok`; the `pos:<c>` rule raises only when it is not NULL. `fields:pos:<c>` is unchanged. A
board shipped with `PB_REPORT_POS_UNKNOWN=1` then raises nothing, and the rule is not deaf when the
flag is flipped. `/health` carries `pos_ok_seen`.

**D12 — The app (pitch-85d502).** `ControllerHealth` grows `latched`, `last_refill`, `err`,
`retired`, `pos_ok_seen`, every one defaulting. `Backend` grows `refill(c)` and `resume(c)`. The
controllers card gets a "refilled" chip per controller and, for a latched one, a card in the
backend's words — "board N stopped watering: <reason> since HH:MM. Check the tank, type `clear
contra` on the board, then resume." — with a Resume button behind one confirmation. `problems()`
adds "board N stopped watering (<reason>)" for a latched controller and gates "lost its manifold
position" on `pos_ok_seen`, mirroring D11. `cannotWater` refuses on a latched controller, after
the silence check and before the busy one, in the same words the backend would use.
`controllerLine` appends "stopped" or "retired".

**D13 — Version 0.18.0.** AGENTS.md and README follow. DECISIONS.md gets one dated entry: the
dose ceiling is one number in two places, and what latches the backend (D2, with the deviation).

## 3. Traps

- Board 0 is a real board and is falsy: every check is `is None`, never truthiness.
- The `status` upsert must leave `latched_ts`, `latch_reason`, `err`, `err_ts` alone unless this
  report sets them: `CASE` expressions reading the pre-update row, as the existing columns do.
- `add_columns` runs before `schema.sql` and skips a table that does not exist yet; a fresh
  database gets the columns from the `CREATE`. Both must list them.
- The ticker's `found` list is transitions only: raise once, clear once, `floor_ok` for re-raises.
- `refills` needs its own index; the stale rule reads the latest row per controller every tick.
- The app's `Json` ignores unknown keys and every new field defaults, so a phone against 0.17.0
  still parses; a 0.18.0 backend against the old phone just carries fields it never reads.
- Test the two writers of the latch (`ch207` and `err=contra`) and the two readers
  (`water_rules`, `/command`) separately; and prove `resume` clears the alert, not just the row.
