# Survey: what repeats in the 17 test files

Read before editing anything. 9,690 lines, 579 collected cases, all green.
Scaffolding: this file is deleted in the last commit of the branch.

`conftest.py` already holds `TOKEN`, `make_app`, `capturing`, the fixtures
(`db`, `photos`, `sent`, `pinged`, `settings`, `app`, `client`) and `post`,
`report`, `make_pot`, `health`, `tick`, `keys`, `run_sql`, `age_controller`,
`taps`, `word_since`, `origin`.

---

## 1. Helpers still declared in more than one file

### 1.1 The tank family — test_tank, test_tank_size, test_latches

`test_latches.py` imports eight names **from `test_tank.py`** — `age`,
`alerts`, `dose`, `dry_reports`, `learn_the_tank`, `rules_water`, `settings`,
`tap`. A test module importing another test module is the clearest sign the
helper belongs in `conftest.py`.

| helper | test_tank | test_tank_size | verdict |
| --- | --- | --- | --- |
| `age(db, seconds)` | 44 lines | 44 lines | **the same five UPDATEs and the same `tank:` key rewrite**; only statement order and the docstring differ |
| `refill` / `tap(client, db)` | `tap` calls `refill` | `tap` inlines the POST | one helper, written twice |
| `full(client)` | sends ` pos=ok` | does not | same but for `pos=` |
| `empty(client)` | — | no ` pos=ok` | pairs with `full` |
| `still_empty(client, db)` | sends ` pos=ok` | does not | same but for `pos=` |
| `rise(db)` | identical | identical | one line, twice |
| `dose(client, ml, float_ok=1)` | the meter always counts the dose | `dose(client, ml, flow=None)` over `hand` + `ack` | `dose(client, ml, flow=<the dose>, float_ok=1)` covers both; `test_tank_size.py:226` is the one site that wants `flow=None` |
| `dry_reports`, `alerts`, `rules_water`, `learn_the_tank` | here | — | imported from here by test_latches |

`pos=` probe, run and reverted: giving `test_tank_size.py`'s `full`, `empty`
and `still_empty` the ` pos=ok` that `test_tank.py`'s carry leaves all 45 of
its cases green. `pos` and the float are separate columns, a `pos=ok` can
only quiet the `pos:` rule and never raise it, and the real firmware sends
`pos=` on every report — so one helper serves both files.

`commands(db)` is declared in test_tank (`id, state, flow_ml`) and test_rules
(`id, state, source, ml, cap_s, outlet`), and `states(db)` in test_commands
(`id, state` as a dict). Same table, three projections that are each
load-bearing where they are: **not** one helper. They should each go through
`run_sql` instead of opening their own connection.

### 1.2 A pot minted from an answer

Five files end a pot create the same way — `assert answer.status_code == 200`
then `answer.text.split()[0].removeprefix("pot=")`:
`conftest.make_pot:106-108`, `test_photos.make_pot:31`, `test_species.make_pot:800`,
`test_pots.pot_id:26`, `test_delete.pot:23`, `test_doses.post_pot:56`,
`test_report.pot:35`. `test_photos.photo_id:42` is the same function on
`photo=`.

conftest's `make_pot` cannot replace them as it stands: its default pot is
**wired** to `controller=0 channel=0 outlet=3`, so a second call in one test
is refused for a collision, and both local versions mint unwired pots that
their files depend on (`test_photos.py:101`, `:546`; `test_species.py:815`
and five more, where the extra calibration would change the advice the case
asserts on). Closing the gap needs either `drop=` taking a sequence or a
bare mode starting from `{"name": "basil"}`.

### 1.3 The garden read

| | header | asserts 200 | returns |
| --- | --- | --- | --- |
| `test_photos.py:521` | `auth()` | yes | list |
| `test_pots.py:22` | none | no | list |
| `test_species.py:807` | `{"X-Token": TOKEN}` | yes | dict keyed on name |

`GET /pots` takes no token (`test_pots.py:308` asserts it), so the headers are
noise in two of the three. One `garden(client)` returning the list plus a
`by_name` and an `only_pot` serves all three — `(entry,) = garden(client)`
alone appears twelve times in test_pots.

### 1.4 A dose row written by hand

The same `INSERT INTO commands (created_ts, controller, kind, outlet, ml,
cap_s, state, source, sent_ts, acked_ts, flow_ml)` — one acked manual water
on outlet 3, `cap_s` 30, acked the second after it was sent — appears six
times in `test_tank_size.py` (232, 328, 923, 969, 1226, 1319) with only the
controller, the millilitres and the stamps differing. `test_tank.py:1424`'s
copy is inside an open transaction the case is inspecting and stays.

`water(...)` — queue a dose, hand it out, ack it through the real routes —
is written twice: `test_delete.py:34` and `test_doses.py:62`. The second
takes a `pot_id` it never reads and asserts the hand-off the first does not.

### 1.5 The token header, and the absence of one

