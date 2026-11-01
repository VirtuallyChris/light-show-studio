"""The Party Hub's modes: the music down in the room's stream (or paused), fading over each mode's own time and
back, the speakers' own volume left alone. Fakes only: nothing is played, no lights are driven.
Run with  .venv\\Scripts\\python -m unittest tests.test_contest -v"""

import shutil
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from lightshow.studio.jam_service import JamService
from lightshow.studio.relay import Relay
from tests.test_jam import SR, Clock, Sim, song


def read_all(s, frames):
    x = np.zeros((0,), dtype=np.int16)
    while len(x) < frames:
        got = s.read(frames - len(x), timeout=0)
        if not got:
            break
        x = np.concatenate([x, np.frombuffer(got, dtype=np.int16).reshape(-1, 2)[:, 0]])
    return x


class TestRelayDuck(unittest.TestCase):
    def setUp(self):
        self.relay = Relay(sr=1000, channels=2, capacity_s=60, clock=Clock())
        self.relay.write(np.full((30000, 2), 1000, dtype=np.int16))

    def test_the_music_fades_down_from_where_it_is_and_stays_down(self):
        self.relay.set_duck(5.0, 5.0, 0.2)
        x = read_all(self.relay.open_stream(0.0, pace=False), 20000)
        self.assertTrue(np.all(x[:5000] == 1000))                       # before: as it was
        self.assertAlmostEqual(x[7500], 600, delta=10)                  # halfway: halfway down
        self.assertTrue(np.all(x[10000:20000] == 200))                  # then a fifth of itself, on and on

    def test_every_stream_plays_it_the_same_a_restart_too(self):
        self.relay.set_duck(5.0, 5.0, 0.2)
        x = read_all(self.relay.open_stream(12.0, pace=False), 3000)
        self.assertTrue(np.all(x == 200))

    def test_back_up_from_where_it_got_to(self):
        self.relay.set_duck(5.0, 5.0, 0.2)
        self.relay.set_duck(7.5, 5.0, 1.0)                              # turned around halfway
        x = read_all(self.relay.open_stream(7.0, pace=False), 10000)
        self.assertAlmostEqual(x[500], 600, delta=10)                   # from where it was, no jump
        self.assertGreater(x[3000], 700)
        self.assertTrue(np.all(x[5500:10000] == 1000))                  # all the way back


class TestEngineDuck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        a, ta = song(128.0, 1)
        self.sim = Sim(self.d, target=10.0)
        self.sim.play(a, ta)
        self.sim.run(14.0, analyse=False)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_playing_the_speakers_get_a_fresh_stream_with_the_fade_in_it(self):
        e, out = self.sim.engine, self.sim.out
        starts, room = out.starts, e.room_capture_time()
        e.duck_music(0.2, 5.0)
        self.assertEqual(out.starts, starts + 1)                        # what they held (up to 20 s) is dropped
        self.assertAlmostEqual(out.stream.cursor / SR, room, delta=0.05)   # from where the room is: nothing skipped
        s, n, g0, g1 = self.sim.relay.duck
        self.assertAlmostEqual(s / SR, room, delta=0.05)
        self.assertEqual((n, g0, g1), (5 * SR, 1.0, 0.2))

    def test_paused_it_is_simply_down_when_the_music_plays_again(self):
        e, out = self.sim.engine, self.sim.out
        self.sim.spotify.pause()
        self.sim.run(2.0, analyse=False)
        starts = out.starts
        e.duck_music(0.2, 5.0)
        self.assertEqual(out.starts, starts)                            # nothing heard: no fresh stream
        self.assertEqual(self.sim.relay.duck[1:], (1, 1.0, 0.2))        # at once

    def test_a_cue_holding_the_room_is_left_to_play(self):
        e, out = self.sim.engine, self.sim.out
        e.cue_insert(np.zeros((SR * 4, 2), dtype=np.int16), 2.0)
        starts = out.starts
        e.duck_music(0.2, 5.0)
        self.assertEqual(out.starts, starts)                            # the doorbell's song plays on
        self.assertIsNotNone(out.stream.cut)
        self.assertAlmostEqual(self.sim.relay.duck[0] / SR, e.held_at, delta=0.05)   # the music after it comes back down


MODES = [{"id": "contest", "name": "Costume contest", "music": 0.2, "pause": False, "fade": 5.0, "color": "#ff6000", "confirm": True},
         {"id": "stop", "name": "STOP PARTY", "music": 0.0, "pause": True, "fade": 0.0, "color": "#ffaa5a", "confirm": True},
         {"id": "quiet", "name": "Speeches", "music": 0.5, "pause": False, "fade": 2.0, "color": "#ffffff", "confirm": False}]


