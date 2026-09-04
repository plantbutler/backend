"""What does this plant want: the taxonomy hop, the care source, and the
band the two of them deliberately do not decide."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from butler import (
    BASE_BAND,
    PLANT_KINDS,
    POT_REF_CM,
    SOIL_SHIFTS,
    CARE_MISS_TTL_S,
    binomial_case,
    cm_from_text,
    kind_for,
    read_candidates,
    size_shifts,
    sole_match,
    Taxon,
    create_app,
    normalise_species,
    parse_advice,
    pick_species,
    read_gbif,
    read_trefle,
    target_band,
)

TOKEN = "test-token"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "butler.db"


class Sources:
    """A stand-in for the two web services, and a record of what was asked.

    Answers are keyed on a substring of the URL so a test says what it means
    ("gbif", "search", "ocimum-basilicum") instead of rebuilding query
    strings. A None answer is the source being down; a missing key is a test
    that did not expect that call and should say so loudly.
    """

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for key, answer in self.answers.items():
            if key in url:
                return answer
        raise AssertionError(f"unexpected fetch: {url}")

    def hits(self, key):
        return sum(1 for url in self.calls if key in url)


def gbif(species="Ocimum basilicum", match="EXACT", rank="SPECIES", family="Lamiaceae"):
    return {
        "matchType": match,
        # GBIF sends confidence 100 with matchType NONE; the parser must not
        # believe it, and this fixture keeps the trap in the tests.
        "confidence": 100 if match == "NONE" else 99,
        "rank": rank,
        "kingdom": "Plantae",
        "canonicalName": species,
        "species": species,
        "family": family,
    }


def search(name="Ocimum basilicum", slug="ocimum-basilicum"):
    return {"data": [{"scientific_name": name, "slug": slug}]}


def detail(light=7, humidity=5, common="Basil"):
    return {
        "data": {
            "common_name": common,
            "image_url": "https://bs.plantnet.org/image/o/abc",
            "growth": {
                "light": light,
                "atmospheric_humidity": humidity,
                "ph_minimum": 6.5,
                "ph_maximum": 7.0,
                "soil_humidity": None,
                "minimum_temperature": {"deg_c": None},
            },
        }
    }


def row(name, slug, common=None, rank="species", image="https://img/x"):
    return {
        "scientific_name": name,
        "slug": slug,
        "common_name": common,
        "rank": rank,
        "image_url": image,
    }


MONSTERAS = {
    "data": [
        row("Monstera adansonii", "monstera-adansonii", "Tarovine"),
        row("Monstera deliciosa", "monstera-deliciosa", "Fruit-salad-plant"),
    ]
}

BASILS = {
    "data": [
        row("Clinopodium acinos", "clinopodium-acinos", "Basil thyme"),
        row("Ocimum gratissimum", "ocimum-gratissimum", "African basil"),
        row("Ocimum basilicum", "ocimum-basilicum", "Basil"),
    ]
}

LILIES = {
    "data": [
        row("Spathiphyllum floribundum", "spathiphyllum-floribundum", "Peace-lily"),
        row("Spathiphyllum wallisii", "spathiphyllum-wallisii", "Peace lily"),
    ]
}

TOMATOES = {
    "data": [
        row("Solanum lycopersicum", "solanum-lycopersicum", "Tomato"),
        row(
            "Solanum lycopersicum var. lycopersicum",
            "slvl",
            "Garden tomato",
            rank="var",
        ),
        row("Solanum betaceum", "solanum-betaceum", "Tree-tomato"),
    ]
}


BASIL = {
    "gbif": gbif(),
    "species/search": search(),
    "species/ocimum-basilicum": detail(),
}


def app(db, sources=None, trefle_token="trefle-token"):
    return TestClient(
        create_app(
            db_path=str(db),
            token=TOKEN,
            next_s=60,
            cmd_ttl_s=900,
            trefle_token=trefle_token,
            fetch=sources,
        )
    )


def look(client, q):
    answer = client.get("/species", params={"q": q}, headers={"X-Token": TOKEN})
    assert answer.status_code == 200, answer.text
    return answer.json()


# --- the taxonomy hop ----------------------------------------------------


def test_normalise_species_folds_the_wire_spelling():
    # A k=v token cannot hold a space, so the app sends underscores.
    assert normalise_species("Ocimum_basilicum") == "ocimum basilicum"
    assert normalise_species("  OCIMUM   basilicum \n") == "ocimum basilicum"


def test_gbif_is_asked_in_botanical_case():
    # GBIF answers NONE for `monstera` and GENUS for `Monstera`, so the
    # cache key and the question cannot be the same string.
    assert binomial_case("ocimum basilicum") == "Ocimum basilicum"
    assert binomial_case("monstera") == "Monstera"
    assert binomial_case("") == ""


def test_gbif_none_arrives_with_full_confidence():
    # The trap this parser exists for: matchType NONE, confidence 100.
    assert read_gbif(gbif(match="NONE")) == Taxon(None, None, "none")


def test_gbif_synonym_resolves_to_the_accepted_name():
    payload = gbif(species="Dracaena trifasciata")
    payload["canonicalName"] = "Sansevieria trifasciata"
    payload["status"] = "SYNONYM"
    assert read_gbif(payload).accepted == "Dracaena trifasciata"


def test_gbif_fuzzy_is_a_hit_that_says_so():
    assert read_gbif(gbif(match="FUZZY")).matched == "fuzzy"


def test_gbif_genus_is_not_enough_to_look_up():
    payload = gbif(rank="GENUS")
    payload["species"] = None
    assert read_gbif(payload) == Taxon(None, "GENUS", "genus")


def test_gbif_outside_the_plant_kingdom_is_a_wrong_hop():
    payload = gbif()
    payload["kingdom"] = "Animalia"
    assert read_gbif(payload).accepted is None


# --- the care source -----------------------------------------------------


def test_the_shortlist_leaves_out_the_varieties():
    # "Solanum lycopersicum var. lycopersicum" is a worse answer to "tomato"
    # than the species is, and it is the one Trefle ranks second.
    names = [c["name"] for c in read_candidates(TOMATOES)]
    assert names == ["Solanum lycopersicum", "Solanum betaceum"]


def test_the_shortlist_drops_a_picture_the_phone_should_not_load():
    payload = {"data": [row("X y", "x-y", "X", image="javascript:alert(1)")]}
    assert read_candidates(payload)[0]["image"] is None


def test_one_plant_of_that_name_is_followed_and_two_are_not():
    assert sole_match(read_candidates(BASILS), "basil") == "Ocimum basilicum"
    assert sole_match(read_candidates(LILIES), "peace lily") is None
    assert sole_match(read_candidates(MONSTERAS), "monstera") is None


def test_pick_species_takes_the_binomial_and_not_the_neighbour():
    payload = {
        "data": [
            {"scientific_name": "Basilicum polystachyon", "slug": "basilicum"},
            {"scientific_name": "Ocimum basilicum", "slug": "ocimum-basilicum"},
        ]
    }
    assert pick_species(payload, "ocimum basilicum") == "ocimum-basilicum"


def test_pick_species_refuses_a_near_miss():
    # Trefle answers a query it does not know with whatever was nearest.
    assert pick_species(search("Ocimum americanum", "oa"), "ocimum basilicum") is None


def test_read_trefle_keeps_the_few_fields_that_exist():
    care = read_trefle(detail())
    assert care["light"] == 7 and care["humidity"] == 5
    assert care["ph_min"] == 6.5 and care["common_name"] == "Basil"


def test_read_trefle_survives_a_species_that_carries_nothing():
    # Dracaena trifasciata, on 2026-09-04: resolves, and every field null.
    care = read_trefle({"data": {"scientific_name": "x", "growth": {}}})
    assert set(care.values()) == {None}


def test_read_trefle_refuses_an_image_the_app_should_not_load():
    payload = detail()
    payload["data"]["image_url"] = "javascript:alert(1)"
    assert read_trefle(payload)["image_url"] is None


def test_read_trefle_refuses_a_light_level_that_is_not_one():
    payload = detail(light=True)
    assert read_trefle(payload)["light"] is None
    assert read_trefle(detail(light=99))["light"] is None


# --- the band, which comes from neither of them --------------------------


def test_band_without_a_single_field_is_still_an_offer():
    band = target_band(None, None, None, None, 4)  # April: no seasonal shift
    assert (band.low, band.high) == (35, 55)
    assert "unlabelled" in band.why


def test_band_follows_the_kind_of_plant():
    assert target_band("succulent", None, None, None, 4)[:2] == (15, 30)
    assert target_band("fern", None, None, None, 4)[:2] == (55, 75)


def test_a_kind_outside_the_set_reads_as_unlabelled():
    # Tolerant on the way out, strict on the way in: `plant_type` was free
    # text until 0.15.0, so a row may still say "basil" or "foliage", and
    # the base band is the honest reading of one. parse_pot refuses to
    # write a new one — that half is tested in test_pots.
    for stale in ("basil", "foliage", "hardy fern", "cauliflower"):
        assert target_band(stale, None, None, None, 4)[:2] == BASE_BAND


def test_a_cactus_is_drier_than_the_succulents_it_used_to_share_a_row_with():
    assert target_band("cactus", None, None, None, 4)[:2] == (10, 25)
    assert target_band("succulent", None, None, None, 4)[:2] == (15, 30)


def test_the_new_kinds_all_move_the_band():
    """A dropdown entry that landed on the base band would be a choice with
    no consequence — the thing the closed set exists to prevent."""
    for kind in ("cactus", "orchid", "mediterranean", "bulb", "palm", "carnivorous"):
        assert target_band(kind, None, None, None, 4)[:2] != BASE_BAND, kind


def test_gritty_soil_and_a_small_pot_pull_opposite_ways():
    assert target_band("herb", "sandy", None, None, 4)[:2] == (30, 50)
    assert target_band("herb", None, 10, None, 4)[:2] == (39, 55)


def test_a_soil_outside_the_set_reads_as_unsaid():
    """Tolerant out, strict in, exactly like the plant kind: rows written
    while soil was free text still read, and shift nothing."""
    for stale in ("sandy loam", "not sandy, holds moisture well", "universal"):
        assert target_band("fern", stale, None, None, 4)[:2] == (55, 75)


def test_winter_is_drier_and_summer_is_not():
    assert target_band("herb", None, None, None, 1)[:2] == (25, 45)
    assert target_band("herb", None, None, None, 7)[:2] == (40, 55)


def test_a_squeezed_band_gives_way_at_the_bottom():
    # A succulent in clay in a small pot: the shifts close the band
    # completely. Widening it upwards would offer a wetter ceiling than the
    # succulent's own 30%, and would contradict the "clay soil" beside it.
    low, high = target_band("succulent", "clay", 8, None, 7)[:2]
    assert (low, high) == (15, 25)
    assert high <= 30


# --- the measurements ----------------------------------------------------


def test_the_reference_pot_moves_nothing():
    low, high, why = size_shifts(POT_REF_CM, None)
    assert (low, high, why) == (0.0, 0.0, [])
    # And a plant of the height that pot assumes moves nothing either.
    assert size_shifts(POT_REF_CM, POT_REF_CM * 1.5)[2] == []


def test_the_shift_is_the_log_of_the_volume_not_the_volume():
    """A 40 cm pot holds 23x a 14 cm one. The band moves by a step per
    doubling of that buffer, not by a factor of 23 — which is the whole
    reason this is not linear in the cube."""
    small = target_band("herb", None, 10, None, 4)
    big = target_band("herb", None, 24, None, 4)
    huge = target_band("herb", None, 40, None, 4)
    assert (small.low, small.high) == (39, 55)
    assert (big.low, big.high) == (29, 49)
    # 23x the water, and still a single-digit shift — here the cap, which
    # 28 cm already reaches.
    assert (huge.low, huge.high) == (28, 48)
    assert target_band("herb", None, 28, None, 4)[:2] == (28, 48)


def test_only_a_big_pot_touches_the_ceiling():
    # No pot size is a reason to keep a plant WETTER than its kind wants.
    assert target_band("herb", None, 8, None, 4)[:2] == (41, 55)
    assert target_band("herb", None, 30, None, 4)[1] < 55


def test_height_is_read_against_the_pot_not_on_its_own():
    """40 cm of basil is thirsty in a 10 cm pot and comfortable in a 30 cm
    one, so the same height moves the floor in opposite directions."""
    cramped = target_band("herb", None, 10, 40, 4)
    roomy = target_band("herb", None, 30, 40, 4)
    assert cramped.low > roomy.low
    assert (cramped.low, cramped.high) == (42, 55)
    # A height with no pot to measure against falls back to the reference
    # pot, which is the assumption the base band already makes.
    assert size_shifts(None, 40)[0] == size_shifts(None, 40)[0]
    assert target_band("herb", None, None, 21, 4)[:2] == (35, 55)


def test_height_never_lifts_the_ceiling():
    for height in (1, 21, 200, 1000):
        assert target_band("herb", None, 14, height, 4).high == 55


def test_a_typo_sized_measurement_is_capped_not_obeyed():
    # 200 cm across and 1000 cm tall are both inside what the write path
    # allows, and neither may propose a band nobody could water to.
    for diameter in (1, 200):
        for height in (1, 1000):
            band = target_band("herb", None, diameter, height, 1)
            assert 5 <= band.low < band.high <= 95, (diameter, height, band)
    assert size_shifts(1, None)[0] == 7.5  # the cap, exactly
    assert size_shifts(200, None)[0] == -7.5


def test_zero_and_negative_are_unsaid_rather_than_fatal():
    # The write path refuses these; a row written before it did must not
    # take the whole garden down through a log of zero.
    assert size_shifts(0, 0) == (0.0, 0.0, [])
    assert size_shifts(-5, -5) == (0.0, 0.0, [])
    assert target_band("herb", None, 0, 0, 4)[:2] == (35, 55)


def test_a_shift_too_small_to_matter_is_not_explained():
    # Within a centimetre of the reference pot: under half a point, which
    # cannot move a whole-point band on its own.
    assert size_shifts(14.5, None)[2] == []
    assert target_band("herb", None, 14.5, None, 4).why == "herb"
    # But anything that DID move it is named, however little it moved.
    assert target_band("herb", None, 15, None, 4).why == "herb, 15 cm pot"


def test_the_band_never_closes_or_leaves_the_scale():
    """Every combination, not one sample: the offer a human is asked to
    accept must never be inverted, shut, off the scale, or wetter at the top
    than the plant type's own unmodified ceiling."""
    kinds = [None, *PLANT_KINDS]
    soils = [None, *SOIL_SHIFTS]
    diameters = [None, 1, 8, 14, 30, 200]
    heights = [None, 1, 21, 120, 1000]
    for kind in kinds:
        for soil in soils:
            for diameter in diameters:
                for height in heights:
                    for month in range(1, 13):
                        band = target_band(kind, soil, diameter, height, month)
                        where = (kind, soil, diameter, height, month, band)
                        assert 5 <= band.low < band.high <= 95, where
                        assert band.high - band.low >= 10, where
                        if kind:
                            assert band.high <= PLANT_KINDS[kind][1], where


