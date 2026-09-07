"""What the two outside plant services say, and how little of it is a number.

Reading only: the fetching and the caching belong to create_app, which owns
the database and the timeouts. What lives here is the shape of the answers,
which is the part that has to survive a service changing its mind.
"""

import http.client
import json
import urllib.request
from typing import NamedTuple

from . import notify


# --- The care lookup -----------------------------------------------------
#
# Two hops, both cached, and neither of them a source of watering numbers.
# GBIF turns whatever somebody typed into the accepted binomial: free, no
# key, and it is what lets "Sansevieria trifasciata" find the plant now
# filed under "Dracaena trifasciata". Trefle then answers about that
# binomial, when it knows it at all.
#
# The target band comes from target_band() below and from nowhere else. The
# care source carries no watering regime — `soil_humidity` is NULL for every
# species, houseplants included — and what it does carry, light and
# atmospheric humidity on its own 0-10 scales, is context for a human rather
# than an input to a number.

GBIF_MATCH_URL = "https://api.gbif.org/v1/species/match"
TREFLE_BASE = "https://trefle.io/api/v1"
CARE_TIMEOUT_S = 6  # three hops worst case, so a lookup answers inside ~20 s
CARE_MISS_TTL_S = 30 * 86400  # a complete hit is kept forever; anything else
#                               is re-asked monthly (see taxon_for)
SPECIES_MAX = 120  # longer than any binomial; bounds what goes on the wire
CARE_BODY_CAP = 1 << 20  # a species page is ~40 KB; never read a stream


def fetch_json(url: str) -> dict | None:
    """One GET, parsed as JSON. None for everything else.

    A care source that is down, slow, rate-limiting, redirecting or
    answering HTML is a normal Tuesday, and the caller's answer is the same
    in every one of those cases: nothing is known about this plant. Never
    raises — a lookup must not be able to take the service down.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": "plantbutler-backend"}
    )
    try:
        with notify._OPENER.open(request, timeout=CARE_TIMEOUT_S) as answer:
            if not 200 <= answer.status < 300:
                return None
            body = answer.read(CARE_BODY_CAP)
        parsed = json.loads(body.decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalise_species(text: str) -> str:
    """What both caches are keyed on: underscores back to spaces (a k=v
    token cannot hold one), collapsed whitespace, lowercased."""
    return " ".join(text.replace("_", " ").split()).lower()


def binomial_case(query: str) -> str:
    """What GBIF is asked, which is not what the cache is keyed on.

    GBIF matches a lowercase binomial happily but a lowercase genus not at
    all: `monstera` answers NONE while `Monstera` answers GENUS. So the key
    stays lowercase, and the question goes out in botanical case — genus
    capitalised, epithet not.
    """
    return query[:1].upper() + query[1:]


class Taxon(NamedTuple):
    accepted: str | None  # the binomial to ask about; None when unresolved
    rank: str | None
    matched: str  # exact | fuzzy | genus | none
    family: str | None = None  # what the plant-kind guess is read from


def read_gbif(payload: dict) -> Taxon:
    """GBIF's match answer, read defensively.

    Two traps. A `matchType` of NONE arrives with `confidence: 100`, so
    confidence says nothing on its own and the match type is the only field
    worth believing. And a name that resolves to a genus has no `species`
    at all — inventing one from it would mint exactly the junk cache row the
    taxonomy hop exists to prevent, so a genus resolves to nothing and says
    so, for the screen to ask which species.
    """
    kind = str(payload.get("matchType") or "NONE").upper()
    if kind not in ("EXACT", "FUZZY"):
        return Taxon(None, None, "none")
    if str(payload.get("kingdom") or "") != "Plantae":
        # A plant name that matches an animal is a wrong hop, not a hit.
        return Taxon(None, None, "none")
    rank = str(payload.get("rank") or "").upper() or None
    # `species` is the ACCEPTED name even when the typing was a synonym,
    # which is the entire reason this hop is here.
    accepted = payload.get("species")
    if not isinstance(accepted, str) or not accepted.strip():
        return Taxon(None, rank, "genus" if rank == "GENUS" else "none")
    family = payload.get("family")
    return Taxon(
        accepted.strip(),
        rank,
        "fuzzy" if kind == "FUZZY" else "exact",
        family.strip() if isinstance(family, str) and family.strip() else None,
    )


def pick_species(payload: dict, wanted: str) -> str | None:
    """The slug for `wanted` in a Trefle search answer, or None.

    Trefle's search is fuzzy and ranks by its own relevance: asking for
    Ocimum basilicum also returns Basilicum polystachyon, and a query it
    knows nothing about still returns whatever was nearest. Only an exact
    binomial is this plant; anything else is a different one.
    """
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        if normalise_species(str(row.get("scientific_name") or "")) == wanted:
            slug = str(row.get("slug") or "").strip()
            return slug or None
    return None


def _scale(value: object, low: float, high: float) -> float | None:
    """A number inside [low, high], or None. Booleans are not numbers here
    (True would otherwise read as a light level of 1)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if low <= value <= high else None


