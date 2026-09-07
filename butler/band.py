"""The target band, worked out here because no care source carries one.

Plant kind, soil, the two sizes and the month, on our own percentage scale.
It is an offer with a sentence saying why, and never applied without a human.
"""

import math
from typing import NamedTuple

from . import species


# The band is on OUR percentage — air is 0, tap water is 100, a straight line
# between two calibration points. It is not volumetric water content, so no
# published figure could be copied into it even if a source had one. These are
# a starting offer for a human to correct, and nothing here is applied without
# one.

BASE_BAND = (35, 55)
BAND_FLOOR, BAND_CEIL, BAND_MIN_WIDTH = 5, 95, 10

# The closed set the form offers, and the band each kind starts from. A
# dropdown rather than free text because this is the biggest lever of the four:
# an unlabelled plant starts at 35-55 and a succulent at 15-30, so a typo here
# is a 20-point error no pot measurement recovers. Reading stays tolerant — an
# unlisted value matches nothing and falls to the base band — but writing one
# is refused. Insertion order is the dropdown's and the refusal message's:
# driest first, so the list reads as the one axis it is.
PLANT_KINDS = {
    # 5 points is the difference between a barrel cactus and an echeveria.
    "cactus": (10, 25),
    "succulent": (15, 30),
    # An epiphyte in bark is not potted in anything that holds water. The
    # band is low because the medium is, not because the plant likes drought.
    "orchid": (20, 35),
    "mediterranean": (25, 45),  # rosemary, lavender, olive: woody and dry
    "bulb": (30, 50),  # rots wet, and dormant for half the year
    "flower": (35, 50),
    "herb": (35, 55),
    "palm": (40, 55),
    "tropical": (40, 60),  # the leafy houseplants: aroids, marantas, figs
    "vegetable": (45, 65),
    "fern": (55, 75),
    "carnivorous": (70, 90),  # a bog plant: the one kind that wants it wet
}

# The soils that MOVE the band, and the phrase each one contributes to the
# reason. An ordinary potting mix is not here on purpose: it is the
# reference the plant bands are written against, so "not said" and "the bag
# from the shop" are the same answer and the list stays a list of movers.
#
# Every ceiling shift is <= 0, and that is an invariant: the band is only ever
# widened DOWNWARDS (see the squeeze at the end of target_band). A soil that
# raised the ceiling above the plant's own base would offer a wetter top than
# the kind allows, and contradict the reason printed beside it.
SOIL_SHIFTS = {
    "sphagnum": (10, 0, "sphagnum moss"),  # stays wet by design
    "peat": (5, 0, "peat soil"),  # what most nursery pots arrive in
    "clay": (0, -5, "clay soil"),  # holds water: the risk is the top
    "sandy": (-5, -5, "sandy soil"),  # drains before the sensor notices
    "perlite": (-5, -5, "perlite mix"),
    "cactus": (-8, -5, "cactus mix"),  # gritty, and meant to run dry
    "bark": (-10, -5, "bark mix"),  # orchid bark barely holds water at all
}