def test_the_reason_names_what_moved_it():
    why = target_band("herb", "sandy", 10, 40, 1).why
    assert why == "herb, sandy soil, 10 cm pot, 40 cm plant, winter"


def test_every_soil_carries_its_own_phrase():
    """The word on the wire is not always the phrase a person reads: `bark`
    is a mix, not a soil, and "bark soil" would be nonsense."""
    assert target_band(None, "bark", None, None, 4).why.endswith("bark mix")
    assert target_band(None, "sphagnum", None, None, 4).why.endswith("sphagnum moss")
    for wire in SOIL_SHIFTS:
        assert SOIL_SHIFTS[wire][2] in target_band(None, wire, None, None, 4).why


def test_a_measurement_reads_as_a_number_not_a_word():
    assert cm_from_text("14cm") == 14.0
    assert cm_from_text("10") == 10.0
    assert cm_from_text("3.5 cm across") == 3.5
    # A word meant something to the old keyword table and would have to be
    # invented into centimetres here, so it is dropped instead.
    assert cm_from_text("small") is None
    assert cm_from_text("large") is None
    assert cm_from_text(None) is None
    assert cm_from_text("") is None
    assert cm_from_text("0") is None  # a 0 cm pot was never a measurement


# --- the plant kind the lookup offers ------------------------------------


