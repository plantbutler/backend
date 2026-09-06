-- The whole database. Additive only: new tables and indexes arrive as new
-- CREATE IF NOT EXISTS lines with the pitch that needs them (no migrations
-- framework, per the plan's no-gos). Raw counts are kept forever; percentages
-- are derived at read time and never stored.
--
-- One exception exists, and only one: butler.migrate() rebuilds `pots` at
-- startup to retype its primary key and move the wiring into pot_mappings,
-- which no CREATE IF NOT EXISTS can do. It runs once per database, in one
-- transaction, and leaves <db>.pre-identity.bak behind. Anything else that
-- needs a shape change on a live database is a new table, not a second one
-- of those.

CREATE TABLE IF NOT EXISTS readings (
  ts         INTEGER NOT NULL,  -- server arrival time, unix seconds
  controller INTEGER NOT NULL,  -- c= in the report, the board's own number
  channel    INTEGER NOT NULL,  -- chN key
  raw        INTEGER NOT NULL,  -- the count, uninterpreted
  t          INTEGER,           -- board uptime ms (t=), NULL when not sent.
                                -- Deliberately NOT unique: uptime restarts on
                                -- reboot, so values recur; retry dedup is a
                                -- time-windowed check in the app instead.
  pot_id     TEXT               -- whose reading it is, stamped as the row
                                -- lands from the pot_mappings window then in
                                -- force. NULL when nothing was mapped there
                                -- (an environment channel, or a socket
                                -- nobody has claimed). Never recomputed: the
                                -- board sends a channel, and which plant sat
                                -- on it at 04:00 is not a fact the board can
                                -- be asked about later.
);

CREATE INDEX IF NOT EXISTS readings_by_channel
  ON readings (controller, channel, ts);

CREATE INDEX IF NOT EXISTS readings_by_uptime
  ON readings (controller, t, ts);

-- The chart and the hard delete both read by pot. Without this they scan the
-- one table that grows without bound, on a NAS, and they degrade slowly
-- enough that no test would ever notice.
CREATE INDEX IF NOT EXISTS readings_by_pot
  ON readings (pot_id, ts);

-- Command hand-off (cycle 1 stretch). One slot per controller, not a job
-- system: queued -> sent (handed once, in a report response) -> acked, or
-- expired (no ack on the next report, or nobody collected it in time).
-- The command log doubles as the watering history, so rows are never pruned.
-- The one exception is a pot the owner has erased: POST /pot/delete removes
-- its commands with everything else of its. See DECISIONS.md.

-- AUTOINCREMENT, and it earns its keep: without it `id` is a rowid alias
-- and sqlite hands a deleted command's id straight back out. POST /pot/delete
-- makes that reachable, and a recycled id inherits the erased pot's verdict
-- and its `dose:<id>` judgement ledger row — so a stranger's verdict labels a
-- new dose, and a real dose is never judged at all. The delete removes both
-- of those anyway; this is the belt to that pair of braces, and the only one
-- of the two a race with the alert ticker cannot get past.
CREATE TABLE IF NOT EXISTS commands (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts INTEGER NOT NULL,
  controller INTEGER NOT NULL,
  kind       TEXT    NOT NULL,  -- 'water' | 'stop'
  outlet     INTEGER,           -- water only: flat outlet index
  ml         INTEGER,           -- water only: dose
  cap_s      INTEGER,           -- water only: hard seconds cap
  state      TEXT    NOT NULL,  -- 'queued' -> 'sent' -> 'acked' | 'expired'
  source     TEXT    NOT NULL,  -- who queued it ('manual' until rules exist)
  sent_ts    INTEGER,
  acked_ts   INTEGER,
  flow_ml    INTEGER,           -- what the board says actually flowed
  pot_id     TEXT               -- whom the dose was made for, stamped when
                                -- the row is written. NULL for a stop, and
                                -- for a hose no pot was on. The board is
                                -- still handed an outlet, not a pot.
);

CREATE INDEX IF NOT EXISTS commands_open
  ON commands (controller, state);

CREATE INDEX IF NOT EXISTS commands_by_pot
  ON commands (pot_id, sent_ts);

-- One row per controller that has ever reported or been configured:
-- heartbeat (last_seen) and the per-controller report interval override
-- (next_s, NULL means the BUTLER_NEXT_S default).

CREATE TABLE IF NOT EXISTS controllers (
  controller INTEGER PRIMARY KEY,
  last_seen  INTEGER NOT NULL,  -- 0 = configured but never heard from
  next_s     INTEGER,
  retired    INTEGER NOT NULL DEFAULT 0  -- 1: a board that is gone; reports
                                         -- still land, nothing pages or waters
);

-- Pots, plants and calibration (cycle 2). One row per pot: the two
-- calibration numbers, the Planta-style descriptive fields, and the knobs
-- the watering rules read. Percentages are derived at read time from
-- (dry_raw, wet_raw) and never stored, so recalibrating reinterprets
-- history instead of losing it.

-- The id is a random `pot-xxxxxx`, stable for the life of the pot, and the
-- only thing anything else keys on; the name is a nickname and may be
-- edited. The physical wiring is NOT here: it lives in pot_mappings with a
-- validity window, so remapping reinterprets history instead of misfiling
-- it, exactly as recalibration reinterprets percentages.

CREATE TABLE IF NOT EXISTS pots (
  id              TEXT    PRIMARY KEY,      -- pot-3f9a21, minted once
  name            TEXT    NOT NULL UNIQUE,  -- nickname, editable
  species         TEXT,                     -- what the care lookup keys on
  plant_type      TEXT,     -- one of butler.PLANT_KINDS, or NULL for "not sure"
  plant_height_cm REAL,     -- measurements, not adjectives: the band engine
  pot_diameter_cm REAL,     -- reads them as numbers (butler.size_shifts)
  soil            TEXT,     -- one of butler.SOIL_SHIFTS, or NULL for "not said"
  dry_raw         INTEGER,  -- calibration: raw count bone dry
  wet_raw         INTEGER,  -- calibration: raw count soaked
  target_low_pct  INTEGER,  -- the ideal moisture range
  target_high_pct INTEGER,
  dose_ml         INTEGER,  -- for the rules pitch
  mode            TEXT    NOT NULL DEFAULT 'manual',  -- manual|learning|auto
  cooldown_h      INTEGER,
  daily_cap_ml    INTEGER,
  status          TEXT    NOT NULL DEFAULT 'alive'  -- one of butler.POT_STATUSES
);

-- Where a pot was wired, and when. Exactly one open row per pot (to_ts IS
-- NULL) is its mapping now; a remap closes the open row and opens another.
-- from_ts 0 means "since before this table existed", which is what the
-- rebuild writes, so the whole of history stays attributed.

CREATE TABLE IF NOT EXISTS pot_mappings (
  pot_id     TEXT    NOT NULL,
  controller INTEGER,  -- which board its sensor reports from
  channel    INTEGER,  -- chN in that controller's reports
  outlet     INTEGER,  -- flat outlet index its hose hangs on
  from_ts    INTEGER NOT NULL,
  to_ts      INTEGER   -- NULL while this is the current wiring
);

CREATE INDEX IF NOT EXISTS pot_mappings_open
  ON pot_mappings (pot_id, to_ts);

CREATE INDEX IF NOT EXISTS pot_mappings_by_channel
  ON pot_mappings (controller, channel, from_ts);

CREATE INDEX IF NOT EXISTS pot_mappings_by_outlet
  ON pot_mappings (controller, outlet, from_ts);

-- Every reader that wants "the pot as it is wired right now" reads this and
-- gets the column shape the pots table used to have.
--
-- Dropped and recreated on every start rather than IF NOT EXISTS. A view
-- holds no data — it is derived, like a percentage — so rebuilding it costs
-- nothing, and IF NOT EXISTS would leave a database that predates a column
-- serving the old shape forever with nothing to say it had.

DROP VIEW IF EXISTS pots_now;
CREATE VIEW pots_now AS
SELECT p.id, p.name, p.species,
       m.controller, m.channel, m.outlet,
       p.plant_type, p.plant_height_cm, p.pot_diameter_cm, p.soil,
       p.dry_raw, p.wet_raw, p.target_low_pct, p.target_high_pct,
       p.dose_ml, p.mode, p.cooldown_h, p.daily_cap_ml, p.status
  FROM pots p
  LEFT JOIN pot_mappings m ON m.pot_id = p.id AND m.to_ts IS NULL;

-- Rules that water (cycle 2). Proposals live in the commands table as
-- state 'proposed' (source 'rules'); verdicts are the learning log — one
-- human judgement per executed dose, the dataset adaptive dosing will one
-- day fit on. Never pruned.

CREATE TABLE IF NOT EXISTS verdicts (
  command_id INTEGER PRIMARY KEY,  -- one verdict per dose; re-verdict replaces
  ts         INTEGER NOT NULL,
  verdict    TEXT    NOT NULL     -- 'ok' | 'too_much' | 'too_little'
);

CREATE INDEX IF NOT EXISTS commands_by_outlet
  ON commands (controller, outlet, sent_ts);

-- Tell me when it's wrong (cycle 2). `status` is each controller's latest
-- safety fields, with a `since` per value so a float bouncing at the
-- waterline or a manifold homing at boot must persist before it alarms.
-- `alerts` is the alerting state, one row per condition or judgement,
-- overwritten in place: which conditions are raised now, when a cleared one
-- may sound again, which doses are already judged. State, not history —
-- ntfy keeps no archive and neither does this (a no-go in the pitch).

CREATE TABLE IF NOT EXISTS status (
  controller     INTEGER PRIMARY KEY,
  ts             INTEGER NOT NULL,  -- when the latest report landed
  float_ok       INTEGER,           -- float= in that report, NULL if not sent
  float_since    INTEGER,           -- when float_ok last changed value
  pos            TEXT,              -- pos= in that report, NULL if not sent
  pos_since      INTEGER,           -- when pos last changed value
  float_seen     INTEGER,           -- last time float= arrived at all: its
                                    -- vanishing afterwards is its own alarm
  pos_seen       INTEGER,
  float_bad      INTEGER,           -- the last two float=0 sightings: two
  float_bad_prev INTEGER,           -- inside FLAP_WINDOW_S raise, so a float
                                    -- flapping at the waterline still pages
  pos_bad        INTEGER,           -- same, for pos=unknown
  pos_bad_prev   INTEGER,
  err            TEXT,              -- err= in the latest report that carried one:
  err_ts         INTEGER,           -- the board's last safety error, and when
  latched_ts     INTEGER,           -- the durable half of the board's contradiction
  latch_reason   TEXT,              -- latch: 'contra' | 'resetmid', NULL when not
  pos_ok_seen    INTEGER            -- last pos=ok ever seen; pos: pages only after one
);

-- A refill is a human event (pitch "Trust the tank"): the app says so, the
-- board cannot. The stuck-float rule reads the latest one per controller
-- against ch204, every tick, hence the index.
CREATE TABLE IF NOT EXISTS refills (
  ts         INTEGER NOT NULL,  -- server time when the human said so
  controller INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS refills_by_controller ON refills (controller, ts);

CREATE TABLE IF NOT EXISTS alerts (
  key        TEXT PRIMARY KEY,  -- silent:<c> | sensor:<c>:<ch> | float:<c> |
                                -- pos:<c> | fields:<kind>:<c> | dose:<id> |
                                -- dosefail:<c> | proposal:<c>:<outlet> |
                                -- latch:<c> (the board stopped itself) |
                                -- stale:<c> (float never moved across a refill), plus
                                -- meta:tick / meta:up bookkeeping rows
  raised_ts  INTEGER NOT NULL,
  cleared_ts INTEGER,           -- NULL while the condition stands
  detail     TEXT               -- dose judgements: 'ok'|'failed'|'unverified'
);

-- What does this plant want? (cycle 2). Two caches, because the two hops
-- have different lifetimes: many spellings resolve to one accepted name,
-- and one accepted name has one answer from the care source. Both keep the
-- date they were fetched, so an answer can always be told from a fresh one.

-- The taxonomy hop, GBIF. `query` is what the user typed, normalised;
-- `accepted` is the binomial to ask a care source about, NULL when GBIF
-- recognised nothing (or only a genus, which is not enough to look up).

CREATE TABLE IF NOT EXISTS species_names (
  query      TEXT PRIMARY KEY,  -- lowercased, whitespace-collapsed typing
  fetched_ts INTEGER NOT NULL,
  accepted   TEXT,
  rank       TEXT,              -- SPECIES, GENUS, ... as GBIF reports it
  matched    TEXT NOT NULL,     -- exact | fuzzy | genus | none
  family     TEXT               -- what the plant-kind guess is read from
);

-- The care source's answer for one accepted binomial. `found = 0` is a
-- real answer and is cached too: Trefle's houseplant coverage is empty,
-- not thin, so "nothing known" is the common case and must not re-ask on
-- every screen open. Nothing here is a watering number — Trefle has none
-- (soil_humidity was NULL for every species probed on 2026-09-04). The
-- target band comes from the local table, and only from there.

CREATE TABLE IF NOT EXISTS species_care (
  species     TEXT PRIMARY KEY,  -- the accepted binomial, lowercased
  fetched_ts  INTEGER NOT NULL,
  source      TEXT    NOT NULL,  -- 'trefle'
  found       INTEGER NOT NULL,
  common_name TEXT,
  light       INTEGER,  -- 0-10, the source's own scale, not a percentage
  humidity    INTEGER,  -- 0-10, atmospheric
  ph_min      REAL,
  ph_max      REAL,
  temp_min_c  REAL,
  image_url   TEXT
);

-- A target band the user said no to. One row per pot and kind, overwritten
-- in place — state, not history, like `alerts`. The fingerprint is the
-- numbers that were refused: propose something different (a new season, a
-- repot, a different soil) and it is a new offer, so it is raised again.

CREATE TABLE IF NOT EXISTS advice_dismissed (
  pot_id      TEXT    NOT NULL,
  kind        TEXT    NOT NULL,  -- 'target'
  fingerprint TEXT    NOT NULL,
  ts          INTEGER NOT NULL,
  PRIMARY KEY (pot_id, kind)
);

-- The fuzzy half of the lookup. GBIF only knows scientific names, so
-- "basil", "peace lily" and "tomatoe" resolve to nothing there; Trefle's own
-- search matches common names and tolerates a typo, and its results already
-- carry a picture, which is how a person confirms they found their plant.
-- One row per typing, since that is what was searched for.

CREATE TABLE IF NOT EXISTS species_search (
  query      TEXT PRIMARY KEY,
  fetched_ts INTEGER NOT NULL,
  candidates TEXT NOT NULL  -- JSON array of {name, common, image, slug}
);

-- A picture of the plant, over time (cycle 2). The bytes live on the
-- bind-mounted volume beside this database, one file per row, and this
-- table is the truth: a photograph is listed, served and deleted by its
-- row, and the directory is never read to decide what exists.
--
-- That settles the two reachable inconsistencies in opposite directions,
-- on purpose. A file no row knows about is invisible and harmless — it is
-- what a crash between the two writes leaves behind, and what a database
-- restored from an older backup than the volume leaves behind. A row whose
-- file has gone is the other way round, cannot be hidden, and is reported
-- as `missing` rather than served as a broken image.
--
-- `species` is what the pot said it was when the picture was taken. A pot
-- outlives its plant, and this is what lets the strip draw the break where
-- one plant ended and the next began without inventing a replant event.

CREATE TABLE IF NOT EXISTS photos (
  id      TEXT PRIMARY KEY,  -- photo-3f9a21b4, minted once; the filename too
  pot_id  TEXT    NOT NULL,
  ts      INTEGER NOT NULL,  -- server arrival time, unix seconds
  bytes   INTEGER NOT NULL,
  w       INTEGER,           -- what the phone says it downscaled to
  h       INTEGER,
  species TEXT
);

CREATE INDEX IF NOT EXISTS photos_by_pot ON photos (pot_id, ts);
