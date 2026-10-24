"""The star map's name search (models/star_catalog.search) and its endpoints:
typos, names in other languages, and catalogue numbers that only match exactly."""
import array
import unittest

from harness import app
from models import star_catalog
from models.star_catalog import StarCatalog

# designation, magnitude, names cell (as the catalogue tables hold it)
OBJECTS = [
    ("HD48915", -1.46, "Sirius, Alpha Canis Majoris"),
    ("HD39801", 0.42, "Betelgeuse"),
    ("HD116656", 2.23, "Mizar"),
    ("HD129988", 2.35, "Izar"),
    ("NGC1432", 1.6, "Pleiades, Seven Sisters"),
    ("NGC0224", 3.44, "Andromeda Galaxy, M 31"),
    ("NGC0104", 4.09, "47 Tuc Cluster"),
    ("NGC6205", 5.8, "Hercules Globular Cluster"),
    ("NGC0221", 8.1, "(Companion of Andromeda Galaxy)"),
    ("NGC7635", 10.0, "Bubble Nebula"),
    ("NGC9999", 10.5, "Rubble Nebula"),  # made up, to tie with the Bubble Nebula
    ("NGC3242", 7.3, "Jupiter's Ghost Nebula"),
]

TRANSLATIONS = {"es": {
    ("HDSTARTable", "HD48915"): {"name": "Sirio", "aliases": []},
    ("NGCtable", "NGC0224"): {"name": "Galaxia de Andrómeda", "aliases": []},
    ("NGCtable", "NGC1432"): {"name": "Pléyades", "aliases": ["Siete Hermanas"]},
    ("SolarSystem", "moon"): {"name": "Luna", "aliases": []},
    ("SolarSystem", "jupiter"): {"name": "Júpiter", "aliases": []},
    ("ConstellationsTable", "Ori"): {"name": "Orión", "aliases": []},
}}

CONSTELLATIONS = [
    {"id": "Ori", "name": "Orion", "anchor": [84.0, 13.0], "lines": [[[88.8, 7.4], [81.3, 6.3]]]},
    # No label position: placed at the middle of its stars, across RA 0
    {"id": "And", "name": "Andromeda", "anchor": None, "lines": [[[358.0, 30.0], [2.0, 30.0]]]},
]


def make_catalog():
    names = [name for name, _, _ in OBJECTS]
    mags = [mag for _, mag, _ in OBJECTS]
    order = sorted(range(len(names)), key=lambda i: mags[i])
    records = {}
    for name, _, cell in OBJECTS:
        aliases = star_catalog.split_aliases(cell)
        records[name] = {"aliases": aliases,
                         "name": next(a for a in aliases if not star_catalog.is_designation(a))}
    return StarCatalog(
        [names[i] for i in order],
        array.array("d", [0.0] * len(names)), array.array("d", [0.0] * len(names)),
        array.array("d", [mags[i] for i in order]), len(names), "test",
        name_records=records, constellations=CONSTELLATIONS,
        messier={31: "NGC0224", 13: "NGC6205"}, translations=TRANSLATIONS)


class SearchTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = star_catalog._catalog
        star_catalog._catalog = make_catalog()

    @classmethod
    def tearDownClass(cls):
        star_catalog._catalog = cls.saved

    def keys(self, query, lang="en", **kwargs):
        return [m["key"] for m in star_catalog.search(query, lang, **kwargs)]


class CatalogueNumberTests(SearchTestCase):
    def test_numbers_match_exactly(self):
        for query in ("M13", "m 13", "Messier 13", "NGC 6205", "ngc6205"):
            matches = star_catalog.search(query)
            self.assertEqual([m["key"] for m in matches], ["NGC6205"], query)
            self.assertEqual(matches[0]["match"], "number")

    def test_numbers_never_turn_into_close_ones(self):
        # M14 and NGC 6206 aren't in this catalogue: nothing, not M13 or M31
        for query in ("M14", "M3", "M311", "NGC 6206", "HD48916", "IC 6205"):
            self.assertEqual(self.keys(query), [], query)

    def test_numbers_in_names_are_whole(self):
        self.assertEqual(self.keys("47 tuc"), ["NGC0104"])
        self.assertEqual(self.keys("48 tuc"), [])
        self.assertEqual(self.keys("4"), [])

    def test_designation_aliases_are_not_names(self):
        # "M 31" sits in the names cell, but only the number path may match it
        self.assertNotIn("NGC0224", self.keys("m 3"))


