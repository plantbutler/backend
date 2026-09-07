# Working on the backend

[README.md](README.md) says what this is, how to run it, how to configure it and what it answers.
The umbrella's [AGENTS.md](https://github.com/plantbutler/plantbutler/blob/main/AGENTS.md) holds
the project-wide rules; its
[DECISIONS.md](https://github.com/plantbutler/plantbutler/blob/main/DECISIONS.md) entries 4 (the
wire), 5 (what this decides and the firmware does not) and 7 (safety) are what this code keeps.

## Rules

- **The failure direction is dry.** Every watering gate refuses rather than waters: a missing
  reading, an unproven tank, a board that stopped itself, a command already in flight. A change
  that makes a gate water on doubt is wrong however well it reads.
- **A column exists only if it is in both places.** `schema.sql` creates it for a new database;
  the added-columns table adds it to an old one. `CREATE TABLE IF NOT EXISTS` does not retype an
  existing column, so a changed type means a rebuild, not an edit.
- **A malformed report is refused whole.** Half a report stored looks exactly like a working
  system with dead sensors. Unknown keys are ignored on purpose: the board grows keys before this
  service learns to read them.
- **A command is handed over once.** Queued, handed to the board in one answer, acknowledged in
  the next report. A report that does not acknowledge it expires it. Re-handing a watering
  command the board may already have run is how a plant drowns.
- **The tests are the only guard.** `uv run pytest` before every commit. The fixtures live in
  `tests/conftest.py`; a file that needs the app built differently redefines the settings, and
  the app and client follow.

## The shape

The service is the package `butler/`, and `__init__.py` is its facade: it builds the app and
re-exports every public name, so `from butler import X` and reading `butler.X` mean what they
always did. Inside the package a module imports other **modules**, not names, so a module's
function has one home that every caller resolves at call time; the three places that import a
name say why in their file header. Keep the import graph acyclic: when two modules need the same
thing, move it down a layer rather than importing sideways.

**Patch the module that owns the function, not the facade.** `monkeypatch.setattr(butler, "X")`
rebinds only the facade's own name, which nothing outside `__init__.py` reads, so the caller
carries on with the original. `monkeypatch.setattr(butler.wire, "X")` is what a caller inside the
package sees. This is why the split moved six patch targets in the tests and changed nothing
else.

## Traps

- A string literal in this package is part of the wire or the alert keys. Changing one changes
  behaviour, whatever the diff looks like.
- Timestamps are stamped on arrival: the board has no clock worth trusting. The board's own
  uptime is what makes a retried report recognisable as a repeat.
- Board 0 is a real board. Never test a controller for truth, only for being unset.
- Alerts are raised and cleared by a ticker, not by a request, because a silent board cannot be
  noticed on arrival. Some keys page once and never clear.
- Photograph bytes are files; the row is the truth. An id is claimed before it is written so a
  collision cannot overwrite a picture that exists.

## Deploying

The image is built on the server and the token comes from `deploy.env` there. Backend deployment
is Jacopo's standing permission; anything else on that machine is asked first. The server's
address and the token never enter this repository.

## Comments

- A file starts with one line saying what it holds.
- A comment says why, not what. Keep units, invariants, security reasons, wire facts and traps.
- No dates, version numbers, decision numbers, pitch names, reviewer names, issue or pull request
  numbers, or history. Keep the fact, drop the provenance.
