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

Ask whether an address is a butler at all, and whether it accepts a token. This is what the app's
setup screen asks before it stores either, because "nothing is listening there" and "that is not
your token" are different mistakes and only one of them is yours to fix:

```bash
curl -s -H 'X-Token: dev' http://localhost:8000/hello
# -> butler=0.15.0     (401 bad token when the token is wrong; connection
#                       refused when the address is)
```

Keep a photograph of a plant. The bytes are the body — the one route here whose payload is not
`k=v`, because it is not text — and the phone downscales before it uploads; anything that is not a
JPEG, or is over 3 MiB, is refused. The pictures and the listing are the only reads that want the
token: everything else here is numbers about plants, and these are the one thing that could show
the inside of a house.

```bash
curl -s -X POST 'http://localhost:8000/photo?pot=pot-3f9a21&w=1600&h=1200' \
  -H 'X-Token: dev' -H 'Content-Type: image/jpeg' --data-binary @basil.jpg
# -> photo=photo-9c1f0ab2 ts=1757000000
curl -s -H 'X-Token: dev' 'http://localhost:8000/photos?pot=pot-3f9a21'
curl -s -H 'X-Token: dev' http://localhost:8000/photo/photo-9c1f0ab2 > basil.jpg
curl -s -X POST http://localhost:8000/photo/delete \
  -H 'X-Token: dev' --data-binary 'photo=photo-9c1f0ab2'
```

The bytes live under `BUTLER_PHOTOS` (by default `photos/` beside the database, so they share the
bind mount and are backed up or lost together), one directory per pot, named by the photograph's
own id. **The row is the truth.** A picture is listed, served and deleted by its row, and the
directory is never read to decide what exists — so a file no row knows about is invisible and
harmless, and a row whose file has gone is listed as `missing` rather than served as a broken
image. Keeping a photograph writes the file first and the row second; deleting one removes the row
first and the file second. Whichever way a crash or a half-restored backup lands, what is left over
is the harmless direction.

Queue a command for the board's next report, or change its report interval:

```bash
curl -s -X POST http://localhost:8000/command \
  -H 'X-Token: dev' --data-binary 'c=butler1 water=3 ml=50 cap_s=30'
# -> cmd=1        (409 busy: ... while the slot is taken; stop=1 instead of a dose;
#                  leave cap_s out and the rules' own formula sizes it)
curl -s -X POST http://localhost:8000/interval \
  -H 'X-Token: dev' --data-binary 'c=butler1 next=120'
```

Tell it what hangs where. A bare `name=` creates a pot and mints its id; the answer carries
both, and that id is what every later save keys on — the name is only a nickname and is edited
like any other field. A post without an `id=` is *always* a create: if that name is taken it is
refused rather than quietly editing the pot that has it. Whatever fields you feel like setting, in any order (repotting or swapping
a hose is an edit; recalibrating is two numbers):

```bash
curl -s -X POST http://localhost:8000/pot \
  -H 'X-Token: dev' \
  --data-binary 'name=basil controller=butler1 channel=0 outlet=3 plant_type=herb pot_diameter_cm=14'
# -> pot=pot-3f9a21 name=basil
curl -s -X POST http://localhost:8000/pot \
  -H 'X-Token: dev' --data-binary 'id=pot-3f9a21 dry_raw=12000 wet_raw=4000'
curl -s http://localhost:8000/pots     # the garden, with latest raw and derived %
curl -s 'http://localhost:8000/doses?pot=pot-3f9a21&limit=50'
# -> {"doses": [...], "now": ...}: the watering history, newest first — what was asked, what
#    the meter counted, how it ended, the verdict, and the pot it belonged to. Page back with
#    before=<ts>&before_id=<id> from the last row you have.
curl -s 'http://localhost:8000/history?pot=pot-3f9a21&hours=24&bucket_s=300'
# -> {"pot": ..., "since": ..., "to": ..., "points": [{"ts", "raw", "lo", "hi", "n"}, ...]}: the
#    chart's wire, raw counts bucketed on the server's clock; the app derives % from the pot's
#    calibration, so recalibrating re-reads the whole curve. By pot, so a plant wired into a dead
#    one's socket does not inherit its curve
curl -s -X POST http://localhost:8000/pot \
  -H 'X-Token: dev' --data-binary 'id=pot-3f9a21 status=graveyard'
# -> the reversible one: keeps every record, closes the mapping window so the channel and the
#    outlet go back to the garden, and expires any proposal it was waiting on. status=alive
#    brings it back, unwired.
curl -s -X POST http://localhost:8000/pot/delete \
  -H 'X-Token: dev' --data-binary 'id=pot-3f9a21'
# -> ok: the pot, its wiring, its readings, its doses and their verdicts, its dismissed advice
#    and its photographs with their files. No undo.
```