def test_a_family_suggests_a_kind():
    assert kind_for("Ocimum basilicum", "Lamiaceae") == "herb"
    assert kind_for("Monstera deliciosa", "Araceae") == "tropical"
    assert kind_for("Crepis vesicaria", "Asteraceae") == "flower"
    assert kind_for("Nephrolepis exaltata", "Nephrolepidaceae") == "fern"
    # A palm is no longer filed under the leafy houseplants, and a cactus is
    # no longer filed under the succulents.
    assert kind_for("Chamaedorea elegans", "Arecaceae") == "palm"
    assert kind_for("Dionaea muscipula", "Droseraceae") == "carnivorous"
    # GBIF's case is not a promise.
    assert kind_for("Echinopsis pachanoi", "CACTACEAE") == "cactus"


def test_an_orchid_is_answered_now_rather_than_dodged():
    """Orchidaceae used to be left out on purpose, because a bark epiphyte
    waters nothing like a flowering pot plant and there was no band for one.
    There is now, so the honest answer is available."""
    assert kind_for("Phalaenopsis amabilis", "Orchidaceae") == "orchid"


def test_the_genus_is_asked_before_the_family():
    # Asparagaceae holds a leafy thing that wants watering and a succulent
    # in all but name, so the family alone would water one of them wrong.
    assert kind_for("Dracaena fragrans", "Asparagaceae") == "tropical"
    assert kind_for("Dracaena trifasciata", "Asparagaceae") == "succulent"
    assert kind_for("Zamioculcas zamiifolia", "Araceae") == "succulent"
    assert kind_for("Euphorbia trigona", "Euphorbiaceae") == "succulent"


