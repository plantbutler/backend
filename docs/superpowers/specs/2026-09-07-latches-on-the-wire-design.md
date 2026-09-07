# Latches on the wire — firmware, backend and app design

2026-09-07. plantbutler/firmware#4 and plantbutler/backend#27. Supersedes the "water once from
the phone" way out in D7 of `2026-09-06-tank-size-design.md` and makes the `err=resetmid` edge of
trust-the-tank D2 a fallback. No board runs this firmware yet, so the wire changes now, before
bring-up; after it, it would cost a reflash and a compatibility story.

Decisions and traps only. The code says the rest.

## 1. The wire (firmware)

- Two diagnostic channels after `ch209`: **`ch210` = the flap latch** (`safety_float_flap()`, 1
  while three consecutive float refusals stand, `config.h:148-153`) and **`ch211` = the dry
  latch** (`safety_dry()`, 1 while `g_nv.dry_latched` stands, set by a reset with a dose in
  flight and cleared only by `dry off`). In every report, as `ch207` carries the contra latch;
  clamped like the rest (the diag array grows 10 → 12, `report.cpp:104-113`). Absent on the wire
  means 0 to the backend, so an older board reads as never latched, never tripped.
- `float=` is unchanged: still the board's word (debounced AND !contra AND !flap). The channels
  say *why* it is 0; they do not change what it is.
- Trap: the report's size. `put_ch` writes into a fixed buffer with a cap the tests pin; two
  channels are about 16 bytes. Whatever asserts the longest report must be updated, not
  loosened.
- Tests, in `test/test_report`: `ch210=1` after `PB_FLOAT_FLAP_LIMIT` calls of
  `safety_float_refusal_count(true)` and 0 after a granted dose's `dose_end_ml_` path; `ch211=1`
  under `safety_dry_set(true)`; both 0 on a clean boot. `make check` stays green (its invariant
  count is pinned in `tools/check.sh`). The device binary builds (`pio run`) in the main
  checkout, which is the only one with `include/secrets.h`; no worktree.
- The firmware's AGENTS.md channel list, if it has one, names the two.

## 2. Backend (0.20.0)

Constants, exact: `FLAP_CHANNEL = 210`, `DRY_CHANNEL = 211`, `VERSION = "0.20.0"`. Schema, in the
`CREATE` **and** `ADDED_COLUMNS`: `status.flap INTEGER NOT NULL DEFAULT 0`, `status.flap_since
INTEGER` (when `flap` last went 0 → 1, kept by the upsert), `status.dry INTEGER NOT NULL DEFAULT
0`; all three from the report's channels, absent = 0, as `status.contra` is kept.

**D1 — The dry latch is a level.** `ch211=1` latches the backend with reason `resetmid` (the
words are already `dry off`, D12), exactly as `ch207=1` latches `contra`. When both stand on one
report, `contra` is the reason: its step comes first, and once `clear contra` is typed and
`ch207` goes, the dry level still stands and D14(d) re-pages with `dry off`. The `err=resetmid`
edge stays **only** for a report that carries no `ch211` at all (an older board); a report that
carries `ch211=0` is a board saying it is not dry, and the edge is not consulted. Closes
backend#27: a second reset mid-dose is a level again, not an edge that never comes.

**D2 — The flap is told apart.** `/health` controller entries gain `flap` (0 | 1). D7's page
when `flap = 1`: "the float on board N still says empty M min after the refill at HH:MM: the
board's own float check tripped — refill to the top and tap refilled, and the butler will try
one dose"; when `flap = 0`: "…: presumed stuck at empty, look at the magnet". The "water once
from the phone" sentence goes.

**D3 — A tap answers the flap.** `water_rules`' float gate becomes: `r.float_ok == 1`, **or**
`r.channels.get(FLAP_CHANNEL) == 1` and the latest non-NULL-snapshot tap is later than
`status.flap_since`. Then the rules queue their next dose as they would; the board's own float
check runs at dose time — granted if the float is up (the flap resets, `float=1` returns),
refused with `err=float` if not (one dosefail page, the flap stands, the rules are dry again
until the next tap). The tap is the human saying full; the board re-checks. Traps: the sample
and counter logic are untouched — a flap-forced 0 is a firm drop, since the level was at the
line when it tripped, and the tap after it has no drop after it, so the rise the granted dose
brings leaves the tap as the origin (tank-size D3, as amended). `POST /command` stays ungated.
The D6 over judgement needs both words full and is unaffected.

**D4 — `fake_device.py`** gains `--flap` and `--dry` (`ch210=1` / `ch211=1` on every report), for
the smoke.

