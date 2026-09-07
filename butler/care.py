"""The three-hop species lookup, and the caches in front of it.

GBIF normalises what somebody typed, Trefle answers about the accepted
binomial, and Trefle's own search takes the typing as a common name when GBIF
could place nothing. All three are cached in the database — hits for ever,
misses for a month — so `GET /pots` reads caches only and never the network.

No connection is held across a fetch. The first write in a connection opens
sqlite's write transaction, and holding one for the length of three HTTP
timeouts would make every board report in that window answer "try again":
somebody typing a plant's name must not be able to stop the garden reporting.

No watering number comes back from any of it. Trefle carries no watering
regime at all, so the band is proposed locally — see band.py.
"""

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlencode

from . import band, species, store

Fetcher = Callable[[str], dict | None]


def taxon_for(
    db: Path, get_json: Fetcher, query: str, now: int
) -> species.Taxon | None:
    """The accepted binomial for what somebody typed, cached.

    None means the name service could not be asked — which is not the
    same as "no such plant" and must not be written down as one.

    No database connection is held across the fetch, here or below. The
    first write in a connection opens sqlite's write transaction, and
    holding one for the length of three HTTP timeouts would make every
    board report in that window answer "try again": somebody typing a
    plant's name must not be able to stop the garden reporting.
    """
    with store.connect(db) as con:
        row = con.execute(
            "SELECT accepted, rank, matched, fetched_ts, family "
            "FROM species_names WHERE query = ?",
            (query,),
        ).fetchone()
    # A hit is kept for ever only when it is COMPLETE: a row that resolved
    # a name but carries no family can suggest no plant kind, and a cache
    # hit never re-asks, so without this it would suggest nothing for the
    # life of the database. Re-asking is TTL-gated, so a name that really
    # has no family costs one call a month, not one per screen open.
    fresh = row and now - row[3] < species.CARE_MISS_TTL_S
    complete = row and row[0] is not None and row[4] is not None
    if row and (complete or fresh):
        return species.Taxon(row[0], row[1], row[2], row[4])
    payload = get_json(
        f"{species.GBIF_MATCH_URL}?"
        f"{urlencode({'name': species.binomial_case(query)})}"
    )
    if payload is None:
        # A re-ask that cannot reach GBIF must not turn a name that
        # resolved yesterday into "the lookup is not answering".
        return species.Taxon(row[0], row[1], row[2], row[4]) if row and row[0] else None
    taxon = species.read_gbif(payload)
    with store.connect(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO species_names "
            "(query, fetched_ts, accepted, rank, matched, family) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (query, now, taxon.accepted, taxon.rank, taxon.matched, taxon.family),
        )
    return taxon