class TestModes(unittest.TestCase):
    """The Party Hub's modes, each the user's own: its music level (or a pause), its fade, its color."""

    def service(self, playing=True, modes=MODES):
        calls = []
        self.cfg = {"party_modes": [dict(m) for m in modes]}
        now = SimpleNamespace(track=object(), playing=playing)
        s = SimpleNamespace(mode_on={}, MAX_MODES=JamService.MAX_MODES, DEFAULT_MODES=JamService.DEFAULT_MODES,
                            load_config=lambda: dict(self.cfg), save_config=lambda c: self.cfg.update(c),
                            engine=SimpleNamespace(now=now, duck_music=lambda to, secs: calls.append(("duck", to, secs))))
        s._cover_color, s.clean_modes = JamService._cover_color, JamService.clean_modes
        for f in ("modes", "modes_status", "_music_gain", "_music_playing", "_mode", "_set_modes"):
            setattr(s, f, getattr(JamService, f).__get__(s))
        s._playback_fn = lambda action: (lambda: calls.append(action))
        return s, calls

    def status(self, s, mid):
        return next(m for m in s.modes_status() if m["id"] == mid)

    def test_a_mode_turns_the_music_down_to_its_own_level_over_its_own_fade(self):
        s, calls = self.service()
        s._mode("contest", True)
        s._mode("contest", True)                                        # (again: nothing more)
        s._mode("contest", False)
        self.assertEqual(calls, [("duck", 0.2, 5.0), ("duck", 1.0, 5.0)])

    def test_two_on_the_quietest_wins_and_the_other_level_comes_back(self):
        s, calls = self.service()
        s._mode("quiet", True)
        s._mode("contest", True)
        s._mode("contest", False)
        self.assertEqual(calls, [("duck", 0.5, 2.0), ("duck", 0.2, 5.0), ("duck", 0.5, 5.0)])

    def test_a_pause_mode_pauses_and_plays_on_only_what_it_paused(self):
        s, calls = self.service()
        st = s._mode("stop", True)["modes"][1]
        self.assertEqual((st["on"], st["fade"], st["color"]), (True, 0.0, "#ffaa5a"))
        s._mode("stop", False)
        self.assertEqual(calls, ["pause", "resume"])
        s, calls = self.service(playing=False)
        s._mode("stop", True); s._mode("stop", False)
        self.assertEqual(calls, [])

    def test_its_own_color_each_time_and_another_picked_while_on(self):
        s, calls = self.service()
        self.assertEqual(s._mode("contest", True)["modes"][0]["color"], "#ff6000")
        s._mode("contest", None, "#8000ff")
        self.assertEqual((self.status(s, "contest")["color"], self.status(s, "contest")["own"]), ("#8000ff", "#ff6000"))
        self.assertEqual(calls, [("duck", 0.2, 5.0)])                   # (a color alone leaves the music be)
        s._mode("contest", False)
        self.assertEqual(s._mode("contest", True)["modes"][0]["color"], "#ff6000")

    def test_modes_are_made_changed_and_taken_away_on_the_hub(self):
        s, calls = self.service()
        s._mode("quiet", True)
        out = s._set_modes([MODES[0], MODES[1], {"name": "Toast", "music": 0.3, "color": "#FF0000"}])["modes"]
        self.assertEqual([m["name"] for m in out], ["Costume contest", "STOP PARTY", "Toast"])
        self.assertEqual((out[2]["color"], out[2]["confirm"], out[2]["fade"]), ("#ff0000", True, 5.0))
        self.assertNotIn("quiet", s.mode_on)                             # (taken away while on: switched off first)
        self.assertEqual(calls[-1], ("duck", 1.0, 1.0))

    def test_a_new_home_starts_with_two_plain_modes(self):
        s, _ = self.service()
        self.cfg.pop("party_modes")
        self.assertEqual([m["id"] for m in s.modes()], ["announce", "stop"])

    def test_only_a_real_color_is_taken(self):
        self.assertEqual(JamService._cover_color(" #80FF00 "), "#80ff00")
        for bad in ("red", "#12345", "#gggggg", None, "javascript:x"):
            self.assertIsNone(JamService._cover_color(bad))

    def test_levels_and_fades_are_kept_in_range(self):
        m = JamService.clean_modes([{"id": "A B!", "music": 7, "fade": -2}, {"id": "a-b"}])
        self.assertEqual((m[0]["id"], m[0]["music"], m[0]["fade"]), ("ab", 1.0, 0.0))
        self.assertNotEqual(m[1]["id"], "a-b") if m[0]["id"] == "a-b" else None


if __name__ == "__main__":
    unittest.main()