# Guessing the plant kind from the taxonomy GBIF hands back anyway, so the
# dropdown opens pre-selected. It is a guess and is treated as one: it fills
# the field only while the field is still empty, and one tap changes it.
# Being wrong costs a tap; being silent costs a 20-point band nobody
# noticed. Nothing here is ever written to a pot by the backend.
#
# Genus is asked before family, because family is wrong exactly where it
# matters most: Asparagaceae holds Dracaena fragrans, a leafy thing that
# wants watering, and Dracaena trifasciata, a succulent in all but name.
SPECIES_KINDS = {
    "dracaena trifasciata": "succulent",
    # Lamiaceae would call it a herb; it is a woody Mediterranean shrub and
    # wants a good deal less water than basil.
    "salvia rosmarinus": "mediterranean",
}
GENUS_KINDS = {
    "aloe": "succulent",
    "haworthia": "succulent",
    "gasteria": "succulent",
    "echeveria": "succulent",
    "sedum": "succulent",
    "kalanchoe": "succulent",
    "euphorbia": "succulent",  # the family is half spurges that are not
    "zamioculcas": "succulent",
    "sansevieria": "succulent",  # the name half the world still uses
    "peperomia": "succulent",  # thick leaves; Piperaceae is otherwise vines
    "schlumbergera": "tropical",  # a cactus that lives on a branch, not sand
    "citrus": "mediterranean",
    "lavandula": "mediterranean",
}
# Lowercased, because GBIF's case is not a promise.
FAMILY_KINDS = {
    "cactaceae": "cactus",
    "crassulaceae": "succulent",
    "aizoaceae": "succulent",
    "asphodelaceae": "succulent",
    "didiereaceae": "succulent",
    "polypodiaceae": "fern",
    "dryopteridaceae": "fern",
    "pteridaceae": "fern",
    "nephrolepidaceae": "fern",
    "aspleniaceae": "fern",
    "athyriaceae": "fern",
    "lamiaceae": "herb",  # basil, mint, rosemary, thyme, sage, oregano
    "apiaceae": "herb",  # parsley, coriander, dill
    "solanaceae": "vegetable",
    "cucurbitaceae": "vegetable",
    "brassicaceae": "vegetable",
    "amaranthaceae": "vegetable",
    "araceae": "tropical",  # monstera, philodendron, pothos, peace lily
    "marantaceae": "tropical",
    "arecaceae": "palm",
    "musaceae": "tropical",
    "strelitziaceae": "tropical",
    "bromeliaceae": "tropical",
    "moraceae": "tropical",  # the figs
    "araliaceae": "tropical",
    "asparagaceae": "tropical",
    "asteraceae": "flower",
    "gesneriaceae": "flower",
    "rosaceae": "flower",
    "begoniaceae": "flower",
    "violaceae": "flower",
    "orchidaceae": "orchid",
    "droseraceae": "carnivorous",
    "nepenthaceae": "carnivorous",
    "sarraceniaceae": "carnivorous",
    "cephalotaceae": "carnivorous",
    "amaryllidaceae": "bulb",
    "iridaceae": "bulb",
    "liliaceae": "bulb",
    "oleaceae": "mediterranean",
    "cistaceae": "mediterranean",
    "rutaceae": "mediterranean",  # the citruses
}


def kind_for(accepted: str | None, family: str | None) -> str | None:
    """The plant kind to pre-select for a resolved name, or None.

    None is a real answer and the common one — an unlisted family means
    nobody here knows, and "not sure" already has correct behaviour.
    """
    if not accepted:
        return None
    name = species.normalise_species(accepted)
    if name in SPECIES_KINDS:
        return SPECIES_KINDS[name]
    genus = name.split(" ")[0]
    if genus in GENUS_KINDS:
        return GENUS_KINDS[genus]
    return FAMILY_KINDS.get(species.normalise_species(family or ""))


# What a measurement does to the band. Volume goes as the cube of the
# diameter, but the SHIFT cannot: a 40 cm pot holds 23x the water of a 14 cm
# one, and no band survives being multiplied by 23. What is linear in
# percentage points is the LOG of the volume — each doubling of buffer moves
# the band one step — so the cube arrives as the factor of 3 that log2 turns
# (d/d0)**3 into.
POT_REF_CM = 14.0  # the pot the base bands assume
HEIGHT_REF_RATIO = 1.5  # a 21 cm plant in a 14 cm pot: neither tall nor short
BAND_PER_DOUBLING = 2.5  # percentage points per doubling
# Three doublings of volume is twice the diameter, so a pot of 28 cm or
# more moves the band as far as this model will take it. That is not a claim
# that 28 cm and 60 cm want the same water — it is where a table of a dozen
# plant kinds stops being worth extrapolating, and a bounded wrong answer
# beats an unbounded one.
POT_DOUBLINGS_CAP = 3.0  # +-7.5 points
HEIGHT_DOUBLINGS_CAP = 2.0  # +-5
SIZE_WHY_MIN = 0.5  # under half a point cannot move a whole-point band

# Northern hemisphere, because the flat this waters is in one. A pot in the
# southern half of the world wants these two swapped, and this code has no
# way of being told so — a wrong answer worth naming rather than hiding.
SEASONS = {12: "winter", 1: "winter", 2: "winter", 6: "summer", 7: "summer", 8: "summer"}
SEASON_SHIFTS = {"winter": (-10, -10), "summer": (5, 0)}