Tests: `ch211=1` latches with the `dry off` words in the 409 and the page; both levels → `contra`
first, then after `/resume` and a report with `ch207=0 ch211=1` → re-latched, re-paged `dry off`;
a report without `ch211` → the `err=resetmid` edge still latches; with `ch211=0` → it does not;
`ch210=1` before any tap → dry; a tap later than `flap_since` → the next dose is queued; the
board's `float=1` afterwards → normal; a refusal (`ack= flow_ml=0 err=float`) → dosefail, dry
again; the two D7 texts; `/health` `flap`; the migration.

## 3. App

`ControllerHealth.flap: Int = 0`. `controllerLine` says `float check tripped` instead of `float
EMPTY` when `flap == 1`; `problems()` says "board N's float check tripped: refill to the top and
tap refilled" instead of "reservoir empty on board N" for that board; `describeAlert`'s `stale`
text loses its ": look at the magnet, or water once from the phone" tail (the page carries the
specifics now). Tests for the three.

## 4. Records

DECISIONS #31 in the umbrella: the board's three latches are on the wire and the backend latches
on levels; a tap answers the flap. plantbutler/firmware#4 and plantbutler/backend#27 closed by
the PRs. Global constraints as in the tank-size spec §4.

## 5. Amendments, 2026-09-07, after the review

**A1 — The tap's one try must not wait out the refusal's cooldown, and the dead page must not
fire before it.** The flap only trips on refusals, and a refusal is an acked dose with
`flow_ml=0`, which the cooldown gate counts as watering (loop protection, #29). So the D3 try
waited six hours while D7 paged at three minutes telling the person to do what they had just
done. Now: while `tap_answers_flap` holds (the flap stands, a non-NULL-snapshot tap later than
`flap_since`, no water handed since the tap — one try per tap, as implemented), the cooldown
gate ignores doses acked with `flow_ml = 0` before the tap; and D7 is skipped while it holds —
the try is pending. After the try: granted → `float=1`, all normal; refused → the ack spends the
tap, the refusal cools the pot as before, and D7 pages "the board's float check tripped — refill
to the top and tap refilled, and the butler will try one dose" on the next tick. The flap path
requires `r.float_ok == 0` (the word the flap forces), never an omitted `float=`.

**A2 — The level's reason is `dry`, and the edge stays beside it.** `ch211=1` is the board held
dry by whoever did it: a reset with a dose in flight, or `dry on` at the console. Its reason is
**`dry`**, text "the board is held dry: a reset with the pump running, or dry on at the
console", step "type dry off on the board". The `err=resetmid` edge keeps its reason
(`resetmid`, its text, the same step) and is consulted on **every** report, level or no level:
`dry off` typed at the console before the first post-reset report (bring-up 7c, exactly) would
otherwise hide the reset for good. When more than one applies on a report: `contra`, then `dry`,
then `resetmid`. `status.dry` joins `status.contra` in the alerts' quiet gate: no `stale:` or
`over:` page while the board's own dry level stands after a `/resume`. The app's `LATCH_WORDS`
and `latchSteps` gain `dry`.

**A3 — The app's flap line survives the page.** A flapped board earns the `float:<c>` page
within two reports, and the strip then rendered "reservoir empty on board N" for the life of
the flap. `problems()` renders a raised `float:<c>` for a board whose `flap == 1` as the tripped
line ("board N's float check tripped: refill to the top and tap refilled"), and `learningGaps`
says "the board reporting float=1 and pos=ok, or a tap after its float check tripped" when
`flap == 1`.

**A4 — Firmware tests assert the sibling.** Each latch test asserts the other channel is 0, and
one test latches both and reads both 1: a merged or masking encoding must fail.

**A5 — The page waits for a try that can come, not for one nobody will make.** A1 skipped D7
while `tap_answers_flap` held, and `flap_try_pending` read "no water handed since the tap" as
"the try is still to come". That is also what a board nobody will try looks like: no pot mapped,
every pot manual or learning, an auto pot not yet calibrated. The rules hand water only for a
live auto pot with what the ladder needs; a learning pot's proposal never gets a `sent_ts`
unless a human approves it, and one nobody approves expires and is proposed again, so there the
page was silenced for good. Now the try is pending while it is queued or with the board (handed
since the tap, neither acked nor expired), or while nothing has been handed since the tap and
the rules can still make it: a live auto pot on the board with the ladder's own fields
(`RULES_POT_SQL`, one predicate for both). A proposal standing is not a try coming — approving it
is the human's, as a `/command` is — and on a board with no such pot the page comes at
`PERSIST_S` as for a float presumed stuck, with D7's flap text: its refill-and-tap step is stale
advice to someone who just tapped, but the silence was worse.