def cached_care(con: sqlite3.Connection, key: str) -> dict | None:
    row = con.execute(
        "SELECT fetched_ts, source, found, "
        f"{', '.join(species.CARE_KEYS)} FROM species_care WHERE species = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    entry = {"fetched": row[0], "source": row[1], "found": bool(row[2])}
    entry.update(zip(species.CARE_KEYS, row[3:]))
    return entry


def care_for(
    db: Path, get_json: Fetcher, care_token: str, accepted: str, now: int
) -> dict | None:
    """What the care source says about one binomial, cached.

    A miss is cached too — Trefle's houseplant coverage is empty, not
    thin, so "nothing known" is the ordinary answer and re-asking it on
    every screen open would be the bug. None means it could not be
    asked at all: no token configured, or the source is not answering.
    """
    key = species.normalise_species(accepted)
    with store.connect(db) as con:
        entry = cached_care(con, key)
    if entry and (entry["found"] or now - entry["fetched"] < species.CARE_MISS_TTL_S):
        return entry
    if not care_token:
        return None
    found = get_json(
        f"{species.TREFLE_BASE}/species/search?"
        f"{urlencode({'q': accepted, 'token': care_token})}"
    )
    if found is None:
        return None
    slug = species.pick_species(found, key)
    care = dict.fromkeys(species.CARE_KEYS)
    if slug:
        detail = get_json(
            f"{species.TREFLE_BASE}/species/{quote(slug, safe='')}?"
            f"{urlencode({'token': care_token})}"
        )
        if detail is None:
            return None
        care = species.read_trefle(detail)
    with store.connect(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO species_care "
            f"(species, fetched_ts, source, found, {', '.join(species.CARE_KEYS)}) "
            f"VALUES (?, ?, 'trefle', ?, {', '.join('?' * len(species.CARE_KEYS))})",
            (key, now, int(bool(slug)), *(care[k] for k in species.CARE_KEYS)),
        )
    return {"fetched": now, "source": "trefle", "found": bool(slug), **care}


def search_for(
    db: Path, get_json: Fetcher, care_token: str, query: str, now: int
) -> list[dict]:
    """Trefle's own search on what was typed, cached.

    This is the fuzzy half. GBIF only knows scientific names, so "basil",
    "basilico" and "tomatoe" resolve to nothing there; Trefle's search
    matches common names, survives a typo, and its rows already carry a
    picture — which is what lets somebody confirm by eye rather than by
    spelling. An empty list is both "nothing found" and "could not ask":
    the screen is the same either way, a list with nothing in it.
    """
    with store.connect(db) as con:
        row = con.execute(
            "SELECT fetched_ts, candidates FROM species_search WHERE query = ?",
            (query,),
        ).fetchone()
    if row:
        cached = json.loads(row[1])
        if cached or now - row[0] < species.CARE_MISS_TTL_S:
            return cached
    if not care_token:
        return []
    payload = get_json(
        f"{species.TREFLE_BASE}/species/search?"
        f"{urlencode({'q': query, 'token': care_token})}"
    )
    if payload is None:
        return []
    candidates = species.read_candidates(payload)
    with store.connect(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO species_search "
            "(query, fetched_ts, candidates) VALUES (?, ?, ?)",
            (query, now, json.dumps(candidates)),
        )
    return candidates


def care_note(accepted: str, matched: str, care: dict) -> str:
    if care["light"] is None and care["humidity"] is None:
        note = f"Trefle knows {accepted} but has no numbers for it"
    else:
        note = f"Trefle: {accepted}"
    return f"read as {accepted}. {note}" if matched == "fuzzy" else note


def miss_note(answer: dict) -> str:
    if answer["candidates"]:
        return "not sure which one — pick the plant you recognise"
    if answer["matched"] == "unavailable":
        return "the lookup is not answering — type the numbers in"
    if answer["matched"] == "genus":
        return "that is a genus — which species?"
    if answer["accepted"] is None:
        return "no plant of that name — check the spelling, or type the numbers in"
    if answer["care"] is None:
        return "no care source configured or answering — type the numbers in"
    return f"{answer['accepted']} is not in Trefle — type the numbers in"


def look_up(
    db: Path, get_json: Fetcher, care_token: str, query: str, depth: int = 0
) -> dict:
    """One species lookup, and a sentence saying what came of it.

    Three ways in, in order of how much they can be trusted. GBIF on the
    typing resolves a scientific name, corrects a typo in one, and
    redirects a synonym to the name the plant was renamed to. Failing
    that, Trefle's search takes the typing as a common name and answers
    with pictures. And if exactly one of those pictures is called what
    was typed, that is not a guess and is followed.

    Every unhappy path ends in a working screen: the numbers are typed
    in, which is what happens for most houseplants anyway.
    """
    now = int(time.time())
    taxon = taxon_for(db, get_json, query, now)
    answer = {
        "query": query,
        "matched": "unavailable",
        "accepted": None,
        "rank": None,
        "kind": None,
        "care": None,
        "candidates": [],
        "note": "",
    }
    if taxon is not None:
        answer["matched"] = taxon.matched
        answer["accepted"] = taxon.accepted
        answer["rank"] = taxon.rank
        answer["kind"] = band.kind_for(taxon.accepted, taxon.family)
        if taxon.accepted:
            answer["care"] = care_for(db, get_json, care_token, taxon.accepted, now)
            care = answer["care"]
            if care is not None and care["found"]:
                answer["note"] = care_note(taxon.accepted, taxon.matched, care)
                return answer
            # A name GBIF resolved and Trefle has never heard of is a
            # finished answer, not a reason to go offering other plants:
            # the shortlist is for a name nobody could place at all.
            answer["note"] = miss_note(answer)
            return answer
    candidates = search_for(db, get_json, care_token, query, now)
    pick = species.sole_match(candidates, query)
    if pick and depth == 0 and species.normalise_species(pick) != query:
        deeper = look_up(
            db, get_json, care_token, species.normalise_species(pick), depth + 1
        )
        if deeper["accepted"]:
            deeper["query"] = query
            deeper["matched"] = "common"
            return deeper
    answer["candidates"] = candidates
    answer["note"] = miss_note(answer)
    return answer
