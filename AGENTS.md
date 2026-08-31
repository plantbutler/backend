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
Home Assistant, weather, ML, a species database.

## Design sketch (brainstormed 2026-08-31)

Wire, board → backend once a minute (POST, token, plain text `k=v`):

```
c=butler1  t=<uptime_ms>  float=1  pos=ok|unknown  last=ok|fault:...
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

Tables (`schema.sql`): `readings(ts, controller, channel, raw)`; `pots(id, name, controller,
channel, outlet, plant_type, plant_size, pot_size, soil, dry_raw, wet_raw, target_low_pct,
target_high_pct, dose_ml, mode, cooldown_h, daily_cap_ml, enabled)` (`mode`:
manual | learning | auto) — mapping, calibration, thresholds and the descriptive
fields (Planta-style: what is potted, how big, in what) in one table until that hurts; `commands(id, created_ts,
outlet, ml, cap_s, state proposed→queued→sent→acked/expired/failed, source, result, verdict)` — the command log is
the watering history; `events(ts, kind, detail)`. Percentages are derived at read time, never
stored. Air temperature and light ride the same readings table as extra channels (the sensor kit
has both modules); season is derived from the date. Adaptive dosing from range, temperature,
light and season is a later pitch — see the plan's Planta note — v1 rules stay thresholds.

Rules run in-process on each report arrival, no cron: median over a window → N consecutive dry →
cooldown → daily cap → quiet hours → heartbeat fresh and float ok and position ok → enqueue — directly in auto; in learning, a
proposal to approve and verdict at the pot. The flip to auto is a human act, per pot.

## When you start

Stack (chosen 2026-08-31): a `uv` project, FastAPI + uvicorn, standard-library `sqlite3`, one
`schema.sql`. Ask Jacopo for NAS
access before the first pitch — Container Manager in the DSM UI or SSH. The token and the NAS
address never enter the repository.