def test_an_unknown_family_offers_nothing_rather_than_a_guess():
    # An unlisted family means nobody here knows, and "not sure" already
    # behaves correctly — better than 20 confident points the wrong way.
    assert kind_for("Ginkgo biloba", "Ginkgoaceae") is None
    assert kind_for("Some plant", "Nothingaceae") is None
    assert kind_for("Some plant", None) is None
    assert kind_for(None, "Lamiaceae") is None


def test_every_suggested_kind_is_one_the_form_can_show():
    from butler import FAMILY_KINDS, GENUS_KINDS, SPECIES_KINDS

    for table in (FAMILY_KINDS, GENUS_KINDS, SPECIES_KINDS):
        for name, kind in table.items():
            assert kind in PLANT_KINDS, (name, kind)


# --- the endpoint --------------------------------------------------------


def test_species_needs_the_token(db):
    assert app(db, Sources(BASIL)).get("/species", params={"q": "x"}).status_code == 401


def test_species_refuses_an_empty_or_giant_query(db):
    client = app(db, Sources(BASIL))
    head = {"X-Token": TOKEN}
    assert client.get("/species", params={"q": "  "}, headers=head).status_code == 400
    long = client.get("/species", params={"q": "a" * 200}, headers=head)
    assert long.status_code == 400


