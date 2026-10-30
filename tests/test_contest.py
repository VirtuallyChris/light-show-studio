"""Costume contest mode (the Party Hub's switch): the music down to 20% in the room's stream, fading over 5 s and
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


class TestContestSwitch(unittest.TestCase):
    def service(self):
        calls = []
        s = SimpleNamespace(contest={"on": False, "at": -1e9}, CONTEST_GAIN=0.2, CONTEST_FADE_S=5.0,
                            engine=SimpleNamespace(duck_music=lambda to, secs: calls.append((to, secs))))
        s.contest_status = lambda: JamService.contest_status(s)
        s.set = lambda on: JamService._contest(s, on)
        return s, calls

    def test_on_and_off_turn_the_music_down_and_up(self):
        s, calls = self.service()
        self.assertTrue(s.set(True)["contest"]["on"])
        s.set(True)                                                     # (again: nothing more)
        s.set(False)
        self.assertEqual(calls, [(0.2, 5.0), (1.0, 5.0)])
        self.assertFalse(s.contest_status()["on"])
        self.assertLess(s.contest_status()["age"], 1.0)                 # (the studio page fades the lights from then)

    def test_without_a_jam_it_is_only_remembered(self):
        s, calls = self.service()
        s.engine = None
        self.assertTrue(s.set(True)["contest"]["on"])


class TestStopParty(unittest.TestCase):
    def service(self, playing):
        calls = []
        now = SimpleNamespace(track=object(), playing=playing)
        s = SimpleNamespace(stop_party={"on": False, "at": -1e9, "paused": False}, engine=SimpleNamespace(now=now))
        s._playback_fn = lambda action: (lambda: calls.append(action))
        s._music_playing = lambda: JamService._music_playing(s)
        s.stop_status = lambda: JamService.stop_status(s)
        s.set = lambda on: JamService._stop_party(s, on)
        return s, calls

    def test_the_music_pauses_while_it_is_on_and_plays_on_after(self):
        s, calls = self.service(playing=True)
        st = s.set(True)["stop_party"]
        self.assertEqual((st["on"], st["fade"]), (True, 0))            # (the lights: orange at once)
        s.set(True)
        s.set(False)
        self.assertEqual(calls, ["pause", "resume"])

    def test_music_paused_already_stays_paused_after(self):
        s, calls = self.service(playing=False)
        s.set(True)
        s.set(False)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
