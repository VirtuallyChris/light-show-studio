"""The Energy built up to 100% over an hour (party_service: the phone remote's button under the slider): a step up as
each new song starts, to where the hour has got to. A stand-in for the service: nothing is saved."""

import unittest
from types import SimpleNamespace

from lightshow.studio.party_service import PartyService


def service(level, ramp):
    s = SimpleNamespace(party_energy=level, energy_ramp=ramp, RAMP_MINUTES=60, saved=0)
    s._save_level = lambda: setattr(s, "saved", s.saved + 1)
    s.step = lambda key, now: PartyService._ramp_step(s, key, now)
    return s


class TestPartyRamp(unittest.TestCase):
    def test_a_step_up_only_as_a_new_song_starts(self):
        s = service(0.4, {"from": 0.4, "start": 1000.0, "minutes": 60, "song": "a", "waiting": False})
        self.assertFalse(s.step("a", 1000.0 + 600))               # the same song: no change mid-song
        self.assertEqual(s.party_energy, 0.4)
        self.assertTrue(s.step("b", 1000.0 + 900))                # a quarter of the hour: a quarter of the way
        self.assertEqual(s.party_energy, 0.55)
        self.assertFalse(s.step("b", 1000.0 + 1800))              # still song b
        self.assertTrue(s.step("c", 1000.0 + 1800))
        self.assertEqual(s.party_energy, 0.7)

    def test_the_hour_up_it_ends_at_100(self):
        s = service(0.3, {"from": 0.3, "start": 0.0, "minutes": 60, "song": "a", "waiting": False})
        s.step("b", 3700.0)
        self.assertEqual(s.party_energy, 1.0)
        self.assertIsNone(s.energy_ramp)

    def test_the_song_playing_when_it_was_pressed_keeps_its_level(self):
        s = service(0.2, {"from": 0.2, "start": 0.0, "minutes": 60, "song": None, "waiting": True})
        self.assertFalse(s.step("a", 600.0))                      # (pressed before the party was playing)
        self.assertEqual(s.party_energy, 0.2)
        self.assertTrue(s.step("b", 1200.0))
        self.assertAlmostEqual(s.party_energy, 0.47, places=2)

    def test_never_down(self):
        s = service(0.6, {"from": 0.2, "start": 0.0, "minutes": 60, "song": "a", "waiting": False})
        self.assertFalse(s.step("b", 60.0))                       # where the hour is (0.21) is below the level now
        self.assertEqual(s.party_energy, 0.6)

    def test_nothing_without_a_build_up(self):
        s = service(0.5, None)
        self.assertFalse(s.step("b", 100.0))
        self.assertEqual((s.party_energy, s.saved), (0.5, 0))


if __name__ == "__main__":
    unittest.main()
