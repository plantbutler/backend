-- The whole database. Additive only: new tables and indexes arrive as new
-- CREATE IF NOT EXISTS lines with the pitch that needs them (no migrations
-- framework, per the plan's no-gos). Raw counts are kept forever; percentages
-- are derived at read time and never stored.

CREATE TABLE IF NOT EXISTS readings (
  ts         INTEGER NOT NULL,  -- server arrival time, unix seconds
  controller TEXT    NOT NULL,  -- c= in the report
  channel    INTEGER NOT NULL,  -- chN key
  raw        INTEGER NOT NULL,  -- the count, uninterpreted
  t          INTEGER            -- board uptime ms (t=), NULL when not sent.
                                -- Deliberately NOT unique: uptime restarts on
                                -- reboot, so values recur; retry dedup is a
                                -- time-windowed check in the app instead.
);

CREATE INDEX IF NOT EXISTS readings_by_channel
  ON readings (controller, channel, ts);

CREATE INDEX IF NOT EXISTS readings_by_uptime
  ON readings (controller, t, ts);

-- Command hand-off (cycle 1 stretch). One slot per controller, not a job
-- system: queued -> sent (handed once, in a report response) -> acked, or
-- expired (no ack on the next report, or nobody collected it in time).
-- The command log doubles as the watering history, so rows are never deleted.

CREATE TABLE IF NOT EXISTS commands (
  id         INTEGER PRIMARY KEY,
  created_ts INTEGER NOT NULL,
  controller TEXT    NOT NULL,
  kind       TEXT    NOT NULL,  -- 'water' | 'stop'
  outlet     INTEGER,           -- water only: flat outlet index
  ml         INTEGER,           -- water only: dose
  cap_s      INTEGER,           -- water only: hard seconds cap
  state      TEXT    NOT NULL,  -- 'queued' -> 'sent' -> 'acked' | 'expired'
  source     TEXT    NOT NULL,  -- who queued it ('manual' until rules exist)
  sent_ts    INTEGER,
  acked_ts   INTEGER,
  flow_ml    INTEGER            -- what the board says actually flowed
);

CREATE INDEX IF NOT EXISTS commands_open
  ON commands (controller, state);

-- One row per controller that has ever reported or been configured:
-- heartbeat (last_seen) and the per-controller report interval override
-- (next_s, NULL means the BUTLER_NEXT_S default).

CREATE TABLE IF NOT EXISTS controllers (
  controller TEXT PRIMARY KEY,
  last_seen  INTEGER NOT NULL,  -- 0 = configured but never heard from
  next_s     INTEGER
);

-- Pots, plants and calibration (cycle 2). One row per pot: the channel ->
-- outlet -> pot -> plant mapping, the two calibration numbers, the
-- Planta-style descriptive fields, and the knobs the watering rules will
-- read later. One table until that hurts. Percentages are derived at read
-- time from (dry_raw, wet_raw) and never stored, so recalibrating
-- reinterprets history instead of losing it.

CREATE TABLE IF NOT EXISTS pots (
  id              INTEGER PRIMARY KEY,
  name            TEXT    NOT NULL UNIQUE,
  controller      TEXT,     -- where its sensor reports from
  channel         INTEGER,  -- chN in that controller's reports
  outlet          INTEGER,  -- flat outlet index its hose hangs on
  plant_type      TEXT,     -- descriptive, Planta-style
  plant_size      TEXT,
  pot_size        TEXT,
  soil            TEXT,
  dry_raw         INTEGER,  -- calibration: raw count bone dry
  wet_raw         INTEGER,  -- calibration: raw count soaked
  target_low_pct  INTEGER,  -- the ideal moisture range
  target_high_pct INTEGER,
  dose_ml         INTEGER,  -- for the rules pitch
  mode            TEXT    NOT NULL DEFAULT 'manual',  -- manual|learning|auto
  cooldown_h      INTEGER,
  daily_cap_ml    INTEGER,
  enabled         INTEGER NOT NULL DEFAULT 1
);

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
  controller     TEXT PRIMARY KEY,
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
  pos_bad_prev   INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
  key        TEXT PRIMARY KEY,  -- silent:<c> | sensor:<c>:<ch> | float:<c> |
                                -- pos:<c> | fields:<kind>:<c> | dose:<id> |
                                -- dosefail:<c> | proposal:<c>:<outlet>, plus
                                -- meta:tick / meta:up bookkeeping rows
  raised_ts  INTEGER NOT NULL,
  cleared_ts INTEGER,           -- NULL while the condition stands
  detail     TEXT               -- dose judgements: 'ok'|'failed'|'unverified'
);
