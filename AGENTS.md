# Working on the backend

Not started (2026-08-30). Read the umbrella's
[AGENTS.md](https://github.com/plantbutler/plantbutler/blob/main/AGENTS.md) (on this machine: `~/projects/plant-butler/AGENTS.md`) and
[DECISIONS.md](https://github.com/plantbutler/plantbutler/blob/main/DECISIONS.md) first; decisions
#2, #4, #5 and #6 are the ones this repository implements.

## What it is going to be

One Python container with SQLite on a bind-mounted volume, in Docker on the Synology NAS,
reachable on the LAN only. One static token. Server timestamps on arrival. Raw readings kept
forever; percentages derived at read time from two calibration numbers per channel, never stored.

The board talks first: one plain-HTTP round trip per report interval, `k=v` lines each way. The
response carries the next interval and at most one pending command (`water outlet k: N ml, capped
at S s`, or `stop`), one-shot and expiring, acknowledged by the report after the one that carried it. No
MQTT, no JSON, no server on the board.

The backend decides when to water (thresholds, smoothing, per-pot cooldown, daily cap, quiet
hours) and never enqueues on a stale heartbeat or an empty reservoir. NAS or WiFi down means no
watering.

## What is here (2026-09-01)

- `butler.py` — the whole service: `create_app` factory (env: `BUTLER_TOKEN` required,
  `BUTLER_DB`, `BUTLER_NEXT_S`, `BUTLER_CMD_TTL_S`, `BUTLER_QUIET`,
  `BUTLER_NTFY_TOPIC`, `BUTLER_NTFY_URL`, `BUTLER_DEADMAN_URL`, `BUTLER_SILENT_S`,
  `BUTLER_TREFLE_TOKEN` — unset means every care number is typed in, which is a working path and
  not an error, `BUTLER_PHOTOS` — where the photograph bytes live, `photos/` beside the database by
  default), `POST /report` (k=v body, `X-Token`
  header, refuses whole on malformed channels, ignores unknown keys, stamps arrival time,
  answers `next=` plus at most one `cmd=` line), `POST /command` (queue `water=<outlet>
  ml= [cap_s=]` or `stop=1`, one slot per controller, 409 when busy; a missing cap_s is sized
  by `cap_for`, the one owner of FLOW_FLOOR_ML_S), `POST /interval`
  (per-controller `next=` override, 0 clears), `POST /controller` (`c= retired=`: retire a board
  or bring it back; answers `controller=<c> retired=<0|1>`), `POST /refill` (`c=`: a human
  refilled that board's tank; answers `refill=<ts>`), `POST /resume` (`c=`: lift the latch;
  answers `resumed=<c>`, idempotent), `POST /pot` (partial edit keyed on `id=`, the
  `pot-xxxxxx` a bare `name=` create mints and every answer carries; mapping, calibration,
  Planta-style fields, rules knobs. A post without an `id=` is always a CREATE: a taken name is
  refused, not quietly edited, so a stale client cannot overwrite a pot it could not see. Which
  also means a client that keys on the name forks the pot the day it is renamed and the old name
  comes free. Refuses inconsistent merges and channel/outlet collisions), `GET /pots` (the garden with latest raw, derived %, any open
  proposal and the last handed dose with its verdict), `GET /history` (`pot= hours= bucket_s=`: bucketed raw counts with
  lo/hi/n and the server's `since`/`to`, `since` on a bucket boundary, `hours` up to a month and
  at most 2016 buckets — the bucket cap is the one that bites, so a month has to be asked for
  hourly. By pot, not by channel: a plant wired into a dead one's socket must not open its chart
  on somebody else's curve. Takes no token, which now means an unauthenticated caller can confirm
  a pot id exists — accepted, and a pot that does not answers 200 with no points. The chart's
  wire), `POST /pot/delete` (`id=`: erase a pot, its wiring, its readings, its doses and their
  verdicts, its dismissed advice, and its photographs with their files. Its own route so a save
  that lost its body can never become an erasure; the graveyard, `status=graveyard` on `POST
  /pot`, is the reversible half and is what an app offers first),
  `GET /doses` (`pot= limit= before= before_id=`: the watering history, newest first — what was
  asked, what the meter counted, how it ended, the verdict, and the pot attributed through its own
  mapping windows. Proposals are left out (offers, not water) and so are stops (no outlet, no
  millilitres, never attributable); the expired, unacked and short-flowing rows are not, since they
  are what the list is for. Without `pot=` the whole garden, and a dose no window claims carries a
  null pot rather than vanishing; with `pot=` only handed doses can appear, because a dose belongs
  to a pot from the moment the board is given it. The pot on a dose is the STAMP the row carries,
  written when the command was, not a window join at read time. `before=`/`before_id=` are the last row you have,
  both together because several doses can share a second and a timestamp-only cursor would skip or
  repeat them — the table is never pruned, so the older history has to stay reachable),
  `GET /species` (`q=`: the taxonomy hop through GBIF then Trefle for the accepted binomial,
  both cached — `matched` is exact|fuzzy|common|genus|none|unavailable, `care` is null when
  nothing could be asked, `candidates` is the shortlist with pictures when no name could be
  placed, and `note` is the sentence for the screen. No watering number comes back: see the band
  below), `POST /advice` (`pot= kind=target dismiss=1`: this offer was refused, keyed on
  a fingerprint of the numbers refused, so a different offer is asked again. There is no accept —
  accepting is an ordinary `POST /pot`),
  `POST /approve` (proposed -> queued, slot permitting), `POST /verdict` (ok | too_much |
  too_little per executed dose), `GET /health` (count, last ts, the default interval, per-controller
  heartbeat/knob/open command/safety fields — since 0.18.0 also `err`, `err_ts`, `pos_ok_seen`,
  `retired`, `latched` (`{since, reason}` or null) and `last_refill`, since 0.19.0 `tank_ml`,
  `tank_samples`, `pumped_ml` and `over` — raised alerts), `GET /hello` (`butler=<VERSION>`, or
  401 — the one gated route that neither writes nor reads the database, so a phone being set up can
  tell a wrong address from a wrong token, and a butler whose volume came unmounted can still say
  the token was wrong. `VERSION` lives in butler.py because the container installs no package; a
  test asserts it matches pyproject.toml),
  `POST /photo` (`?pot=&w=&h=` with the JPEG as the body — the one route whose payload is not k=v;
  JPEG checked by its first bytes so what is served back can always be labelled image/jpeg and never
  sniffed, 3 MiB cap, `w`/`h` a layout hint and nothing more, and the pot's `species` of the day
  stamped on the row), `GET /photos` (`?pot=&limit=`: the strip, newest first, each row with
  `missing`), `GET /photo/<id>` (the bytes, `nosniff`, immutable cache), `POST /photo/delete`
  (`photo=<id>`; its own route so an upload that lost its body can never be a deletion).
  Those four and only those are gated reads — everything else here is numbers about plants, and a
  photograph is the one thing that could show the inside of a house.
- **Photographs: the row is the truth.** Bytes under `BUTLER_PHOTOS` (default `photos/` beside the
  database, so they share the bind mount and are backed up or lost together), one directory per pot.
  A picture is listed, served and deleted by its row and the directory is never read to decide what
  exists, so a file no row knows about is invisible and harmless while a row whose file has gone is
  reported as `missing`. Keeping writes the file then the row (and unlinks the file if the row
  fails); deleting removes the row then the file. Both orders leave the harmless inconsistency.
  Neither connection is held across the disk write — a photograph is megabytes over a NAS volume,
  and a write transaction held that long is the board's reports blocked, the same trap the care
  lookup hit. Every id that becomes a path goes through `SAFE_ID` first, in `photo_path`, which is
  the only function that turns an id into a path.
- The rules ladder runs in-process on each fresh report, stateless, inside the report's own
  transaction: float=1 and pos=ok from that very report, outside BUTLER_QUIET (HH-HH, server
  local time — set TZ in the container), median of the last 5 readings below target_low_pct,
  no open command on the hose, cooldown passed (default 6 h, 0 disables), daily cap unspent
  (default 3 doses). Auto queues directly; learning proposes. The board does not send float=
  or pos= yet, so the rules ship dark and `fake_device.py --float/--pos` exercises them.
- The alert ticker — the one periodic thing here: every minute it evaluates alert rules from
  database state (controller silent, a mapped sensor's channel gone missing, `float=0` /
  `pos=unknown` seen twice in ten minutes, a safety field that vanished after being seen, a
  dose never acked / short on the meter / no moisture rise a soak later, a learning proposal
  waiting) and posts the transitions to `BUTLER_NTFY_TOPIC` (unset = alerts off; the topic name
  is the secret and lives in deploy.env). Cleared conditions re-raise at most hourly and dose
  failures page once per controller per hour. `BUTLER_DEADMAN_URL` is GET only after a fully
  clean pass — a quiet pass first proves ntfy reachable — so the butler dying and the butler
  losing ntfy both stop the pings; the observation window survives short restarts via a
  `meta:tick` row. `status` and `alerts` tables carry the state; `/health` shows `float`/`pos`
  per controller and what stands raised; one "butler is up" probe, at most daily, 10 min after
  a start.
- The care lookup and the band it does not come from. GBIF normalises what was typed (free, no
  key) and Trefle answers about the accepted binomial; `species_names` and `species_care` cache
  both hops, hits forever and misses for a month, so `GET /pots` reads caches only and never the
  network. GBIF knows scientific names only, so a name it cannot place falls to Trefle's own
  search, which matches common names, survives a typo and answers with pictures (`species_search`,
  cached the same way); exactly one candidate bearing the typed common name is followed
  (`matched: common`), two are a question with two pictures, and a name GBIF *did* place is never
  second-guessed with a shortlist. Two things the live services taught, both now in the code as comments: GBIF sends
  `matchType: NONE` with `confidence: 100`, so confidence alone means nothing; and GBIF matches a
  lowercase binomial but not a lowercase genus, so the cache key is lowercased and the question
  goes out in botanical case. Trefle has no watering regime at all (`soil_humidity` NULL for every
  species probed on 2026-09-04) and no houseplant coverage worth the name, so `target_band()`
  proposes the band locally from plant type, soil, pot size and month, `GET /pots` carries it as
  `advice`, and applying it is a human's `POST /pot`.
- The command slot: queued → handed exactly once in a report response (sent) → acked by the
  next report's `ack=<id> flow_ml=`; a no-ack report or the TTL expires it. Expired is gone —
  ask again. The commands table is never pruned: it doubles as the watering history.
- **The tank (0.18.0, pitch "Trust the tank").** The board's `err=` is stored on `status` (last
  value — a short token, digits included, since the I2C refusal's is `i2c` — and `err_ts` = when
  it last *changed*: the board repeats its last error on every report).
  `ch207=1` latches the backend, and so does `err=` *turning to* `resetmid` — an edge, never a
  level, and `err=contra` never latches: `err=` is sticky and `clear contra` on the console never
  touches it, so a level would re-latch a resumed board forever. A latch: `water_rules` goes dry,
  `POST /command water=` answers 409, the queued dose expires, and `latch:<c>` pages high without
  the re-alert floor — until `POST /resume`, the human's half, which the app offers beside the
  board's own word for the reason — `LATCH_STEP`, the one map the 409 and the `latch:<c>` page
  spell the steps from: `clear contra` for a contra, `dry off` for a board that reset with the
  pump running, which the firmware latches dry and `clear contra` does not touch, the contra
  words for a reason neither knows. A second fault landing while the latch stands overwrites
  the reason and keeps the stamp: the newest fault is the one to fix; both on one report name
  `resetmid`, the edge seen this once — the contra level re-latches after the resume. The float
  going empty does not latch: the rules already refuse on it. `POST /refill` records a human
  refill, and the tap means "full to the top": the row snapshots `status.float_word`, the
  board's last real word (a report that omits `float=` blanks `float_ok`, and a tap made under
  it counts all the same). The tank's counter starts at `counter_origin()` —
  the latest tap that saw the float (`base_tap()`; a NULL snapshot, the rows 0.18.0 left, is no
  base for anything), or the float's latest rise (`status.float_rise` — the word's own clock,
  which a report that omits `float=` leaves alone, where `float_since` is `float_ok`'s and the
  `fields:` rule's) once the word has gone 1 → 0 since that tap (the tap's `drop_ts`, stamped
  by the report path on the firm word's first drop after it — not on a `ch207=1` report, the
  contra latch being what forces the word: a forced 0 is not a drop; a drain under any other
  latch is) and risen strictly later: a float that went
  1 → 0 → 1 since the tap is a tank refilled by someone who forgot to tap; a rise with no drop
  since the tap is the tap's own refill reaching the float, a `clear contra` after a tap, the
  manual dose lifting the flap, and the tap stands; one that said nothing once has not moved;
  and a second drain keeps the rise rather than falling back to the tap — and the stuck-at-empty
  rule reads `latest_refill()`, snapshot and all; the ticker reads both once per board.
  `POST /controller c= retired=1` retires a board: reports land (the latch row included —
  it comes back with the board), nothing pages or waters — the dose judgement included — and
  whatever page stood for it is cleared, since no rule would ever clear it now. `MAX_DOSE_ML` is
  250, the board's own ceiling, at `/command` and at pot save. The daily cap charges acked water
  only (a lost response is likelier than a lost ack; the cooldown still counts the handed dose).
  `pos:<c>` pages only once a board has ever said `pos=ok`.