class Band(NamedTuple):
    low: int
    high: int
    why: str


def _doublings(ratio: float, cap: float) -> float:
    """log2 of a ratio, capped both ways. The cap is what keeps a fat-fingered
    200 cm pot from proposing a band nobody could water to."""
    return max(-cap, min(cap, math.log2(ratio)))


def size_shifts(
    diameter_cm: float | None, height_cm: float | None
) -> tuple[float, float, list[str]]:
    """What the two measurements do to the band, and the phrases to say so.

    Two independent effects, and both move the FLOOR far more than the
    ceiling. The pot is a water buffer: a small one runs out before anybody
    looks again, so its floor rises; a big one holds water around roots that
    rot, so its ceiling drops. The ceiling only ever drops — no pot size is a
    reason to keep a plant wetter than its kind wants, and raising it would
    err wet.

    The plant is the demand against that buffer, which is why height is read
    OVER diameter rather than on its own: 40 cm of basil is thirsty in a 10 cm
    pot and comfortable in a 30 cm one. A height with no pot to measure
    against falls back to the reference pot, the same assumption the base
    bands make.

    Zero and negative are read as unsaid rather than refused: the write path
    rejects them, and a row that predates it must not make the whole garden
    unreadable through a log of zero.
    """
    low = high = 0.0
    why: list[str] = []
    diameter = diameter_cm if diameter_cm and diameter_cm > 0 else None
    height = height_cm if height_cm and height_cm > 0 else None
    if diameter is not None:
        buffer_ratio = (diameter / POT_REF_CM) ** 3
        shift = -BAND_PER_DOUBLING * _doublings(buffer_ratio, POT_DOUBLINGS_CAP)
        low += shift
        high += min(shift, 0.0)
        if abs(shift) >= SIZE_WHY_MIN:
            why.append(f"{diameter:g} cm pot")
    if height is not None:
        against = diameter if diameter is not None else POT_REF_CM
        demand = (height / against) / HEIGHT_REF_RATIO
        shift = BAND_PER_DOUBLING * _doublings(demand, HEIGHT_DOUBLINGS_CAP)
        low += shift
        if abs(shift) >= SIZE_WHY_MIN:
            why.append(f"{height:g} cm plant")
    return low, high, why


def target_band(
    plant_type: str | None,
    soil: str | None,
    diameter_cm: float | None,
    height_cm: float | None,
    month: int,
) -> Band:
    """A target moisture band to offer, and the reason in words.

    The species reaches this only through the plant kind: a lookup may
    pre-select that dropdown, a human may overrule it, and the band reads
    whatever it ends up saying. No care source reaches here at all, because
    none carries a watering regime. What is left is what is on hand — the
    kind of plant, what it sits in, how big the pot and the plant are, and
    the time of year.
    """
    base = PLANT_KINDS.get(plant_type or "", BASE_BAND)
    # Float from here down: three half-point shifts rounded as they land
    # are three points that vanish one at a time.
    low, high = float(base[0]), float(base[1])
    why = [plant_type if plant_type in PLANT_KINDS else "unlabelled plant"]
    shift = SOIL_SHIFTS.get(soil or "")
    if shift:
        low, high = low + shift[0], high + shift[1]
        why.append(shift[2])
    size_low, size_high, size_why = size_shifts(diameter_cm, height_cm)
    low, high = low + size_low, high + size_high
    why.extend(size_why)
    season = SEASONS.get(month)
    if season in SEASON_SHIFTS:
        shift = SEASON_SHIFTS[season]
        low, high = low + shift[0], high + shift[1]
        why.append(season)
    low, high = round(low), round(high)
    # A band the shifts have squeezed shut is widened DOWNWARDS. Raising the
    # top would offer a wetter ceiling than the plant's own base — a succulent
    # in clay capped at 35% where its unmodified top is 30% — and contradict
    # the reason printed beside it. Lowering the floor errs dry.
    low = min(low, high - BAND_MIN_WIDTH)
    low = max(BAND_FLOOR, min(BAND_CEIL - BAND_MIN_WIDTH, low))
    high = max(low + BAND_MIN_WIDTH, min(BAND_CEIL, high))
    return Band(low, high, ", ".join(why))
