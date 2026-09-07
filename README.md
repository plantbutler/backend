# Plant Butler backend

The service in the middle: one Python container with a SQLite database, running on a home
network server. The board posts its readings here, this decides when to water and hands one
command back, and the phone app reads and edits everything through it. How the three parts fit
together is in the [umbrella README](https://github.com/plantbutler/plantbutler#readme); the
words are in its [glossary](https://github.com/plantbutler/plantbutler/blob/main/GLOSSARY.md).

Every watering gate refuses rather than waters. When a reading is missing, a tank is unproven or
a board has stopped itself, nothing is queued.

## Run it

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                                  # the tests
BUTLER_TOKEN=dev BUTLER_DB=./butler.db \
  uv run uvicorn butler:create_app --factory   # serves on :8000
```

Talk to it the way the board does, and the way the app does:

```bash
curl -s -X POST localhost:8000/report -H 'X-Token: dev' --data-binary 'c=0 ch0=8123 ch1=7902'
# -> next=60
curl -s localhost:8000/hello -H 'X-Token: dev'     # -> butler=<version>
curl -s localhost:8000/health                       # no token: is it alive, what is wrong
```

`fake_device.py` is a board that never existed: it reports on a loop and obeys the commands it
gets back. It is the quickest way to see the whole thing work.

```bash
uv run python fake_device.py --url http://localhost:8000 --token dev
```

## Configure

Every setting is an environment variable. Only the first has no usable default.

| variable | default | what it is |
| --- | --- | --- |
| `BUTLER_TOKEN` | none, and empty is refused | the shared secret the board and the app send |
| `BUTLER_DB` | `/data/butler.db` | the database file; its directory must be a real mount |
| `BUTLER_PHOTOS` | `photos/` beside the database | where photograph files live |
| `BUTLER_NEXT_S` | `60` | seconds between reports, told to the board in every answer |
| `BUTLER_CMD_TTL_S` | `900` | how long a queued command waits before it expires |
| `BUTLER_SILENT_S` | `600` | how long a board may say nothing before it is called silent |
| `BUTLER_QUIET` | `22-08` | hours during which the rules never water |
| `BUTLER_NTFY_TOPIC` | unset, no alerts sent | the push topic; the topic name is the secret |
| `BUTLER_NTFY_URL` | `https://ntfy.sh` | the push service |
| `BUTLER_DEADMAN_URL` | unset | pinged only after a fully clean alert pass |
| `BUTLER_TREFLE_TOKEN` | unset, care numbers typed in | the plant care service |

## Deploy

The image is built on the server, which is x86_64, and the token never enters the image.

```bash
docker build --platform=linux/amd64 -t plantbutler-backend:<version> .
docker run -d --name plantbutler --restart unless-stopped \
  -p 9380:9380 -v /path/on/the/nas/data:/data --env-file deploy.env \
  plantbutler-backend:<version>
```

`deploy.env` holds `BUTLER_TOKEN` and the rest. It stays out of git, and so does the server's
address. Nothing here is ever exposed to the internet.

## What it answers

Every route takes the token in an `X-Token` header, except `/health`. What you send is always
`key=value` pairs rather than JSON, because the board writes them by hand. What comes back is
JSON on the list and history routes, `key=value` elsewhere, and bytes for a photograph.

| route | what it does |
| --- | --- |
| `POST /report` | the board's readings land; the answer carries the next interval and at most one command |
| `POST /command` | queue one watering by hand |
| `POST /interval`, `POST /controller` | change how often a board reports; retire or restore a board |
| `POST /resume`, `POST /refill` | clear a stopped board; record that the tank was filled |
| `GET /pots`, `POST /pot`, `POST /pot/delete` | the garden; create or edit one plant; erase one and everything of it |
| `POST /approve`, `POST /verdict` | say yes to a proposed watering; say how it turned out |
| `GET /history`, `GET /doses` | one plant's moisture over time; the watering log |
| `GET /species`, `POST /advice` | look a plant up by name; ask for a target moisture range |
| `POST /photo`, `GET /photos`, `GET /photo/<id>`, `POST /photo/delete` | pictures of a plant |
| `GET /hello` | is this a butler, and is this token good |
| `GET /health` | no token needed: is it alive, and what is wrong right now |

## Files

The service is the package `butler/`. Its `__init__.py` is a facade: it builds the app and
re-exports every public name, so a caller can keep saying `from butler import X`.

| file | what it holds |
| --- | --- |
| `butler/__init__.py` | builds the app and re-exports the rest |
| `butler/config.py` | every setting, read once, and the refusals to start |
| `butler/wire.py` | every `key=value` parser and the two shapes on the wire |
| `butler/schema.py`, `butler/schema.sql` | the tables, the ids, the added columns, the one rebuild |
| `butler/commands.py` | a report arriving, the queue, approve, verdict, the knobs |
| `butler/rules.py` | the ladder that decides a watering, gate by gate |
| `butler/alerts.py` | the eight alert rules and the periodic pass that runs them |
| `butler/tank.py`, `butler/pots.py` | the latches, refills, float and tank size; a pot's status and wiring |
| `butler/garden.py`, `butler/store.py` | creating, editing, burying and erasing a pot; the photograph files |
| `butler/care.py`, `butler/species.py`, `butler/band.py` | the species lookup, its two sources, and the target range offered locally |
| `butler/notify.py`, `butler/constants.py` | the push service and the dead-man ping; the numbers |
| `butler/routes/` | one router per subject, and the preamble twelve write routes share |
| `fake_device.py` | a board that never existed, for driving the service without hardware |
| `tests/` | one file per subject, named for it; `conftest.py` holds the fixtures they share |
| `Dockerfile` | the image; the database is a bind mount, never a volume |

Where to go: a new route is a handler in the router for its subject plus a parser in `wire.py`;
a new column goes in **both** `schema.py`'s table text and its added-columns list, or it silently
will not exist on an older database; the watering rule is one function in `rules.py`; a new alert
is one more rule function in `alerts.py`.
