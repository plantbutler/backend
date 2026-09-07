-- The whole database. Additive only: a new table or index is a new
-- CREATE IF NOT EXISTS line, and a new COLUMN goes in the CREATE *and* in
-- butler.ADDED_COLUMNS, because IF NOT EXISTS is additive about tables alone
-- and never reaches a database the CREATE already ran on. Raw counts are kept
-- for ever; percentages are derived at read time and never stored.
--
-- One exception exists: butler.migrate() rebuilds `pots` at startup to retype
-- its primary key and move the wiring into pot_mappings, which no
-- CREATE IF NOT EXISTS can do. Anything else needing a shape change on a live
-- database is a new table, not a second one of those.

-- Every reading the boards have ever sent, never pruned.
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
                                -- lands from the window then in force, never
                                -- recomputed. NULL when nothing was mapped
                                -- there: an environment channel, or a socket
                                -- nobody has claimed.
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

-- One command slot per controller, and the watering history: never pruned,
-- except by POST /pot/delete taking an erased pot's rows with it.
--
-- AUTOINCREMENT, and it earns its keep: without it `id` is a rowid alias and
-- sqlite hands a deleted command's id straight back out. A recycled id would
-- inherit the erased pot's verdict and its `dose:<id>` judgement row, so a
-- stranger's verdict labels a new dose and a real dose is never judged.
CREATE TABLE IF NOT EXISTS commands (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts INTEGER NOT NULL,
  controller INTEGER NOT NULL,
  kind       TEXT    NOT NULL,  -- 'water' | 'stop'
  outlet     INTEGER,           -- water only: flat outlet index
  ml         INTEGER,           -- water only: dose
  cap_s      INTEGER,           -- water only: hard seconds cap
  state      TEXT    NOT NULL,  -- 'queued' -> 'sent' -> 'acked' | 'expired'
  source     TEXT    NOT NULL,  -- 'manual' | 'rules'
  sent_ts    INTEGER,
  acked_ts   INTEGER,
  flow_ml    INTEGER,           -- what the board says actually flowed
  pot_id     TEXT               -- whom the dose was for, stamped when the
                                -- board is handed it. NULL for a stop, and
                                -- for a hose no pot was on.
);

CREATE INDEX IF NOT EXISTS commands_open
  ON commands (controller, state);

CREATE INDEX IF NOT EXISTS commands_by_pot
  ON commands (pot_id, sent_ts);

-- One row per controller that has ever reported or been configured.
CREATE TABLE IF NOT EXISTS controllers (
  controller INTEGER PRIMARY KEY,
  last_seen  INTEGER NOT NULL,  -- 0 = configured but never heard from
  next_s     INTEGER,           -- report interval override; NULL = the default
  retired    INTEGER NOT NULL DEFAULT 0  -- 1: a board that is gone; reports
                                         -- still land, nothing pages or waters
);

-- One row per pot. Its wiring is NOT here — that is pot_mappings, with a
-- window — so remapping reinterprets history rather than misfiling it,
-- exactly as recalibration reinterprets percentages.
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
  target_low_pct  INTEGER,
  target_high_pct INTEGER,
  dose_ml         INTEGER,
  mode            TEXT    NOT NULL DEFAULT 'manual',  -- manual|learning|auto
  cooldown_h      INTEGER,
  daily_cap_ml    INTEGER,
  status          TEXT    NOT NULL DEFAULT 'alive'  -- one of butler.POT_STATUSES
);

-- Where a pot was wired, and when. Exactly one open row per pot (to_ts IS
-- NULL) is its mapping now; a remap closes that row and opens another in the
-- same second, and from_ts 0 means "since before this table existed".
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

-- Every reader that wants "the pot as it is wired right now" reads this.
-- Dropped and recreated on every start rather than IF NOT EXISTS: a view holds
-- no data, and IF NOT EXISTS would leave a database that predates a column
-- serving the old shape for ever with nothing to say it had.
DROP VIEW IF EXISTS pots_now;
CREATE VIEW pots_now AS
SELECT p.id, p.name, p.species,
       m.controller, m.channel, m.outlet,
       p.plant_type, p.plant_height_cm, p.pot_diameter_cm, p.soil,
       p.dry_raw, p.wet_raw, p.target_low_pct, p.target_high_pct,
       p.dose_ml, p.mode, p.cooldown_h, p.daily_cap_ml, p.status
  FROM pots p
  LEFT JOIN pot_mappings m ON m.pot_id = p.id AND m.to_ts IS NULL;