Key a save on the name instead and it goes wrong in one of two ways. While the name is still
taken, `name=basil dry_raw=...` is refused — it is a create, and creating a second `basil` is not
allowed. And once somebody renames the pot, the same line succeeds and does something worse: after
`basil` becomes `genovese` the name is free, so that post makes a *second* pot, wired to nothing,
and calibrates that one. The wiring you send does not live in the pot either — it goes to
`pot_mappings` with a validity window, so moving a hose takes the pot's doses, cooldown and
daily cap with it instead of leaving them for whatever hangs there next.

Once a pot is calibrated, has a target range and a dose, and is flipped to `mode=learning`
or `mode=auto` (a human act, per pot), the rules water it — but only when the board's own
report says the reservoir floats and the manifold knows where it is. Learning mode proposes
instead of watering:

```bash
curl -s http://localhost:8000/pots                # the proposal shows up here, later the dose
curl -s -X POST http://localhost:8000/approve -H 'X-Token: dev' --data-binary 'cmd=17'
curl -s -X POST http://localhost:8000/verdict -H 'X-Token: dev' \
  --data-binary 'cmd=17 verdict=ok'               # or too_much / too_little
```

Each pot in `GET /pots` also carries `last_dose`: the newest dose this POT was handed, with
what the meter counted and the verdict so far. Whose dose a command was is STAMPED on the row
when the command is written, not worked out from the wiring afterwards, so moving a hose takes
the dose history with it and the plant that arrives on that hose inherits nothing. Readings carry
the same stamp. What stays keyed on the hose is what physically belongs to it: the board is handed
an outlet, a proposal is fenced to the pot on that hose now, and both watering floors count what
went down a hose whoever it was attributed to — an attribution that came back empty must still
water less, never more.

## What does this plant want?

Type a name and the butler resolves it before asking anybody about it. GBIF turns the typing into
the accepted binomial — free, no key, and it is what makes an old name find the plant it was
renamed to — and then Trefle answers about that name, if `BUTLER_TREFLE_TOKEN` is set and if it
has ever heard of it:

```bash
curl -s -G http://localhost:8000/species -H 'X-Token: dev' --data-urlencode 'q=Sansevieria trifasciata'
# -> {"matched": "exact", "accepted": "Dracaena trifasciata", "rank": "SPECIES",
#     "kind": "succulent",
#     "care": {"found": true, "light": null, ...},
#     "note": "Trefle knows Dracaena trifasciata but has no numbers for it"}
```

`kind` is the one thing the taxonomy hop gives that a care source could not:
which of `plant_type`'s six kinds to pre-select. It is read from GBIF's family,
which arrives in the same free call, with a genus table checked first because
family is wrong exactly where it matters — Asparagaceae holds both a leafy
Dracaena that wants watering and this one, a succulent in all but name. It is a
guess, so it only ever fills a field that is still empty, and one tap changes
it. Orchids are left unguessed on purpose.

`note` is the sentence to put on screen, because most of the answers are unhappy ones and each is
unhappy in its own way: a genus needs a species, a typo is corrected out loud, and a plant Trefle
has never heard of is the *ordinary* case for houseplants rather than an error. Every one of them
ends the same way — you type the numbers in. Both hops are cached: hits forever, misses for a
month, so the second lookup asks nobody and `GET /pots` never touches the network at all.

A name nobody can place is not the end of it, because GBIF only knows scientific ones. Trefle's own
search takes the typing as a common name, survives a typo, and answers with pictures:

```bash
curl -s -G http://localhost:8000/species -H 'X-Token: dev' --data-urlencode 'q=tomatoe'
# -> {"matched": "none", "accepted": null,
#     "candidates": [{"name": "Solanum lycopersicum", "common": "Tomato", "image": "https://…"},
#                    {"name": "Solanum betaceum", "common": "Tree-tomato", "image": "https://…"}],
#     "note": "not sure which one — pick the plant you recognise"}
```

