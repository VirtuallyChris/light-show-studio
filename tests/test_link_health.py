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
        seen = run(g, c, 30, 10.0)
        self.assertEqual(seen[-1], 60)                                    # and then back up

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
