"""The kitchen's meteor tubes (H6821, 240 LEDs in one frame) and big changes sent the moment they are drawn."""

import base64
import json
import unittest
from unittest import mock

from lightshow import govee_lan


class TestLongFrames(unittest.TestCase):
    def sent(self, n):
        d = govee_lan.GoveeDevice(ip="192.168.1.206")
        got = []
        with mock.patch.object(d, "send", lambda cmd, data=None: got.append((cmd, data))):
            d.razer_leds([(i % 256, 0, 255 - i % 256) for i in range(n)])
        return base64.b64decode(got[0][1]["pt"])

    def test_every_led_of_the_tubes_goes_in_one_packet(self):
        pkt = self.sent(240)
        self.assertEqual(pkt[0], 0xBB)
        self.assertEqual((pkt[1] << 8) | pkt[2], 2 + 3 * 240)           # the length takes two bytes
        self.assertEqual(pkt[3], 0xB0)
        self.assertEqual(pkt[5], 240)                                   # one color for each LED
        cs = 0
        for b in pkt[:-1]:
            cs ^= b
        self.assertEqual(pkt[-1], cs)

    def test_a_light_with_fewer_segments_gets_the_packet_it_always_did(self):
        pkt = self.sent(50)
        self.assertEqual(list(pkt[:4]), [0xBB, 0x00, 2 + 150, 0xB0])

    def test_the_tubes_are_known(self):
        self.assertEqual(govee_lan.SKU_SEGMENTS["H6821"], 240)
        self.assertEqual(govee_lan.SKU_FPS["H6821"], 15)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestBigChangesGoAtOnce(unittest.TestCase):
    def run_frames(self, frames, fps=15):
        from lightshow.studio import server

        class Light:
            def __init__(self):
                self.ip, self.mac, self.device_id, self.got = "tubes", "", "", []

            def razer_leds(self, cols, gradient=False):
                self.got.append((clock.t, cols[0]))

        clock = Clock()
        light = Light()
        with mock.patch.object(server, "load_config", lambda: {}), mock.patch.object(server.time, "monotonic", clock):
            o = server.LightOutput(enabled=False)
            o.devices, o.active, o.fps_by_ip = [light], True, {"tubes": fps}
            for f in frames:                                             # the page's 60 frames a second
                clock.t += 1 / 60
                o.frame([f], ["tubes"])
        return light.got

    def test_a_strobe_lands_on_time_at_15_frames_a_second(self):
        white, black = [255, 255, 255] * 20, [0, 0, 0] * 20
        frames = [white if (k // 6) % 2 == 0 else black for k in range(120)]      # 5 flashes a second for 2 s
        got = self.run_frames(frames)
        changes = [t for (t, c), (_, p) in zip(got[1:], got) if c != p]
        self.assertEqual(len(changes), 19)                              # every flash on and off arrives...
        edges = [1000.0 + (k + 1) / 60 for k in range(1, 120) if frames[k] != frames[k - 1]]
        for e, t in zip(edges, changes):
            self.assertAlmostEqual(t, e, delta=1e-6)                   # ...in the very frame it was drawn

    def test_a_steady_show_stays_at_its_rate(self):
        frames = [[(k * 3) % 256, 0, 100] * 20 for k in range(600)]      # small changes, 10 s
        self.assertAlmostEqual(len(self.run_frames(frames)) / 10, 15, delta=1.5)


if __name__ == "__main__":
    unittest.main()