CARE_KEYS = (
    "common_name",
    "light",
    "humidity",
    "ph_min",
    "ph_max",
    "temp_min_c",
    "image_url",
)


def read_trefle(payload: dict) -> dict:
    """The handful of fields worth keeping from a Trefle species page.

    Everything is optional and most of it is usually absent — that is the
    normal case, not a failure. `image_url` is only kept when it is https:
    the app loads it, and a plaintext or javascript: URL from a third party
    has no business being handed to a WebView.
    """
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    growth = data.get("growth")
    growth = growth if isinstance(growth, dict) else {}
    temp = growth.get("minimum_temperature")
    temp = temp if isinstance(temp, dict) else {}
    image = str(data.get("image_url") or "")
    light = _scale(growth.get("light"), 0, 10)
    humidity = _scale(growth.get("atmospheric_humidity"), 0, 10)
    common = str(data.get("common_name") or "").strip()
    return {
        "common_name": common or None,
        "light": None if light is None else int(light),
        "humidity": None if humidity is None else int(humidity),
        "ph_min": _scale(growth.get("ph_minimum"), 0, 14),
        "ph_max": _scale(growth.get("ph_maximum"), 0, 14),
        "temp_min_c": _scale(temp.get("deg_c"), -90, 60),
        "image_url": image if image.startswith("https://") else None,
    }


CANDIDATES_MAX = 8  # a screenful of pictures; the rest are worse matches
CANDIDATE_KEYS = ("name", "common", "image", "slug")


def loose(text: str) -> str:
    """Looser than the cache key: hyphens are spaces too. Trefle spells the
    same plant "Peace lily" and "Peace-lily" in adjacent rows, and neither
    spelling is what anybody types."""
    return normalise_species(text.replace("-", " "))


def read_candidates(payload: dict) -> list[dict]:
    """A Trefle search answer as a shortlist to show somebody.

    Species only: Trefle returns varieties and subspecies alongside, and
    "Solanum lycopersicum var. lycopersicum" is a worse answer to "tomato"
    than the species is. The picture comes from the search itself, so a
    shortlist of eight costs one HTTP call rather than nine.
    """
    out = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("rank") or "species") != "species":
            continue
        name = str(row.get("scientific_name") or "").strip()
        slug = str(row.get("slug") or "").strip()
        if not name or not slug:
            continue
        image = str(row.get("image_url") or "")
        common = str(row.get("common_name") or "").strip()
        out.append(
            {
                "name": name,
                "common": common or None,
                # The phone loads this. A plaintext or javascript: URL from
                # a third party has no business being handed to it.
                "image": image if image.startswith("https://") else None,
                "slug": slug,
            }
        )
        if len(out) == CANDIDATES_MAX:
            break
    return out


def sole_match(candidates: list[dict], query: str) -> str | None:
    """The one candidate whose common name IS what was typed, or None.

    "basil" has exactly one Basil among its Basil thymes and African basils,
    so that is not a guess and can be followed. "peace lily" has two, spelt
    "Peace lily" and "Peace-lily", and picking either would be inventing an
    answer — two pictures and a question is the honest response there.
    """
    wanted = loose(query)
    hits = {c["name"] for c in candidates if c["common"] and loose(c["common"]) == wanted}
    return hits.pop() if len(hits) == 1 else None
