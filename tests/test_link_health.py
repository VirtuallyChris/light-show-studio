"""The lights' frame rate follows their Wi-Fi: run with  .venv\Scripts\python -m unittest tests.test_link_health -v

Ping results are made up and time is simulated: no light is pinged or driven.
"""

import unittest

from lightshow.studio.link_health import FpsGovernor


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def run(gov, clock, seconds, rtt, every=0.5):
    """Feed one ping result every half second for `seconds`; returns the frame rates seen."""
    seen = []
    for _ in range(int(seconds / every)):
        clock.t += every
        gov.report(rtt() if callable(rtt) else rtt)
        seen.append(gov.decide())
    return seen


class TestFpsGovernor(unittest.TestCase):
    def test_clean_wifi_climbs_to_60_and_no_further(self):
        c = Clock(); g = FpsGovernor(15, 60, start=24, clock=c)
        seen = run(g, c, 120, 12.0)
        self.assertEqual(seen[-1], 60)
        self.assertEqual(max(seen), 60)
        self.assertEqual(sorted(set(seen)), [24, 30, 40, 50, 60])         # one step at a time
        first_up = next(i for i, f in enumerate(seen) if f > 24)
        self.assertGreaterEqual(first_up * 0.5, 14.5)                     # not before 15 s of clean replies

    def test_a_lost_ping_steps_down_at_once_and_holds(self):
        c = Clock(); g = FpsGovernor(15, 60, start=60, clock=c)
        run(g, c, 20, 10.0)
        self.assertEqual(g.fps, 60)
        g.report(None); c.t += 0.5
        self.assertEqual(g.decide(), 50)                                  # straight away
        seen = run(g, c, 25, 10.0)                                        # clean again, but held
        self.assertEqual(max(seen), 50)
        seen = run(g, c, 120, 10.0)
        self.assertEqual(max(seen), 50)                                   # and it choked at 60: not tried again for a while
        c.t += FpsGovernor.CEILING_S
        seen = run(g, c, 40, 10.0)
        self.assertEqual(seen[-1], 60)                                    # ten minutes on, it may try again

    def test_a_light_settles_where_it_can_keep_up(self):
        # a light that keeps up at 30 but not at 40: it climbs, chokes at 40 once, and stays at 30
        c = Clock(); g = FpsGovernor(15, 40, start=24, clock=c)
        downs = 0
        for _ in range(600):                                              # five minutes, a ping every half second
            g.report(150.0 if g.fps >= 40 else 10.0)
            c.t += 0.5
            before = g.fps
            if g.decide() < before:
                downs += 1
        self.assertEqual(g.fps, 30)
        self.assertEqual(downs, 1)                                        # one stumble, not one a minute

    def test_slow_replies_step_down_and_a_bad_patch_goes_to_the_floor(self):
        c = Clock(); g = FpsGovernor(15, 60, start=40, clock=c)
        seen = run(g, c, 3, 120.0)
        self.assertLess(seen[-1], 40)
        seen = run(g, c, 20, lambda: None)                                # everything lost
        self.assertEqual(seen[-1], 15)
        self.assertEqual(min(seen), 15)                                   # never under the floor

    def test_one_bad_moment_is_one_step(self):
        c = Clock(); g = FpsGovernor(15, 60, start=60, clock=c)
        run(g, c, 20, 10.0)
        g.report(None); c.t += 0.5; g.decide()
        seen = run(g, c, 1.5, 10.0)                                       # the lost ping is still in the window
        self.assertEqual(min(seen), 50)


if __name__ == "__main__":
    unittest.main()


class TestOneLoss(unittest.TestCase):
    def test_a_single_lost_ping_never_takes_two_steps(self):
        c = Clock(); g = FpsGovernor(15, 60, start=60, clock=c)
        run(g, c, 20, 10.0)
        g.report(None); c.t += 0.5; g.decide()
        seen = run(g, c, 5, 10.0)
        self.assertEqual(min(seen), 50)


class TestEachLightItsOwnRate(unittest.TestCase):
    """A pole at the edge of the Wi-Fi never holds back a strip next to the router."""

    def test_each_light_settles_at_its_own_rate(self):
        from types import SimpleNamespace
        from lightshow.studio.link_health import LinkMonitor
        clock = Clock()
        out = SimpleNamespace(enabled=True, active=True, devices=[SimpleNamespace(ip="pole"), SimpleNamespace(ip="strip")],
                              fps_by_ip={}, max_fps=24)
        mon = LinkMonitor(out, 15, 60, log=lambda m: None, clock=clock)
        mon.ping = lambda ip, timeout: 120.0 if ip == "pole" else 9.0          # the pole answers slowly, the strip fast
        for _ in range(int(120 / 0.25)):
            clock.t += 0.25
            mon.tick()
        self.assertEqual(out.fps_by_ip["pole"], 15)
        self.assertEqual(out.fps_by_ip["strip"], 60)
        self.assertEqual(out.max_fps, 60)
        self.assertEqual(set(mon.to_json()["lights"]), {"pole", "strip"})

    def test_frames_reach_each_light_at_its_own_rate(self):
        from unittest import mock
        from lightshow.studio import server

        class Light:
            def __init__(self, ip):
                self.ip, self.mac, self.device_id, self.got = ip, "", "", 0

            def razer_leds(self, cols, gradient=False):
                self.got += 1

        clock = Clock()
        pole, strip = Light("pole"), Light("strip")
        with mock.patch.object(server, "load_config", lambda: {}), mock.patch.object(server.time, "monotonic", clock):
            o = server.LightOutput(enabled=False)
            o.devices, o.active, o.max_fps = [pole, strip], True, 24
            o.fps_by_ip = {"pole": 15, "strip": 60}
            for _ in range(600):                                           # the page's 60 frames a second, for 10 s
                clock.t += 1 / 60
                o.frame([[200, 0, 0], [0, 0, 200]], ["pole", "strip"])
        self.assertAlmostEqual(pole.got / 10, 15, delta=1.5)
        self.assertAlmostEqual(strip.got / 10, 60, delta=6)