def test_the_answer_offers_a_plant_kind_for_the_dropdown(db):
    """The one thing a care source could never give: species finally reaches
    the band, through a field a human can see and overrule."""
    answer = look(app(db, Sources(BASIL)), "Ocimum_basilicum")
    assert answer["kind"] == "herb"


def test_the_kind_survives_the_cache(db):
    """The family is stored with the name, so the second ask reaches nobody
    and still knows what to pre-select."""
    sources = Sources(BASIL)
    client = app(db, sources)
    look(client, "Ocimum_basilicum")
    before = sources.hits("gbif")
    assert look(client, "Ocimum_basilicum")["kind"] == "herb"
    assert sources.hits("gbif") == before


def test_a_name_cached_without_a_family_is_asked_again(db):
    """A row that resolved a name but carries no family can suggest no plant
    kind, and a cache hit never re-asks — so without this it would suggest
    nothing for the life of the database. Reachable two ways: a row written
    before the family column existed, and a GBIF answer that carried none.
    Re-asking is TTL-gated, so the cost is one call a month, not one a
    screen open."""
    sources = Sources({**BASIL, "gbif": gbif(family=None)})
    client = app(db, sources)
    assert look(client, "Ocimum_basilicum")["kind"] is None
    before = sources.hits("gbif")

    # A month on, and GBIF has learnt the family in the meantime.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE species_names SET fetched_ts = ?", (int(time.time()) - 31 * 86400,))
    sources.answers = {**BASIL, "gbif": gbif(family="Lamiaceae")}
    assert look(client, "Ocimum_basilicum")["kind"] == "herb"
    assert sources.hits("gbif") == before + 1


def test_a_re_ask_that_cannot_reach_gbif_keeps_the_name_it_had(db):
    """The trap in the re-ask: turning a name that resolved yesterday into
    "the lookup is not answering" would be a regression the day GBIF is
    down, for a row that was perfectly usable."""
    sources = Sources({**BASIL, "gbif": gbif(family=None)})
    client = app(db, sources)
    assert look(client, "Ocimum_basilicum")["accepted"] == "Ocimum basilicum"

    with sqlite3.connect(db) as con:
        con.execute("UPDATE species_names SET fetched_ts = ?", (int(time.time()) - 31 * 86400,))
    sources.answers = {**BASIL, "gbif": None}  # the source is down
    answer = look(client, "Ocimum_basilicum")
    assert answer["accepted"] == "Ocimum basilicum"
    assert answer["kind"] is None


