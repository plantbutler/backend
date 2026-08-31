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

## Deploy

`docker build` the image (the NAS is x86_64), run it with `/data` bind-mounted to a volume the
NAS backs up and `BUTLER_TOKEN` set from a `deploy.env` that stays out of git. Port 9380 inside
the container. The token and the NAS address never enter this repository.