class NameTests(SearchTestCase):
    def test_typos(self):
        self.assertEqual(self.keys("sirus")[0], "HD48915")
        self.assertEqual(self.keys("betelguese")[0], "HD39801")
        self.assertEqual(self.keys("andromeda galxy")[0], "NGC0224")
        self.assertEqual(star_catalog.search("sirus")[0]["match"], "fuzzy")

    def test_short_words_need_no_typos(self):
        # 4 letters or fewer must be spelled right: "mizr" isn't Mizar
        self.assertEqual(self.keys("mizr"), [])

    def test_names_in_any_language(self):
        self.assertEqual(self.keys("Sirio", "en"), ["HD48915"])
        found = star_catalog.search("sirius", "es")[0]
        self.assertEqual((found["key"], found["name"], found["matched"]), ("HD48915", "Sirio", "Sirius"))
        self.assertEqual(self.keys("siete hermanas", "en"), ["NGC1432"])

    def test_small_words_and_word_order(self):
        self.assertEqual(self.keys("galaxia andromeda", "es")[0], "NGC0224")
        self.assertEqual(self.keys("galaxy andromeda")[0], "NGC0224")

    def test_exact_name_beats_longer_names(self):
        self.assertEqual(self.keys("mizar")[0], "HD116656")
        self.assertEqual(self.keys("andromeda")[:2], ["And", "NGC0224"])

    def test_bodies_and_constellations(self):
        self.assertEqual(star_catalog.search("luna")[0]["key"], "moon")
        self.assertEqual(star_catalog.search("jupter")[0]["key"], "jupiter")
        found = star_catalog.search("orion", "es")[0]
        self.assertEqual((found["kind"], found["key"], found["name"]), ("constellation", "Ori", "Orión"))

    def test_strict_lookups_skip_typos(self):
        self.assertEqual(star_catalog.find_by_proper_name("Sirio"), "HD48915")
        self.assertIsNone(star_catalog.find_by_proper_name("sirus"))
        self.assertEqual(star_catalog.find_body("Luna"), "moon")
        self.assertIsNone(star_catalog.find_body("lun"))

    def test_constellation_position(self):
        self.assertEqual(star_catalog.constellation_position("Ori"), (84.0, 13.0))
        ra, dec = star_catalog.constellation_position("And")
        self.assertAlmostEqual(min(ra, 360 - ra), 0.0, places=6)
        self.assertGreater(dec, 30.0)

    def test_edit_distance(self):
        self.assertEqual(star_catalog.edit_distance("betelguese", "betelgeuse", 2), 1)
        self.assertEqual(star_catalog.edit_distance("sirus", "sirius", 1), 1)
        self.assertEqual(star_catalog.edit_distance("abc", "xyz", 1), 2)


class EndpointTests(SearchTestCase):
    def setUp(self):
        self.client = app.test_client()

    def post(self, body, lang="en"):
        # The map posts searches without ?lang=: the cookie picks the language
        self.client.set_cookie("lang", lang)
        return self.client.post("/interface/search_object", json=body).get_json()

    def test_typo_flies_to_the_clear_winner(self):
        data = self.post({"searchValue": "sirus"})
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["Name"], "HD48915")
        self.assertEqual(data["data"]["match"]["match"], "fuzzy")

    def test_close_calls_ask(self):
        # "Hubble" is one letter from both "Bubble" and "Rubble": a list
        data = self.post({"searchValue": "hubble nebula"})
        self.assertEqual(data["status"], "choose")
        self.assertEqual(sorted(m["key"] for m in data["matches"]), ["NGC7635", "NGC9999"])

    def test_fewer_extra_words_wins(self):
        data = self.post({"searchValue": "andromeda galxy"})
        self.assertEqual((data["status"], data["data"]["Name"]), ("success", "NGC0224"))
        self.assertEqual([m["key"] for m in data["data"]["alternatives"]], ["NGC0221"])

    def test_number(self):
        data = self.post({"searchValue": "M13"})
        self.assertEqual(data["data"]["Name"], "NGC6205")
        self.assertEqual(data["data"]["match"]["match"], "number")
        self.assertEqual(self.post({"searchValue": "M14"})["status"], "error")

    def test_alternatives_leave_out_near_misses(self):
        data = self.post({"searchValue": "Mizar"})["data"]
        self.assertNotIn("HD129988", [m["key"] for m in data["alternatives"]])

    def test_pick_a_constellation_in_spanish(self):
        data = self.post({"searchValue": "", "pick": {"kind": "constellation", "key": "Ori"}}, lang="es")
        self.assertEqual(data["status"], "success")
        self.assertEqual((data["data"]["Name"], data["data"]["type"]), ("Orión", "constellation"))
        self.assertEqual((data["data"]["RA"], data["data"]["DEC"]), (84.0, 13.0))

    def test_spanish_name_shown_for_english_query(self):
        data = self.post({"searchValue": "Sirius"}, lang="es")["data"]
        self.assertEqual((data["friendlyName"], data["match"]["matched"]), ("Sirio", "Sirius"))

    def test_suggestions(self):
        data = self.client.get("/api/search_suggest?q=pley&lang=es").get_json()
        self.assertEqual(data["matches"][0]["name"], "Pléyades")
        data = self.client.get("/api/search_suggest?q=p&lang=es").get_json()
        self.assertEqual(data["matches"], [])


if __name__ == "__main__":
    unittest.main()
