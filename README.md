# plantbutler / backend

The Plant Butler backend: one Python container with SQLite on a bind-mounted volume, running in
Docker on the Synology NAS, LAN-only. It stores the raw readings the board reports, knows which
channel is which outlet, pot and plant, hands one water command at a time to the board, and — later —
decides when to water and says when something is wrong.

What it does and in which order is in the [plan](https://github.com/plantbutler/plan); the
decisions it is built on are in the
[umbrella](https://github.com/plantbutler/plantbutler/blob/main/DECISIONS.md); the design sketch
and the working notes are in [AGENTS.md](AGENTS.md).

## Run it

```bash
uv sync && uv run pytest                      # the tests
BUTLER_TOKEN=dev BUTLER_DB=./butler.db \
  uv run uvicorn butler:create_app --factory  # serve on :8000 locally
```

Talk to it the way the board does:

```bash
curl -s -X POST http://localhost:8000/report \
  -H 'X-Token: dev' \
  --data-binary 'c=butler1 ch0=8123 ch1=7902'
# -> next=60
curl -s http://localhost:8000/health
```

Queue a command for the board's next report, or change its report interval:

```bash
curl -s -X POST http://localhost:8000/command \
  -H 'X-Token: dev' --data-binary 'c=butler1 water=3 ml=50 cap_s=30'
# -> cmd=1        (409 busy: ... while the slot is taken; stop=1 instead of a dose)
curl -s -X POST http://localhost:8000/interval \
  -H 'X-Token: dev' --data-binary 'c=butler1 next=120'
```

Tell it what hangs where — a pot is a name plus whatever fields you feel like setting
(repotting or swapping a hose is an edit; recalibrating is two numbers):

```bash
curl -s -X POST http://localhost:8000/pot \
  -H 'X-Token: dev' \
  --data-binary 'name=basil controller=butler1 channel=0 outlet=3 plant_type=basil pot_size=14cm'
curl -s -X POST http://localhost:8000/pot \
  -H 'X-Token: dev' --data-binary 'name=basil dry_raw=12000 wet_raw=4000'
curl -s http://localhost:8000/pots     # the garden, with latest raw and derived %
```

Once a pot is calibrated, has a target range and a dose, and is flipped to `mode=learning`
or `mode=auto` (a human act, per pot), the rules water it — but only when the board's own
report says the reservoir floats and the manifold knows where it is. Learning mode proposes
instead of watering:

```bash
curl -s http://localhost:8000/pots                # the proposal shows up here
curl -s -X POST http://localhost:8000/approve -H 'X-Token: dev' --data-binary 'cmd=17'
curl -s -X POST http://localhost:8000/verdict -H 'X-Token: dev' \
  --data-binary 'cmd=17 verdict=ok'               # or too_much / too_little
```

Or skip curl and let a fake board do the whole dance — report, receive, water, ack:

```bash
python fake_device.py --token dev --cycles 3
```

## When something is wrong, the phone buzzes

Set `BUTLER_NTFY_TOPIC` (the topic name is the secret: pick something unguessable) and the butler
posts to `https://ntfy.sh/<topic>` — subscribe to it in the ntfy app. The rules, evaluated once a
minute from database state: a controller silent for `max(BUTLER_SILENT_S, 3x its interval)`;
`float=0` (reservoir empty) or `pos=unknown` (manifold lost) persisting for a few minutes; a dose
that was never acked, came up short on the meter, or did not raise moisture a soak later; a
learning proposal waiting for approval (one nudge per hose per day). Raises and clears are
debounced and a cleared condition re-raises at most hourly — a muted phone is worse than a late
alert — and a dose that worked is recorded silently: this tells you when it's *wrong*.

Set `BUTLER_DEADMAN_URL` (a healthchecks.io ping URL, say) and every clean pass GETs it, so the
butler dying — or failing to reach ntfy — stops the pings and the monitor speaks up through its
own channel (give it one that is not ntfy: it must not share the failure domain). Ten minutes
after every start, one min-priority "butler is up" message probes the topic end to end, because
ntfy answers 200 on any topic name and a typo'd topic would otherwise be a permanent, silent
blackout.

## Deploy

`docker build` the image (the NAS is x86_64), run it with `/data` bind-mounted to a volume the
NAS backs up and `BUTLER_TOKEN` set from a `deploy.env` that stays out of git. Port 9380 inside
the container. The token and the NAS address never enter this repository.
