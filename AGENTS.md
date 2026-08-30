# Working on the backend

Not started (2026-08-30). Read the umbrella's
[AGENTS.md](https://github.com/plantbutler/plantbutler/blob/main/AGENTS.md) and
[DECISIONS.md](https://github.com/plantbutler/plantbutler/blob/main/DECISIONS.md) first; decisions
#2, #4, #5 and #6 are the ones this repository implements.

## What it is going to be

One Python container with SQLite on a bind-mounted volume, in Docker on the Synology NAS,
reachable on the LAN only. One static token. Server timestamps on arrival. Raw readings kept
forever; percentages derived at read time from two calibration numbers per channel, never stored.

The board talks first: one plain-HTTP round trip per report interval, `k=v` lines each way. The
response carries the next interval and at most one pending command (`water valve V for S s`, or
`stop`), one-shot and expiring, acknowledged by the report after the one that carried it. No
MQTT, no JSON, no server on the board.

The backend decides when to water (thresholds, smoothing, per-pot cooldown, daily cap, quiet
hours) and never enqueues on a stale heartbeat or an empty reservoir. NAS or WiFi down means no
watering.

## Pitches, in order (titles in the plan)

1. **Readings land on the NAS** (cycle 1, Claude, Jacopo reviews) — the container, the readings
   endpoint, the database. Done when `curl` from the LAN returns 200 and the row is there. Build
   on the laptop, ship the image; the unknown is Container Manager (image arch, volume
   permissions, port), not the code.
2. **Command hand-off** (cycle 1, stretch) — the command slot, heartbeat/last-seen, the
   next-interval knob, and a **fake-device script** so everything after this is testable without
   hardware.
3. **Pots, plants and calibration** — channel → valve → pot → plant, two numbers per channel.
4. **Rules that water**, then **Tell me when it's wrong** (a public ntfy.sh topic).

Not in scope for any of them: Postgres, a migrations framework, TLS, a reverse proxy, Grafana,
Home Assistant, weather, ML, a species database.

## When you start

Pick the smallest stack that runs in one container (a `uv` project, one web framework, the
standard-library `sqlite3` is fine) and write the choice into this file. Ask Jacopo for NAS
access before the first pitch — Container Manager in the DSM UI or SSH. The token and the NAS
address never enter the repository.