def test_a_complete_row_is_never_asked_again(db):
    """The other side of the fence: a row with a family is a hit for ever,
    and the TTL must not be read as an expiry for it."""
    sources = Sources(BASIL)
    client = app(db, sources)
    look(client, "Ocimum_basilicum")
    before = sources.hits("gbif")
    with sqlite3.connect(db) as con:
        con.execute("UPDATE species_names SET fetched_ts = ?", (int(time.time()) - 400 * 86400,))
    assert look(client, "Ocimum_basilicum")["kind"] == "herb"
    assert sources.hits("gbif") == before


def test_a_family_nobody_listed_offers_no_kind(db):
    sources = Sources({**BASIL, "gbif": gbif(family="Ginkgoaceae")})
    assert look(app(db, sources), "Ocimum_basilicum")["kind"] is None


def test_a_name_that_resolved_to_nothing_offers_no_kind(db):
    sources = Sources({**BASIL, "gbif": gbif(match="NONE")})
    assert look(app(db, sources), "Ocimum_basilicum")["kind"] is None


def test_a_known_plant_comes_back_with_its_numbers(db):
    answer = look(app(db, Sources(BASIL)), "Ocimum_basilicum")
    assert answer["accepted"] == "Ocimum basilicum"
    assert answer["matched"] == "exact"
    assert answer["care"]["light"] == 7
    assert answer["care"]["found"] is True


def test_a_typo_is_corrected_and_the_correction_is_said_out_loud(db):
    sources = Sources({**BASIL, "gbif": gbif(match="FUZZY")})
    answer = look(app(db, sources), "Ocimum basilicom")
    assert answer["matched"] == "fuzzy"
    assert "read as Ocimum basilicum" in answer["note"]


def test_the_second_lookup_asks_nobody(db):
    sources = Sources(BASIL)
    client = app(db, sources)
    look(client, "Ocimum basilicum")
    look(client, "OCIMUM   BASILICUM")  # same plant, other spelling
    assert sources.hits("gbif") == 1
    assert sources.hits("species/search") == 1


def test_the_garden_can_still_report_while_a_lookup_is_waiting(db):
    """A lookup makes up to three HTTP calls with their own timeouts. If a
    write transaction were open across them, every board report in that
    window would answer "try again" — somebody typing a plant's name must
    not be able to stop the garden reporting."""
    wrote = []

    class Meanwhile(Sources):
        def __call__(self, url):
            # Stands in for POST /report landing mid-lookup: a writer with a
            # short timeout, which fails outright if the lock is held.
            with sqlite3.connect(db, timeout=0.2) as other:
                other.execute(
                    "INSERT INTO readings (ts, controller, channel, raw) "
                    "VALUES (1, 0, 0, 8000)"
                )
            wrote.append(url)
            return super().__call__(url)

    look(app(db, Meanwhile(BASIL)), "Ocimum basilicum")
    assert len(wrote) == 3  # gbif, the search, the species page


def test_a_name_service_that_is_down_is_not_a_plant_that_does_not_exist(db):
    sources = Sources({"gbif": None, "species/search": {"data": []}})
    answer = look(app(db, sources), "Ocimum basilicum")
    assert answer["matched"] == "unavailable"
    assert "not answering" in answer["note"]
    # And nothing was written down, so a later lookup asks again.
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM species_names").fetchone()[0] == 0


def test_a_care_source_that_is_down_leaves_the_name_resolved(db):
    sources = Sources({"gbif": gbif(), "species/search": None})
    answer = look(app(db, sources), "Ocimum basilicum")
    assert answer["accepted"] == "Ocimum basilicum"
    assert answer["care"] is None


def test_with_no_token_the_name_still_resolves(db):
    sources = Sources({"gbif": gbif()})
    answer = look(app(db, sources, trefle_token=""), "Ocimum basilicum")
    assert answer["accepted"] == "Ocimum basilicum"
    assert answer["care"] is None
    assert "type the numbers in" in answer["note"]


def test_a_genus_offers_its_species_with_their_pictures(db):
    payload = gbif(rank="GENUS")
    payload["species"] = None
    sources = Sources({"gbif": payload, "species/search": MONSTERAS})
    answer = look(app(db, sources), "Monstera")
    assert answer["matched"] == "genus"
    assert [c["name"] for c in answer["candidates"]] == [
        "Monstera adansonii",
        "Monstera deliciosa",
    ]
    assert answer["candidates"][0]["image"].startswith("https://")
    assert "pick the plant you recognise" in answer["note"]
    # And GBIF was asked as "Monstera": it finds no genus in lower case.
    assert "name=Monstera" in sources.calls[0]