`conftest.post` always sends `{"X-Token": token}`, so it cannot say "no
header at all". Both `test_photos.auth:26` and `test_hello.hello:18` were
invented for that, in opposite polarity. A shared `auth(token=TOKEN)` that
answers `{}` for `None` closes the gap for `post` too.

### 1.6 The board's own constants

`DRY = 11000` and `WET = 8000`, meaning 12% and 50% against `make_pot`'s
`dry_raw=12000 wet_raw=4000`, are declared in `test_alerts.py`,
`test_rules.py` and `test_tank.py`. `test_delete.py`'s `DRY, WET = 9000, 4000`
are its own numbers and stay.

### 1.7 A hand-rolled connection where `run_sql` would do

`test_columns.columns` is `[r[1] for r in run_sql(db, f"PRAGMA table_info({t})")]`;
`test_delete.count` is a `SELECT COUNT(*)` projection over it;
`test_pots.mappings`, `test_report.rows`/`stamps`, `test_rules.commands`,
`test_commands.states` all open their own connection for one statement.
Single-statement `with sqlite3.connect(db)` blocks inside case bodies:
test_delete (7, including two that re-implement its own `count`), test_doses (1),
test_report (4), test_commands (5), test_pots (4), test_species (5),
test_migrate (4). Moving those removes `import sqlite3` from test_delete entirely.

### 1.8 The app built by hand

`TestClient(create_app(db_path=..., token=TOKEN, next_s=60, cmd_ttl_s=900))`
is written out four times in test_tank_size and once each in test_tank,
test_columns and test_species (`app()` at :146). `conftest.make_app` already
is that call, and its docstring says why it exists. Two sites in test_migrate
hardcode `"test-token"` rather than importing `TOKEN`. The two photo-store
tests (`test_photos.py:416`, `:510`) must keep calling `create_app` directly:
`make_app` always passes an explicit `photos_dir`, which is the very default
those two exist to check.

---

## 2. Families that should be one parametrised case

Only families whose assertions are structurally identical per row are listed.

| file | one case out of | rows | what differs |
| --- | --- | --- | --- |
| test_alerts | 5 silent dose judgements | 5 | `plant_dose` kwargs, the verdict row |
| test_alerts | 4 paged dose judgements | 4 | `plant_dose` kwargs, priority, the phrase |
| test_species | `kind_for` over 4 cases | 16 | species, family, kind |
| test_species | `target_band(...)[:2]` over 13 cases | 18 | kind, soil, diameter, height, month, band |
| test_species | the two "no kind" endpoints | 2 | one `gbif()` kwarg |
| test_species | the two cache cases | 2 | `aged_days` — different branches of the TTL gate, so neither row goes |
| test_fake_device | `build_report` over 4 cases | 5 | kwargs and the expected line |
| test_fake_device | `parse_response` over 2 cases | 3 | the response and the tuple |
| test_hello | the 3 refused tokens | 3 | the token; rows 2 and 3 gain the body assertion row 1 already makes |
| test_pots | the collision refusals | 2 | the two bodies |
| test_photos | the store's default location | 2 | `photos_dir` unset vs `""` |
| test_photos | the refused `/photo/delete` bodies | 3 | the body |
| test_report | a bad or absent token stores nothing | 3 | the headers |
| test_report | unknown keys are skipped, the rest lands | 2 | body and expected rows |
| test_commands | a sent command not acked expires | 2 | the third report's `t=` |
| test_commands | create_app refuses, naming its variable | 2 | kwargs and the `match` |
| test_rules | the cooldown and the cap allow one dose | 4 | pot kwargs, `flow_ml`, whether the hose moves |
| test_rules | an unattributable dose still spends | 2 | pot kwargs |
| test_history | two refusals belong in the table at :89 | +2 | hours and bucket — the table's assertion is stricter |
| test_delete | a sensor alarm freed by a change of wiring | 2 | bury vs remap |

The three `plant_dose` neighbours that stay whole each make a claim of their
own: `..._is_judged_for_the_pot_that_got_it` (the hoses are swapped and the
page must name basil, not mint), `..._the_soak_scales_with_a_slow_report_interval`
(builds its own app), `..._correlated_dose_failures_page_once_per_controller`.

Near-families deliberately left alone, because folding them would change an
assertion rather than move one: test_commands' two "the slot holds one
command" cases (one pins the whole text, one a substring), test_rules' three
"the freed slot is taken on the ack" cases (one omits the count), test_photos'
three upload refusals (one omits the strip check), test_species' five
`read_gbif` cases (four different assertion targets), test_delete's
delete-vs-bury pair (each has an assertion the other cannot make).

---

## 3. Cases whose assertions are a subset of another's

An AST pass for a case whose statements are a subsequence of another's in the
same file found one; the rest are from reading. A second pass comparing bare
assertion *sets* found thirty more, but nearly all are false — the same
expression under a different arrangement is a different claim.

