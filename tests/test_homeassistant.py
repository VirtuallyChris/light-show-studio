"""The Home Assistant add-on (homeassistant.py): told when the show has the lights, which ones, a heartbeat while it
does, and at once when it lets go. A stand-in Home Assistant only: nothing is sent anywhere."""

import unittest
from types import SimpleNamespace

from lightshow.studio.homeassistant import HEARTBEAT_S, HomeAssistantLink


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class TestHomeAssistant(unittest.TestCase):
    def make(self, live=True, on=True):
        self.cfg = {"addons": {"homeassistant": on}, "homeassistant": {"url": "http://ha.local:8123", "webhook_id": "lss"},
                    "light_names": {"AA:BB": "Game Room strip"}, "rooms": [{"name": "Game Room", "lights": ["AA:BB"], "on": True}]}
        self.sent, self.clock = [], Clock()
        self.output = SimpleNamespace(enabled=True, active=False, devices=[SimpleNamespace(ip="10.0.0.5", mac="AA:BB", device_id="", sku="H608D")])
        return HomeAssistantLink(SimpleNamespace(output=self.output), lambda: dict(self.cfg), lambda c: None, live=live,
                                 post=lambda url, body: self.sent.append((url, body)), clock=self.clock)

    def test_told_when_the_show_takes_the_lights_and_which_ones(self):
        ha = self.make()
        ha.tick()
        self.assertEqual(self.sent[-1][1]["controlling"], False)
        self.output.active = True
        ha.tick()
        url, body = self.sent[-1]
        self.assertEqual(url, "http://ha.local:8123/api/webhook/lss")
        self.assertEqual((body["controlling"], body["lights"], body["ids"], body["rooms"]), (True, ["Game Room strip"], ["AA:BB"], ["Game Room"]))
        n = len(self.sent)
        ha.tick()
        self.assertEqual(len(self.sent), n)                              # nothing new: nothing sent

    def test_a_heartbeat_while_it_has_them(self):
        ha = self.make()
        self.output.active = True
        ha.tick(); n = len(self.sent)
        self.clock.t += HEARTBEAT_S + 1
        ha.tick()
        self.assertEqual(len(self.sent), n + 1)

    def test_closing_gives_them_back_at_once(self):
        ha = self.make()
        self.output.active = True
        ha.tick()
        ha.release()
        self.assertFalse(self.sent[-1][1]["controlling"])

    def test_a_test_copy_or_the_add_on_off_tells_it_nothing(self):
        for ha in (self.make(live=False), self.make(on=False)):
            self.output.active = True
            ha.tick(); ha.release()
            self.assertEqual(self.sent, [])

    def test_home_assistant_away_is_tried_again(self):
        ha = self.make()
        ha.post = lambda url, body: (_ for _ in ()).throw(OSError("refused"))
        self.output.active = True
        ha.tick()
        self.assertIn("refused", ha.error)
        ha.post = lambda url, body: self.sent.append((url, body))
        ha.tick()
        self.assertTrue(self.sent[-1][1]["controlling"])


if __name__ == "__main__":
    unittest.main()