- **The tank has a size (0.19.0).** `pumped_since()` is the acked water handed out at or after
  the origin's second (the rise's own report hands a dose with the same clock), the daily cap's
  own expression; without an origin it is 0 and nothing arms. The float is read on its **firm**
  word — `status.float_firm`, the word once two consecutive reports carrying `float=` agree;
  `float_word` stays the last real word, kept across a report that omits `float=` — because
  one sighting is a glitch by the board's own design, and a slosh at report time must not
  close a sample early and hand the origin to its recovery. On the firm
  word's first 1 → 0 after the tap that saw it full, the report path stamps that tap's
  `drop_ts` where the word fell (`base_tap(fell)`: the latest tap before the fall, so a person
  who filled and tapped between the two sightings keeps a clean tap and the earlier one gets the
  run) and closes a `tank_samples` row with the water on the counter since the tap as of the
  confirming report — on that drop and no later one: one per tap, `INSERT OR IGNORE`, nothing
  on zero pumped, nothing on a second drain after an untapped refill (nobody said that refill
  was full), no stamp on a `ch207=1` report — the contra latch is what forces the word: "float
  OK, zero pulses" is a fault, not a drop, and stamped it let `clear contra` read as a rise that
  laundered the counter — while under any other latch the drop is stamped and only the sample
  waits, and no sample for a retired board. A firm drop that came with `ch207=1` is remembered
  (`status.float_forced`), and the firm word coming back out of it stamps no rise and clears
  the flag, so `clear contra` is never the origin — not even when the tap's `drop_ts` was
  already set, an untapped refill's run being exactly that. The duplicate check on
  `(controller, t)` runs before the status upsert and the edge, not only before the readings:
  the firmware retries with the body kept, and a retried glitch must not confirm itself. The
  rise (`float_rise`) is the firm word's too, stamped where the word rose rather than where
  the next report confirmed it, so the dose handed as the float rose stays on the counter. No
  carry at the upgrade: a 0.18.0 tap has a NULL snapshot and is no origin, so the first tap
  after the upgrade starts everything. `tank_ml()` is the
  median of the last `TANK_MEDIAN_OF` (5) samples, `None` under `TANK_SAMPLES_TO_ARM` (2). The
  float is judged against that volume, never a clock: `tank_state()` answers "over" when more
  than the size plus `TANK_TOLERANCE_PCT` (10) has been pumped since the origin and the float
  still says 1 — `water_rules` goes dry, `over:<c>` pages high ("since HH:MM", the origin; not
  while latched, not while the latest report carried `ch207=1` — `status.contra`, kept by the
  upsert, since a `/resume` before `clear contra` is typed lifts the latch and not the board's
  own — and not while retired), and a tap that saw the float, later than the page, is the only
  clear — not the float word dropping to 0, which is a contra, a flap or an omitted `float=` as
  often as an empty tank; the rules stay dry while the page stands (`over_stands()`: raised,
  not cleared, and no tap that saw the float later than the raise — the tap frees the rules
  and `/health` the moment it lands, as `/resume` lifts the latch, while the ticker's clear
  waits for ntfy to accept it), not on the live predicate alone, which a float bouncing 0 → 1
  untapped lets go of; `/health`'s `over` is 1 while the predicate holds or the page stands, 0
  for a retired board; `POST /command water=` is not gated. `float_dead()` — a tap made with
  the float at 0, a `float=` reading `PERSIST_S`
  or more after it (`status.float_seen`, not the wall clock: a board behind a WiFi drop has said
  nothing and is judged on nothing), and still 0 (`float_ok`: a report that said nothing said
  nothing) since before the tap (`float_word_since`) — pages `stale:<c>` (not while latched, on
  `ch207=1`, or retired) and nothing else, cleared when the float reads 1 (the key is the 0.18.0 clock rule's,
  so a page standing from it clears through the same path). Every sample is announced once as
  `tank:<c>:<refill_ts>`, marked like `dose:<id>` and left out of `/health` and the up-probe count
  like it, as a warning when it is more than `TANK_DRIFT_PCT` (25) off the median of the samples
  before it; the pending ones are found board by board from the latest announced one back
  (`unannounced_samples()`), never by scanning a board's life of samples every tick. `ch204`
  still lands and nothing reads it.
