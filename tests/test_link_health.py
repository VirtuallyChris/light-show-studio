r"""The lights' frame rate follows their Wi-Fi: run with  .venv\Scripts\python -m unittest tests.test_link_health -v

Ping results are made up and time is simulated: no light is pinged or driven.
"""

import unittest

from lightshow.studio.link_health import FpsGovernor, LinkMonitor


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


class TestRangePerLight(unittest.TestCase):
    def test_poles_at_15_and_strips_from_30(self):
        from types import SimpleNamespace
        out = SimpleNamespace(devices=[SimpleNamespace(ip="pole"), SimpleNamespace(ip="strip")], enabled=True, active=True, fps_by_ip={}, max_fps=0)
        mon = LinkMonitor(out, 15, 40, log=lambda m: None, range_for=lambda ip: (15, 15, 15) if ip == "pole" else (15, 40, 30))
        mon.ping = lambda ip, timeout_ms=300: 5.0
        mon.tick(); mon.tick()
        self.assertEqual(out.fps_by_ip, {"pole": 15, "strip": 30})
        self.assertEqual(mon.governors["pole"].levels, [15])              # a pole never climbs


class TestDropAndRejoin(unittest.TestCase):
    """A light far behind even at its lowest rate (the party night: a pole at 15 losing 70% of its pings,
    a frame every 2 s) is dropped, kept black and sent nothing until its Wi-Fi answers, then rejoins."""

    def weak(self, k=iter(range(10**6))):
        return None if next(k) % 10 < 7 else 240.0

    def test_far_behind_at_its_lowest_rate_is_stuck(self):
        c = Clock(); g = FpsGovernor(15, 15, start=15, clock=c)
        run(g, c, 10, self.weak)
        self.assertIn("pings lost", g.stuck())
        g.fresh()
        run(g, c, 10, 8.0)
        self.assertEqual(g.stuck(), "")

    def test_a_strip_steps_down_first(self):
        c = Clock(); g = FpsGovernor(15, 30, start=30, clock=c)
        g.report(None); g.report(None); g.report(None); g.report(None); g.report(None)
        self.assertEqual(g.stuck(), "")                                   # at 30: a step down comes first

    def monitor(self, pings):
        import threading
        from types import SimpleNamespace
        done, calls = threading.Event(), []

        def rejoin(ip, link_back):
            calls.append(ip)
            done.set()
            return True
        out = SimpleNamespace(devices=[SimpleNamespace(ip="pole")], enabled=True, active=True, fps_by_ip={}, max_fps=0, rejoin=rejoin)
        c = Clock()
        mon = LinkMonitor(out, 15, 30, log=lambda m: None, clock=c, range_for=lambda ip: (15, 15, 15))
        mon.ping = lambda ip, timeout_ms=300: next(pings)
        return mon, c, done, calls

    def test_dropped_once_then_not_again_for_a_while(self):
        k = iter(range(10**6))
        mon, c, done, calls = self.monitor(iter(lambda: None if next(k) % 10 < 7 else 240.0, 0))
        for _ in range(20):
            c.t += 1.5
            mon.tick()
        done.wait(2)
        for _ in range(20):                                               # still weak, half a minute on
            c.t += 1.5
            mon.tick()
        self.assertEqual(calls, ["pole"])

    def test_it_waits_for_quick_replies_but_not_forever(self):
        mon, c, done, calls = self.monitor(iter([None, None, 300.0, 5.0, 6.0, 4.0] + [5.0] * 100))
        slept = []
        mon.sleep = slept.append
        self.assertTrue(mon._link_back("pole"))
        self.assertGreaterEqual(sum(slept), LinkMonitor.DRAIN_S)          # at least long enough to clear
        mon.ping = lambda ip, timeout_ms=300: None
        slept.clear()
        self.assertFalse(mon._link_back("pole"))
        self.assertAlmostEqual(sum(slept), LinkMonitor.REJOIN_MAX_S)      # and back in the show after this

    def test_black_and_nothing_sent_until_it_rejoins(self):
        from unittest import mock
        from lightshow.studio import server

        class Light:
            ip, mac, device_id = "pole", "", ""

            def __init__(self):
                self.got = []

            def razer_leds(self, cols, gradient=False):
                self.got.append(("frame", cols[0]))

            def razer(self, on):
                self.got.append(("realtime", on))

        light = Light()
        with mock.patch.object(server, "load_config", lambda: {}):
            o = server.LightOutput(enabled=True)
            o.devices, o.active = [light], True
            o.frame([[200, 0, 0] * 10], ["pole"])

            def link_back(ip):
                for _ in range(5):                                        # the show plays on meanwhile
                    o.last_frame = server.time.monotonic()
                    o.frame([[0, 255, 0] * 10], ["pole"])
                return True
            self.assertTrue(o.rejoin("pole", link_back))
            o.frame([[0, 0, 255] * 10], ["pole"])
        self.assertEqual(light.got, [("frame", (200, 0, 0)), ("frame", (0, 0, 0)), ("realtime", True),
                                     ("frame", (0, 0, 0)), ("frame", (0, 0, 255))])


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
