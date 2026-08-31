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