-- The learning log: one human judgement per executed dose. Never pruned.
-- Proposals are not here — they are commands in state 'proposed'.
CREATE TABLE IF NOT EXISTS verdicts (
  command_id INTEGER PRIMARY KEY,  -- one verdict per dose; re-verdict replaces
  ts         INTEGER NOT NULL,
  verdict    TEXT    NOT NULL     -- 'ok' | 'too_much' | 'too_little'
);

CREATE INDEX IF NOT EXISTS commands_by_outlet
  ON commands (controller, outlet, sent_ts);

-- Each controller's latest safety fields, with a clock per value so a float
-- bouncing at the waterline or a manifold homing at boot must persist before
-- it alarms.
CREATE TABLE IF NOT EXISTS status (
  controller     INTEGER PRIMARY KEY,
  ts             INTEGER NOT NULL,  -- when the latest report landed
  float_ok       INTEGER,           -- float= in that report, NULL if not sent
  float_since    INTEGER,           -- when float_ok last changed, NULL
                                    -- included: the fields: rule's clock
  pos            TEXT,              -- pos= in that report, NULL if not sent
  pos_since      INTEGER,
  float_seen     INTEGER,           -- last time float= arrived at all: its
                                    -- vanishing afterwards is its own alarm
  pos_seen       INTEGER,
  float_bad      INTEGER,           -- the last two float=0 sightings: two
  float_bad_prev INTEGER,           -- inside FLAP_WINDOW_S raise, so a float
                                    -- flapping at the waterline still pages
  pos_bad        INTEGER,           -- same, for pos=unknown
  pos_bad_prev   INTEGER,
  err            TEXT,              -- the board's last safety error, and when
  err_ts         INTEGER,           -- it last CHANGED: the board repeats it
  latched_ts     INTEGER,           -- the backend's own latch: when it began,
  latch_reason   TEXT,              -- and which fault, one of butler.LATCH_TEXT
  pos_ok_seen    INTEGER,           -- last pos=ok ever seen; pos: pages only after one
  float_word     INTEGER,           -- the board's last real word on the float,
                                    -- kept across a report that omits float=
                                    -- (which blanks float_ok): a report that
                                    -- said nothing must not hide a sighting
  float_word_since INTEGER,         -- when the word last changed, 1 -> 0 or
                                    -- 0 -> 1 and nothing else: the
                                    -- stuck-at-empty rule's clock
  float_rise     INTEGER,           -- when the firm word last went 0 -> 1: the
                                    -- tank's counter restarts here once the
                                    -- word has dropped since the tap
                                    -- (refills.drop_ts) and risen later
  contra         INTEGER NOT NULL DEFAULT 0,  -- ch207 in the latest report:
                                    -- the board's contradiction latch, which
                                    -- over: and stale: keep quiet under
  float_firm     INTEGER,           -- the word once two consecutive reports
                                    -- carrying float= agree; one sighting is a
                                    -- glitch by the board's own design. Both
                                    -- edges the tank is measured on are this
                                    -- word's, each stamped where the word
                                    -- MOVED. NULL is never an edge
  float_forced   INTEGER NOT NULL DEFAULT 0, -- the firm word's last drop came
                                    -- with ch207=1: the latch forcing the
                                    -- word, so the word coming back out of it
                                    -- is `clear contra` typed and no rise
  flap           INTEGER NOT NULL DEFAULT 0,  -- ch210 in the latest report:
                                    -- three float refusals in a row force the
                                    -- board's word to 0 until a dose is
                                    -- granted. Why float= is 0, when it is
  flap_since     INTEGER,           -- when flap last went 0 -> 1: the tap that
                                    -- answers it is later than this
  dry            INTEGER NOT NULL DEFAULT 0   -- ch211 in the latest report:
                                    -- the board held dry, released only by
                                    -- `dry off` at its console
);

-- A refill is a human event: the app says so, the board cannot, and the tap
-- means "full to the top". Read on every report and every tick, hence the
-- index.
CREATE TABLE IF NOT EXISTS refills (
  ts         INTEGER NOT NULL,  -- server time when the human said so
  controller INTEGER NOT NULL,
  float_ok   INTEGER,           -- the board's last real word on the float at
                                -- the tap. NULL when it had never sent
                                -- float=, and then the tap judges nothing and
                                -- starts no counter
  drop_ts    INTEGER            -- the first time the firm word went 1 -> 0
                                -- after this tap, unless that report carried
                                -- ch207=1 (the latch forcing the word: a
                                -- fault, not a drop). The run this tap
                                -- started closes here and no later, and a
                                -- rise counts only past it
);

CREATE INDEX IF NOT EXISTS refills_by_controller ON refills (controller, ts);

