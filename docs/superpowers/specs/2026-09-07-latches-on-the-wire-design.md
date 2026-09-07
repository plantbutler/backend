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
