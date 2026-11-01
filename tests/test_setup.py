"""Setting up a home (setup.py): the checklist, the sound devices picked into the config, the phone's address and QR
code. Stand-ins only: no lights, speakers or microphone are touched."""

import unittest
from types import SimpleNamespace
from unittest import mock

from lightshow.studio import setup


def studio(**kw):
    base = dict(known=[], rooms=SimpleNamespace(list=lambda: []), space=SimpleNamespace(load=lambda: {}),
                cast=SimpleNamespace(casts=[]), jam=SimpleNamespace(spotify=SimpleNamespace(signed_in=False)))
    return SimpleNamespace(**{**base, **kw})


class TestSetup(unittest.TestCase):
    def make(self, cfg=None, **kw):
        self.cfg = dict(cfg or {})
        return setup.HomeSetup(studio(**kw), lambda: dict(self.cfg), lambda c: (self.cfg.clear(), self.cfg.update(c)), 8765)

    def test_a_new_home_has_steps_to_do(self):
        with mock.patch.object(setup, "_outputs", return_value=([], "")):
            items = {i["id"]: i for i in self.make().checklist()}
        self.assertFalse(items["lights"]["done"])
        self.assertIn("LAN Control", items["lights"]["detail"])
        self.assertFalse(items["spotify"]["done"])
        self.assertFalse(items["sound"]["done"])

    def test_a_home_set_up_is_done(self):
        rooms = [{"name": "Living Room", "lights": ["a"], "speakers": ["Kitchen Home"]}]
        space = {"plan": {"shapes": {"rooms": [{"name": "Living Room", "poly": [[0, 0], [1, 0], [1, 1]]}]}}, "lights": {"a": {}}}
        s = self.make({"jam_hidden_output": "CABLE"}, known=[1], rooms=SimpleNamespace(list=lambda: rooms),
                      space=SimpleNamespace(load=lambda: space), jam=SimpleNamespace(spotify=SimpleNamespace(signed_in=True)))
        with mock.patch.object(setup, "_outputs", return_value=(["CABLE Input (VB-Audio Virtual Cable)"], "")):
            items = {i["id"]: i for i in s.checklist()}
        for k in ("lights", "rooms", "map", "speakers", "spotify", "sound"):
            self.assertTrue(items[k]["done"], k)

    def test_devices_picked_are_kept_and_the_default_clears_them(self):
        import asyncio
        s = self.make()

        class Req:
            def __init__(self, body): self.body = body
            async def json(self): return self.body
        with mock.patch.object(setup, "_inputs", return_value=(["Mic, MME"], "Mic, MME")), \
                mock.patch.object(setup, "_outputs", return_value=(["Speakers"], "Speakers")):
            asyncio.run(s.h_devices_set(Req({"mic_device": "Mic, MME", "jam_room_output": "Speakers"})))
            self.assertEqual((self.cfg["mic_device"], self.cfg["jam_room_output"]), ("Mic, MME", "Speakers"))
            asyncio.run(s.h_devices_set(Req({"mic_device": ""})))
        self.assertNotIn("mic_device", self.cfg)
        self.assertEqual(self.cfg["jam_room_output"], "Speakers")

    def test_the_phone_gets_the_lan_address_and_a_qr_code(self):
        with mock.patch.object(setup.govee_lan, "local_ip", return_value="192.168.1.20"):
            url = self.make().link()
        self.assertEqual(url, "http://192.168.1.20:8765/party")
        svg = setup.HomeSetup.qr_svg(url)
        self.assertIn("<svg", svg)
        self.assertIn("viewBox", svg)                     # scales with its box, rather than sitting in a corner of it
        self.assertNotIn('width="', svg.split(">")[0])


if __name__ == "__main__":
    unittest.main()
