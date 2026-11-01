"""Add-ons (the doorbell, Bulb Shows): off until switched on, and while off they do nothing; and the settings that
used to be one home's, now in config.json. Fakes only: no doorbell, no Bulb Show, no light is touched.
Run with  .venv\\Scripts\\python -m unittest tests.test_addons -v"""

import shutil
import tempfile
import unittest
from types import SimpleNamespace

from lightshow.studio import addons, rooms, space
from lightshow.studio.jam_service import DEFAULT_OPENER, JamService


class TestAddons(unittest.TestCase):
    def test_off_until_switched_on_and_kept(self):
        cfg = {}
        self.assertFalse(addons.enabled(cfg, "doorbell"))
        self.assertEqual([a["id"] for a in addons.status(cfg)], ["doorbell", "bulbshow", "homeassistant"])
        self.assertTrue(all(not a["on"] for a in addons.status(cfg)))
        store = {}
        addons.switch(lambda: dict(store), store.update, "doorbell", True)
        self.assertEqual(store["addons"], {"doorbell": True})
        self.assertTrue(addons.enabled(store, "doorbell"))

    def test_only_known_add_ons(self):
        with self.assertRaises(ValueError):
            addons.switch(dict, lambda c: None, "toaster", True)


class TestAddonsDoNothingWhileOff(unittest.TestCase):
    def service(self, cfg):
        s = SimpleNamespace(load_config=lambda: cfg, studio=SimpleNamespace(bulbshows=object()))
        s.cue = lambda: JamService._doorbell_cue(s)
        s.bulbs = lambda: JamService._bulbshows(s)
        return s

    def test_the_doorbell_cue_is_left_on_the_hub_while_the_add_on_is_off(self):
        bell = {"doorbell": {"on": True, "host": "10.0.0.5", "cue": "doorbell"}}
        self.assertEqual(self.service(bell).cue(), "")                           # set up, but the add-on is off
        self.assertEqual(self.service({**bell, "addons": {"doorbell": True}}).cue(), "doorbell")

    def test_bulb_show_is_not_asked_about_while_the_add_on_is_off(self):
        self.assertIsNone(self.service({}).bulbs())
        self.assertIsNotNone(self.service({"addons": {"bulbshow": True}}).bulbs())


class TestOneHomesSettings(unittest.TestCase):
    def test_a_room_can_be_left_off_the_house_map(self):
        out = rooms.clean([{"id": "a", "name": "Office", "map": False}, {"id": "b", "name": "Kitchen"}])
        self.assertIs(out[0]["map"], False)
        self.assertNotIn("map", out[1])                                         # (on the map: nothing to keep)

    def test_a_new_home_has_no_opening_song_and_an_empty_plan(self):
        self.assertEqual(DEFAULT_OPENER, "")
        d = tempfile.mkdtemp()
        try:
            doc = space.Space(d, make_output=None, show_running=lambda: False, known=lambda: [], segments_of=lambda k: 10).load()
            self.assertNotIn("shapes", doc["plan"])                             # the map page opens its floor plan builder
            self.assertEqual((doc["plan"]["width_ft"], doc["plan"]["depth_ft"]), (30, 20))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_plan_drawn_in_the_builder_is_kept(self):
        doc = {"plan": {"width_ft": 22.5, "depth_ft": 14.5, "shapes": {
            "rooms": [{"name": "Living Room", "poly": [[2.5, 1.5], [14.5, 1.5], [14.5, 11.5], [2.5, 11.5]]}],
            "fixtures": [{"name": "Couch", "rect": [3, 2.5, 6.5, 3]}], "walls": [], "soffits": []}}, "lights": {}}
        out = space.clean(doc)["plan"]["shapes"]
        self.assertEqual(out["rooms"][0]["name"], "Living Room")
        self.assertEqual(out["fixtures"], [{"name": "Couch", "rect": [3.0, 2.5, 6.5, 3.0]}])


if __name__ == "__main__":
    unittest.main()