| goes | carried by | why |
| --- | --- | --- |
| `test_report.test_a_retry_still_dedups_when_nothing_is_mapped` | `test_an_identical_retry_is_answered_200_and_stored_once` | the same two posts of `REPORT`; `stamps` and `rows` are one query under two projections, so `len` is the same number |
| `test_pots.test_a_pot_may_not_park_on_a_working_pots_hose` | `test_two_live_pots_cannot_share_an_outlet` | byte-identical assertions and second request; the extra `channel=0` on the first pot is irrelevant to an outlet collision, and the docstring promises a `status=` the body never sets |
| `test_pots.test_create_mints_an_id_and_returns_it` | `test_a_pot_is_born_from_one_line` | `answer.text == f"pot={id} name=basil\n"` with `id.startswith("pot-")` entails the 200, the split, and both halves |
| `test_migrate.test_migrate_leaves_a_backup` | `test_starting_on_a_live_old_database_rebuilds_it` | the same `old_db` seed and the identical `.exists()` expression |
| `test_doses.test_doses_needs_no_token` | `test_a_proposal_is_not_history` | its `get` helper posts no token and asserts 200; twelve cases in the file execute it |
| `test_species.test_a_typo_sized_measurement_is_capped_not_obeyed`, lines 383-386 only | `test_the_band_never_closes_or_leaves_the_scale` | its cross product contains all four of the sampled pairs and asserts the same bound; the two `size_shifts` cap lines are unique and stay |

Checked and cleared: test_rules' two proposal-expiry cases (one through the
board's report, one through `/approve`'s own TTL); test_commands' late-ack
case; test_photos' two id-refusal cases (one asserts 400 exactly, the other
`in (400, 404)` — merging would weaken it); `test_report`'s
`..._the_controller_is_an_integer_and_zero_is_a_real_board`, whose body is a
different report and whose docstring is the only statement that board 0 is
falsy.

---

## 4. Setup sequences repeated more than three times inside one file

| file | times | the sequence |
| --- | --- | --- |
| test_photos | 27 | `pot = make_pot(client)` |
| test_photos | 11 | that, then `pid = photo_id(upload(client, pot))` |
| test_photos | 5 | `assert strip(client, pot).json()["photos"] == []` |
| test_species | 14 | `client = app(db, Sources(BASIL))` |
| test_species | 8 | a raw `headers={"X-Token": TOKEN}` POST in the advice block |
| test_species | 6 | that client, then `make_pot(client, name="basil", plant_type="herb")` |
| test_species | 4 | the cache-ageing UPDATE on `fetched_ts` |
| test_pots | 12 | `(entry,) = garden(client)` |
| test_pots | 7 | `basil = pot_id(pot(client, "name=basil controller=0 channel=0 outlet=3"))` |
| test_pots | 5 | `pot(client, "name=basil controller=0 channel=0")` |
| test_doses | 9 | `now = int(time.time())` then `pot(db, "pot-1", "basil")` |
| test_delete | 9 | `furnished(client, db)`, six of them discarding the second value |
| test_delete | 5 | the alert insert; also in test_alerts, test_tank (x2) and test_tank_size |
| test_migrate | 10 | `path, con = old_db(tmp_path)`, eight followed by `migrate(con, path)` |
| test_latches | 10 | `make_pot(client, cooldown_h=0, daily_cap_ml=100_000)` |
| test_latches | 6 | that, `dry_reports(client, n=4)`, `assert "cmd=" not in flapped(client)`, `age`, `tap` |
| test_latches | 10 | the board refusing the try, `... ch210=1 ack=N flow_ml=0 err=float` |
| test_latches | 9 | `post(client, "/resume", "c=0")` asserted |
| test_rules | 10 | `make_pot(client, mode="learning")` then `soak(client, 5)` |
| test_rules | 7 | `make_pot(...)`, `soak(client, 5)`, `report(client, extra="ack=1 flow_ml=…")` |
| test_commands | 8 | `command(client, "c=0 water=3 ml=50 cap_s=30")` then `report(client, "c=0 t=1000 ch0=8000")` |
| test_tank | 9 | `learn_the_tank(app, client, db, sent, 200)`, `tap`, `dose(client, 250)` |
| test_tank_size | 6 | `full(client)`, `tap(client, db)`, `dose(client, 100, flow=90)` |
| test_alerts | 4 | `report(client)`, `see_everything(app)`, `now = int(time.time())` |
| test_columns | 4 | `con = sqlite3.connect(old)` … `con.close()` |

---

## 5. Two defects found on the way

`test_species.py:371` asserts nothing: `size_shifts(None, 40)[0] ==
size_shifts(None, 40)[0]`, both sides the same expression. The comment above
it says the intent was to compare against `POT_REF_CM`.

`test_species.py:328` — `assert high <= 30` follows from the `== (15, 25)` on
the line above it.

Five cases take a fixture they never use: `test_pots.py:223`, `:516`;
`test_photos.py:245`, `:527`, `:545`.