- The controller is an INTEGER on the wire and in every column, 0..255 (`MAX_CONTROLLER`), since
  0.17.0. It was free text, which made `c=` the one field a typo could turn into a second garden:
  a report from `bench1 ` opened its own controller row, heartbeat and alerts and nothing said the
  two were the same board. **Board 0 is a real board** and is what the app fills in by default, so
  every check is `is None` and never falsiness — `if not controller` refuses the commonest board
  there is. The firmware's `PB_CONTROLLER` is an integer too, asserted into range at compile time.
- Each pot in `GET /pots` carries `photo`: the id of its newest picture, for the thumbnail beside
  the name in the list. The id only — the bytes come from `GET /photo/<id>`, which the app caches —
  and the disk is deliberately NOT checked, unlike the strip, because /pots is fetched on every
  screen open and one stat() per pot on a NAS mount is a cost the list should not carry.
- Attribution is stamped, and the stamp is re-read when the board is HANDED the command, not when
  the command was written. A manual dose queued before its pot was registered carries no stamp at
  create time, and a hose rearranged while a command waits changes who gets the water — and a dose
  the pot half of the cooldown cannot see is one the hose floor stops covering the moment that pot
  is rewired, so both layers of decision 7 go at once. The rules' median window reads the pot's own
  readings, bounded to the last `RULES_WINDOW * 3` reports, so a socket that has changed hands
  cannot water a new plant on a dead one's dryness and a pot rewired after a month waits for its
  own five.