def test_a_genus_nobody_can_search_still_says_which_species(db):
    payload = gbif(rank="GENUS")
    payload["species"] = None
    sources = Sources({"gbif": payload, "species/search": {"data": []}})
    answer = look(app(db, sources), "Monstera")
    assert answer["candidates"] == []
    assert "which species" in answer["note"]


def test_an_unknown_name_that_nothing_can_place_says_so(db):
    sources = Sources({"gbif": gbif(match="NONE"), "species/search": {"data": []}})
    answer = look(app(db, sources), "zzqq notaplant")
    assert answer["accepted"] is None
    assert answer["candidates"] == []
    assert "check the spelling" in answer["note"]


def test_a_common_name_that_is_exactly_one_plant_is_followed(db):
    # "basil" is nothing at all to GBIF, and among Trefle's basil thymes and
    # African basils exactly one is called Basil. That is not a guess.
    sources = Sources(
        {
            "name=Basil": gbif(match="NONE"),
            "name=Ocimum": gbif(),
            "species/search": BASILS,
            "species/ocimum-basilicum": detail(),
        }
    )
    answer = look(app(db, sources), "basil")
    assert answer["matched"] == "common"
    assert answer["accepted"] == "Ocimum basilicum"
    assert answer["care"]["light"] == 7
    assert answer["query"] == "basil"


def test_two_plants_of_the_same_name_are_a_question_and_not_a_guess(db):
    # Trefle spells the same common name two ways in adjacent rows. Picking
    # either would be inventing an answer; two pictures is the honest one.
    sources = Sources({"gbif": gbif(match="NONE"), "species/search": LILIES})
    answer = look(app(db, sources), "peace lily")
    assert answer["accepted"] is None
    assert len(answer["candidates"]) == 2
    assert "pick the plant you recognise" in answer["note"]


def test_a_typo_past_gbif_still_gets_pictures(db):
    sources = Sources({"gbif": gbif(match="NONE"), "species/search": TOMATOES})
    answer = look(app(db, sources), "tomatoe")
    assert [c["common"] for c in answer["candidates"]] == ["Tomato", "Tree-tomato"]


def test_a_name_gbif_resolved_gets_no_second_guessing_shortlist(db):
    # Ficus lyrata: GBIF places it exactly, Trefle has never heard of it.
    # Offering other figs would be worse than saying so.
    sources = Sources(
        {"gbif": gbif("Ficus lyrata"), "species/search": {"data": []}}
    )
    answer = look(app(db, sources), "Ficus lyrata")
    assert answer["accepted"] == "Ficus lyrata"
    assert answer["candidates"] == []
    assert sources.hits("species/search") == 1  # the exact one, not a shortlist


def test_the_shortlist_is_cached_too(db):
    sources = Sources({"gbif": gbif(match="NONE"), "species/search": LILIES})
    client = app(db, sources)
    look(client, "peace lily")
    look(client, "PEACE  LILY")
    assert sources.hits("species/search") == 1


def test_a_plant_trefle_does_not_have_is_the_ordinary_case(db):
    # Ficus lyrata: GBIF knows it, Trefle has never heard of it.
    sources = Sources(
        {"gbif": gbif("Ficus lyrata"), "species/search": {"data": []}}
    )
    client = app(db, sources)
    answer = look(client, "Ficus lyrata")
    assert answer["care"]["found"] is False
    assert "not in Trefle" in answer["note"]
    look(client, "Ficus lyrata")
    assert sources.hits("species/search") == 1  # the miss is cached too


def test_a_cached_miss_is_asked_again_a_month_later(db):
    sources = Sources({"gbif": gbif("Ficus lyrata"), "species/search": {"data": []}})
    client = app(db, sources)
    look(client, "Ficus lyrata")
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE species_care SET fetched_ts = ?",
            (int(time.time()) - CARE_MISS_TTL_S - 1,),
        )
    look(client, "Ficus lyrata")
    assert sources.hits("species/search") == 2


def test_a_species_that_resolves_but_carries_nothing_says_which(db):
    sources = Sources(
        {
            "gbif": gbif("Dracaena trifasciata"),
            "species/search": search("Dracaena trifasciata", "dracaena-trifasciata"),
            "species/dracaena": {"data": {"growth": {}}},
        }
    )
    answer = look(app(db, sources), "Sansevieria trifasciata")
    assert answer["care"]["found"] is True
    assert answer["care"]["light"] is None
    assert "has no numbers" in answer["note"]


# --- the offer at the pot ------------------------------------------------