-- One measurement of a tank: the acked water handed out between a tap that saw
-- the float full and the firm word going empty. One per tap, hence the UNIQUE;
-- the size is the median of the last few by ts, hence the index.
CREATE TABLE IF NOT EXISTS tank_samples (
  ts         INTEGER NOT NULL,  -- the report that confirmed the float empty
  controller INTEGER NOT NULL,
  refill_ts  INTEGER NOT NULL,  -- the tap this run started from
  ml         INTEGER NOT NULL,
  UNIQUE (controller, refill_ts)
);

CREATE INDEX IF NOT EXISTS tank_samples_by_controller ON tank_samples (controller, ts);

-- The alerting state, one row per condition or judgement, overwritten in
-- place: what is raised now, when a cleared one may sound again, which doses
-- are already judged. State, not history — ntfy keeps no archive either.
CREATE TABLE IF NOT EXISTS alerts (
  key        TEXT PRIMARY KEY,  -- silent:<c> | sensor:<c>:<ch> | float:<c> |
                                -- pos:<c> | fields:<kind>:<c> | dose:<id> |
                                -- dosefail:<c> | proposal:<c>:<outlet> |
                                -- latch:<c> (the board stopped itself) |
                                -- over:<c> (pumped past the tank, the firm float
                                --   still full; the tap clears it inline) |
                                -- stale:<c> (float still empty after a refill) |
                                -- tank:<c>:<refill_ts> (a sample announced, once), plus
                                -- meta:tick / meta:up bookkeeping rows
  raised_ts  INTEGER NOT NULL,
  cleared_ts INTEGER,           -- NULL while the condition stands
  detail     TEXT               -- dose judgements: 'ok'|'failed'|'unverified';
                                -- latch:<c>: the reason its page named, so a
                                -- latch renamed while standing pages again
);

-- The taxonomy hop. Many spellings resolve to one accepted name, which is why
-- this cache is separate from the care one; `fetched_ts` is what tells a stale
-- answer from a fresh one.
CREATE TABLE IF NOT EXISTS species_names (
  query      TEXT PRIMARY KEY,  -- lowercased, whitespace-collapsed typing
  fetched_ts INTEGER NOT NULL,
  accepted   TEXT,              -- the binomial to ask a care source about;
                                -- NULL when nothing, or only a genus, matched
  rank       TEXT,              -- SPECIES, GENUS, ... as GBIF reports it
  matched    TEXT NOT NULL,     -- exact | fuzzy | genus | none
  family     TEXT               -- what the plant-kind guess is read from
);

-- The care source's answer for one accepted binomial. `found = 0` is a real
-- answer and is cached too, since houseplant coverage is empty rather than
-- thin. No watering number is here: the care source carries no watering
-- regime, and the target band comes from butler.target_band alone.
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

-- A target band the user said no to, overwritten in place. The fingerprint is
-- the numbers refused, so a different offer — a new season, a repot, another
-- soil — is a new question and is asked again.
CREATE TABLE IF NOT EXISTS advice_dismissed (
  pot_id      TEXT    NOT NULL,
  kind        TEXT    NOT NULL,  -- 'target'
  fingerprint TEXT    NOT NULL,
  ts          INTEGER NOT NULL,
  PRIMARY KEY (pot_id, kind)
);

-- The fuzzy half of the lookup: GBIF knows scientific names only, so "basil"
-- and "tomatoe" resolve to nothing there. One row per typing, since that is
-- what was searched for.
CREATE TABLE IF NOT EXISTS species_search (
  query      TEXT PRIMARY KEY,
  fetched_ts INTEGER NOT NULL,
  candidates TEXT NOT NULL  -- JSON array of {name, common, image, slug}
);

-- One row per photograph, and this table is the truth: a picture is listed,
-- served and deleted by its row, and the directory beside the database is
-- never read to decide what exists. So a file no row knows about is invisible
-- and harmless, while a row whose file has gone is reported as `missing`
-- rather than served as a broken image.
CREATE TABLE IF NOT EXISTS photos (
  id      TEXT PRIMARY KEY,  -- photo-3f9a21b4, minted once; the filename too
  pot_id  TEXT    NOT NULL,
  ts      INTEGER NOT NULL,  -- server arrival time, unix seconds
  bytes   INTEGER NOT NULL,
  w       INTEGER,           -- what the phone says it downscaled to
  h       INTEGER,
  species TEXT               -- what the pot said it was when this was taken:
                             -- a pot outlives its plant, and this draws the
                             -- break in the strip without a replant event
);

CREATE INDEX IF NOT EXISTS photos_by_pot ON photos (pot_id, ts);