- `commands.id` is `AUTOINCREMENT`, so a deleted command's id is never handed out again. Without it
  a recycled id inherits the erased pot's verdict and its `dose:<id>` judgement row, and a real
  dose is then never judged.
- A pot has a `status`, a closed set of `alive` | `graveyard`, where it used to have an `enabled`
  flag. Every reader asks a positive allow-list (`butler.waters` / `butler.live_sql`), never
  `!= 'graveyard'`, so a word a newer backend invents does not water anything here. Burying a pot
  CLOSES its open mapping window — that is what frees the channel and the outlet — expires its
  open proposals, and drops its `sensor:`/`proposal:` alerts, which nothing else could clear once
  it left the loop that raises them. Restoring leaves it unwired. `status=graveyard` together with
  any wiring key in one body is refused: they are opposite instructions.
- One hose, one pot, and the mapping write enforces it: a live pot already on that
  (controller, channel) or (controller, outlet) is refused — asked whatever the pot being saved
  has for `status`, since the point is the other pot. A pot still holding the wiring is displaced:
  its open window closes as the newcomer's opens. This should now be unreachable, since burying is
  what unplugs, and it stays because a database that arrived with two open windows on one hose has
  no read-time GROUP BY papering over it any more — the reading stamp would pick one of the two
  arbitrarily, and permanently.