def make_pot(client, **fields):
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    answer = client.post("/pot", content=body, headers={"X-Token": TOKEN})
    # The 200 first: a refusal's text splits into a plausible id too, and a
    # test that then asks about it passes for the wrong reason.
    assert answer.status_code == 200, answer.text
    return answer.text.split()[0].removeprefix("pot=")


def garden(client):
    answer = client.get("/pots", headers={"X-Token": TOKEN})
    assert answer.status_code == 200, answer.text
    return {p["name"]: p for p in answer.json()["pots"]}


def test_a_pot_with_no_band_is_offered_one(db):
    client = app(db, Sources(BASIL))
    make_pot(client, name="basil", plant_type="herb", soil="sandy")
    advice = garden(client)["basil"]["advice"]
    assert advice["kind"] == "target"
    assert advice["low"] < advice["high"]
    assert "herb" in advice["why"] and "sandy soil" in advice["why"]


def test_the_offer_goes_quiet_once_the_numbers_are_the_offered_ones(db):
    client = app(db, Sources(BASIL))
    pot = make_pot(client, name="basil", plant_type="herb")
    advice = garden(client)["basil"]["advice"]
    client.post(
        "/pot",
        content=f"id={pot} target_low_pct={advice['low']} "
        f"target_high_pct={advice['high']}",
        headers={"X-Token": TOKEN},
    )
    assert garden(client)["basil"]["advice"] is None


def test_a_refused_offer_stays_refused(db):
    client = app(db, Sources(BASIL))
    pot = make_pot(client, name="basil", plant_type="herb")
    assert garden(client)["basil"]["advice"] is not None
    refuse = client.post(
        "/advice", content=f"pot={pot} kind=target dismiss=1", headers={"X-Token": TOKEN}
    )
    assert refuse.status_code == 200, refuse.text
    assert garden(client)["basil"]["advice"] is None


def test_a_different_offer_is_a_new_question(db):
    client = app(db, Sources(BASIL))
    pot = make_pot(client, name="basil", plant_type="herb")
    client.post(
        "/advice", content=f"pot={pot} dismiss=1", headers={"X-Token": TOKEN}
    )
    assert garden(client)["basil"]["advice"] is None
    # A repot changes the numbers, so the refusal no longer covers them.
    client.post(
        "/pot", content=f"id={pot} pot_diameter_cm=10", headers={"X-Token": TOKEN}
    )
    assert garden(client)["basil"]["advice"] is not None


def test_a_buried_pot_is_not_nagged(db):
    client = app(db, Sources(BASIL))
    pot = make_pot(client, name="basil", plant_type="herb")
    client.post("/pot", content=f"id={pot} status=graveyard", headers={"X-Token": TOKEN})
    assert garden(client)["basil"]["advice"] is None


def test_advice_only_dismisses(db):
    client = app(db, Sources(BASIL))
    pot = make_pot(client, name="basil", plant_type="herb")
    for body in (f"pot={pot}", f"pot={pot} dismiss=0", f"pot={pot} kind=dose dismiss=1"):
        answer = client.post("/advice", content=body, headers={"X-Token": TOKEN})
        assert answer.status_code == 400, body
    assert client.post("/advice", content="dismiss=1").status_code == 401
    unknown = client.post(
        "/advice", content="pot=pot-nope dismiss=1", headers={"X-Token": TOKEN}
    )
    assert unknown.status_code == 400


def test_the_garden_finds_the_care_under_the_name_the_pot_actually_stores(db):
    """The lookup offers the accepted name and the form takes it, so the pot
    usually stores "Dracaena trifasciata" — which is a key in the care cache
    but NOT in the alias table, whose key is what somebody typed."""
    sources = Sources(
        {
            "gbif": gbif("Dracaena trifasciata"),
            "species/search": search("Dracaena trifasciata", "dracaena-trifasciata"),
            "species/dracaena": detail(light=4, common="Snake plant"),
        }
    )
    client = app(db, sources)
    look(client, "Sansevieria trifasciata")  # typed the old name
    make_pot(client, name="snake", species="Dracaena_trifasciata")  # stored the new one
    care = garden(client)["snake"]["care"]
    assert care is not None and care["light"] == 4


def test_the_garden_carries_what_was_looked_up_without_asking_again(db):
    sources = Sources(BASIL)
    client = app(db, sources)
    look(client, "Ocimum basilicum")
    make_pot(client, name="basil", species="Ocimum_basilicum")
    care = garden(client)["basil"]["care"]
    assert care["light"] == 7 and care["common_name"] == "Basil"
    assert sources.hits("gbif") == 1  # the garden asks nobody