Ask again with a candidate's name and it resolves exactly. `q=basil` skips even that: among Trefle's
basil thymes and African basils exactly one is called Basil, and one is not a guess — that answers
`"matched": "common"` with the care already filled in. Two are (`q=peace lily` finds a "Peace lily"
and a "Peace-lily"), so those come back as two pictures and a question. A name GBIF *did* place is
never second-guessed with a shortlist: Trefle has never heard of Ficus lyrata, and offering other
figs would be worse than saying so.

No watering number comes from any of this. Trefle carries no watering regime — `soil_humidity` was
NULL for every species probed — so the target band is proposed locally, from the kind of plant, the
soil, the size of the pot, the size of the plant and the month, and it arrives as an offer on each
pot in `GET /pots`:

```bash
# "advice": {"kind": "target", "low": 30, "high": 50,
#            "why": "herb, sandy loam soil, 10 cm pot, 40 cm plant"}
curl -s -X POST http://localhost:8000/pot -H 'X-Token: dev' \
  --data-binary 'id=pot-3f9a21 target_low_pct=30 target_high_pct=50'   # accepting it
curl -s -X POST http://localhost:8000/advice -H 'X-Token: dev' \
  --data-binary 'pot=pot-3f9a21 dismiss=1'                             # refusing it
```

`plant_type` is the base band and the biggest lever: an unlabelled plant starts at 35–55 and a
succulent at 15–30, so it is a closed set — `succulent | fern | herb | vegetable | tropical |
flower` — and anything else is refused rather than saved and quietly ignored, which is what free
text used to do. Reading stays tolerant: a value written before the set existed simply matches
nothing and falls to the base band.

The two sizes are measurements, `pot_diameter_cm` and `plant_height_cm`, and the pot is read as a
water buffer. Volume goes as the cube of the diameter, but the shift cannot: a 40 cm pot holds 23×
a 14 cm one, and no band survives being multiplied by 23. What is linear in percentage points is
the log of that volume — 2.5 points per doubling of buffer — so a 10 cm pot comes out at +4 on the
floor and a 24 cm one at −6 on both ends, which is roughly where the old `small` and `large`
keywords sat. The ceiling only ever drops, because no pot size is a reason to keep a plant wetter
than its kind wants. Height is read over diameter rather than on its own: 40 cm of basil is thirsty
in a 10 cm pot and comfortable in a 30 cm one.

Accepting is an ordinary pot edit, so no number is ever written except by the person writing it.
A refusal is remembered against the numbers that were refused: change the soil, repot, or let the
season turn, and the new band is a new question and is asked again.

Or skip curl and let a fake board do the whole dance — report, receive, water, ack:

```bash
python fake_device.py --token dev --cycles 3
```

## When something is wrong, the phone buzzes

Set `BUTLER_NTFY_TOPIC` (the topic name is the secret: pick something unguessable) and the butler
posts to `https://ntfy.sh/<topic>` — subscribe to it in the ntfy app. The rules, evaluated once a
minute from database state: a controller silent for `max(BUTLER_SILENT_S, 3x its interval)`; a
mapped sensor's channel gone missing while its controller stays healthy; `float=0` (reservoir
empty) or `pos=unknown` (manifold lost) seen twice inside ten minutes — one blip is slosh, a flap
is an empty tank; a safety field that vanished after the board had been sending it; a dose that
was never acked (immediately), came up short on the meter, or did not raise moisture a soak
later; a learning proposal waiting for approval (one nudge per hose per day). A cleared
condition re-raises at most hourly and correlated dose failures page once per controller per
hour — a muted phone is worse than a late alert — and a dose that worked is recorded silently:
this tells you when it's *wrong*.

Set `BUTLER_DEADMAN_URL` (a healthchecks.io ping URL, say) and every clean pass GETs it — a
pass with nothing to send first proves ntfy reachable — so the butler dying and the butler
losing ntfy both stop the pings, and the monitor speaks up through its own channel (give it one
that is not ntfy: it must not share the failure domain). Ten minutes after a start, at most once
a day, one min-priority "butler is up" message probes the topic end to end, because ntfy answers
200 on any topic name and a typo'd topic would otherwise be a permanent, silent blackout.

## Deploy

`docker build` the image (the NAS is x86_64), run it with `/data` bind-mounted to a volume the
NAS backs up and `BUTLER_TOKEN` set from a `deploy.env` that stays out of git. Port 9380 inside
the container. The token and the NAS address never enter this repository.