- `schema.sql` — additive-only DDL: `readings`, `commands`, `controllers`, `pots`,
  `pot_mappings`, the `pots_now` view, `verdicts`, `status`, `alerts`, `species_names`,
  `species_care`, `species_search`, `advice_dismissed` + indexes. Proposals are
  commands in state 'proposed'; the verdict log is the dataset adaptive dosing will one day fit
  on. A pot is a `pot-xxxxxx` id and a nickname; its wiring (controller, channel, outlet) is NOT
  in `pots` but in `pot_mappings`, one row per period with a half-open [from_ts, to_ts) window,
  and every reader asks the `pots_now` view for the pot as it is wired right now. `from_ts` 0
  means "since before that table existed". The one exception to additive-only is
  `butler.migrate()`: a one-time rebuild that retypes the old integer `pots.id` and moves the
  wiring out, run at startup, idempotent, inside a single transaction, leaving the database as
  it was at `<db>.pre-identity.bak` and one line on stderr saying so.
  Moisture % is derived at read time from each pot's (dry_raw, wet_raw), never stored:
  recalibrating reinterprets history instead of losing it. Attribution is NOT: `readings.pot_id`
  and `commands.pot_id` are stamped as the row lands, from the window in force at that moment.
  `pot_mappings` is demoted from "the join that answers whose dose it was" to "the source the
  stamp is read from, and the record of which sensor a pot was on" — the dose judgement still
  asks it for the channel, because the pot may have been rewired between the dose and the soak.
  What stays hose-keyed: the board addresses outlets, `_hose_since` fences a proposal to the pot
  that is on the hose now, and both watering floors count what went down a hose whoever it was
  attributed to (decision #5). A reading on a channel nobody is mapped to stamps NULL, which is an
  environment sensor or an unclaimed socket, and is unreachable from `/history` by design.
  `Dockerfile` — python-slim, port 9380, `/data` volume. `tests/` — the endpoints' contracts,
  `uv run pytest`.
- `fake_device.py` — stdlib board simulator: reports on the `next=` beat, executes the one
  command a response carries, acks it on the following report. `python fake_device.py --token
  dev --cycles 3` against a local uvicorn or the NAS.
- Pitches 1-4 (through the alerts) are deployed (2026-09-01; 0.7.0 with `last_dose`, `next_default`,
  `/history` and the optional `cap_s` on 2026-09-02; 0.8.0 with pot identity, `species`,
  `pot_mappings` and the one-time rebuild, 0.9.0 with `/doses`, and 0.11.0 with the create/edit
  split and a month of `/history`, on 2026-09-03; 0.12.0 with the species lookup and the target-band
  offer, then 0.14.0 with `GET /hello` and the photograph store, on 2026-09-04, verified live from
  the NAS — GBIF and Trefle for the lookup, and for the photographs a `/hello` that answers its
  version to the right token and 401 to a wrong one, a gated `/photos`, and an upload to a pot that
  does not exist refused without writing anything; 0.15.0 on 2026-09-04 turns `plant_type` into a
  closed set of six kinds, replaces the two free-text sizes with `pot_diameter_cm` and
  `plant_height_cm` and reads them as a water buffer and the demand on it, and answers `kind` from
  GBIF's family so the dropdown opens pre-selected; 0.18.0 with the tank on 2026-09-06, verified live: a fake board latched it through `ch207=1`, water was refused with the words to act on, `stop` passed, `/resume` cleared it, and retiring the fake board silenced it): container
  `plantbutler`
  on the NAS, port 9380, image `plantbutler-backend:0.18.0`, built on the NAS from the three files (`Dockerfile`, `butler.py`, `schema.sql`) copied over ssh into `/volume1/docker/plantbutler/build` (the NAS has no sftp, so `cat >` over ssh, not scp), database on `/volume1/docker/plantbutler/data`, secrets in `deploy.env` beside it
  (600, not in git: the token, the ntfy topic, the healthchecks.io ping URL, the Trefle token),
  `-e TZ=Europe/Zurich` so BUTLER_QUIET means local night. Photographs share that volume —
  `/data/photos`, one directory per pot — so they are backed up or lost with the database rather
  than separately, which is the one arrangement a restore cannot half-do.
  Recreating the container keeps the environment by dumping the running one's `BUTLER_*` and `TZ`
  into a temporary `--env-file` on the NAS and shredding it afterwards, so no secret is read out
  or retyped to redeploy. The rules run dark until the
  firmware sends `float=`/`pos=`; the alerts are live — the dead-man feeds healthchecks
  (which notifies by email, not ntfy) and the phone subscribes to the topic in the ntfy app.

## Pitches, in order (titles in the plan)

1. **Readings land on the NAS** (cycle 1, Claude, Jacopo reviews) — the container, the readings
   endpoint, the database. Done when `curl` from the LAN returns 200 and the row is there. Build
   on the laptop, ship the image; the unknown is Container Manager (image arch, volume
   permissions, port), not the code.
2. **Command hand-off** (cycle 1, stretch) — the command slot, heartbeat/last-seen, the
   next-interval knob, and a **fake-device script** so everything after this is testable without
   hardware.
3. **Pots, plants and calibration** — channel → outlet → pot → plant, two numbers per channel.
4. **Rules that water**, then **Tell me when it's wrong** (a public ntfy.sh topic).

Not in scope for any of them: Postgres, a migrations framework, TLS, a reverse proxy, Grafana,
Home Assistant, weather, ML. (A species *lookup* did arrive later, in "What does this plant
want?" — a cache in front of GBIF and Trefle, not a database of our own, and it decides no
watering numbers.)

## Design sketch (brainstormed 2026-08-31)

Wire, board → backend once a minute (POST, token, plain text `k=v`):

```
c=0  t=<uptime_ms>  float=1  pos=ok|unknown  last=ok|fault:...
ch0=8123 ... ch14=...
ack=<cmd id>  flow_ml=<counted>      # only on the report after executing a command
```

Response:

```
next=60
cmd=17  water=<outlet>  ml=50  cap_s=30    # empty when nothing is queued
```

Outlets are a flat index 0–14; only the board knows manifolds exist. A dose is ml counted on the
flow meter with a hard seconds cap; if the bench rig says the meter lies, fall back to
seconds-only and the meter stays safety-only.

Tables (`schema.sql`): `readings(ts, controller, channel, raw, t, pot_id)`; `pots(id, name, controller,
channel, outlet, plant_type, plant_size, pot_size, soil, dry_raw, wet_raw, target_low_pct,
target_high_pct, dose_ml, mode, cooldown_h, daily_cap_ml, status)` (`mode`:
manual | learning | auto) — mapping, calibration, thresholds and the descriptive
fields (Planta-style: what is potted, how big, in what) in one table until that hurts (it did
twice: since 2026-09-03 `pots.id` is a `pot-xxxxxx` and the three mapping columns live in
`pot_mappings` with a window, and since 0.15.0 the two sizes are `plant_height_cm` and
`pot_diameter_cm`, REAL, added by `add_columns()` rather than a second rebuild, and since 0.16.0
`enabled` is `status` — read the current shape above, not this line); `commands(id, created_ts,
outlet, ml, cap_s, state proposed→queued→sent→acked/expired/failed, source, result, verdict,
pot_id)` — the command log is the watering history, never pruned EXCEPT by `POST /pot/delete`; `events(ts, kind, detail)`. Percentages are derived at read time, never
stored. `schema.sql` stays additive, but `CREATE TABLE IF NOT EXISTS` is additive about tables
only — a column appended to a CREATE that already ran never reaches an existing database — so a new
column goes in the CREATE *and* in `butler.ADDED_COLUMNS`, which ALTERs it in at startup and can
carry a value over from an old column (`source`, row by row). `pots_now` is
dropped and recreated on every start for the same reason; it holds no data, and a view over a
column the table has not got yet parses fine and then fails on every read. Air temperature and
light ride the same readings table as extra channels (the sensor kit
has both modules); season is derived from the date. Adaptive dosing from range, temperature,
light and season is a later pitch — see the plan's Planta note — v1 rules stay thresholds.

Rules run in-process on each report arrival, no cron: median over a window → N consecutive dry →
cooldown → daily cap → quiet hours → heartbeat fresh and float ok and position ok → enqueue — directly in auto; in learning, a
proposal to approve and verdict at the pot. The flip to auto is a human act, per pot.

## When you start

Stack (chosen 2026-08-31): a `uv` project, FastAPI + uvicorn, standard-library `sqlite3`, one
`schema.sql`. NAS access exists: SSH with key auth (host alias in ~/.ssh/config on the laptop), and
passwordless sudo scoped to the docker binary only. Jacopo's standing rule, as of 2026-09-03:
backend deployment is autonomous — build the image, ship it, swap the container, run one-off
sqlite3 work on its own database, read logs, verify, and say afterwards what was done. Everything
else on the NAS still needs an announced ask before it runs: package installs, DSM settings,
other containers, writes anywhere but `/volume1/docker/plantbutler`. Reading is free.
The token and the NAS address never enter the repository.
