"""Every song at about the same volume: measuring loudness, the correction, and the relay applying it."""

import os
import shutil
import tempfile
import unittest

import numpy as np

from lightshow.studio.loudness import gain_db, measure
from lightshow.studio.relay import Relay

SR = 44100


def sine(db: float, seconds: float = 10.0, hz: float = 997.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    x = 10 ** (db / 20) * np.sin(2 * np.pi * hz * t)
    return np.stack([x, x], axis=1)


class TestMeasure(unittest.TestCase):
    def test_a_full_scale_tone_in_both_channels_is_zero_lufs(self):
        # BS.1770: a 997 Hz tone at 0 dBFS in one channel reads -3.01 LUFS; in both, 0
        m = measure(sine(-20.0), SR)
        self.assertAlmostEqual(m["lufs"], -20.0, delta=0.3)
        self.assertAlmostEqual(m["peak"], -20.0, delta=0.1)
        self.assertAlmostEqual(m["p99"], -20.0, delta=0.1)

    def test_int16_and_silence(self):
        x = (sine(-12.0) * 32767).astype(np.int16)
        self.assertAlmostEqual(measure(x, SR)["lufs"], -12.0, delta=0.3)
        self.assertIsNone(measure(np.zeros((10 * SR, 2), dtype=np.int16), SR))
        self.assertIsNone(measure(x[:SR], SR))                                   # too short to judge

    def test_quiet_gaps_do_not_drag_a_song_down(self):
        x = np.concatenate([sine(-14.0, 20.0), np.zeros((20 * SR, 2))])           # half silence: gated out
        self.assertAlmostEqual(measure(x, SR)["lufs"], -14.0, delta=0.3)


class TestGain(unittest.TestCase):
    def test_a_quiet_song_with_room_is_turned_up_to_the_target(self):
        self.assertAlmostEqual(gain_db({"lufs": -15.0, "p99": -10.0, "peak": -6.0}, -9.0), 6.0)

    def test_the_boost_stops_where_its_peaks_would(self):
        # 99 in 100 stretches under the ceiling untouched: its p99 at -3 allows 2.7 dB
        self.assertAlmostEqual(gain_db({"lufs": -15.0, "p99": -3.0, "peak": -0.1}, -9.0), 2.7)
        # and the limiter takes 3 dB off the top peaks at most
        self.assertAlmostEqual(gain_db({"lufs": -20.0, "p99": -12.0, "peak": -4.0}, -9.0), 6.7)
        self.assertLessEqual(gain_db({"lufs": -30.0, "p99": -25.0, "peak": -20.0}, -9.0), 8.0)   # never more than 8 dB

    def test_loud_songs_come_down_gently_and_small_changes_are_left(self):
        self.assertAlmostEqual(gain_db({"lufs": -5.0, "p99": -0.5, "peak": 0.0}, -9.0), -2.0)
        self.assertAlmostEqual(gain_db({"lufs": 0.0, "p99": 0.0, "peak": 0.0}, -9.0), -3.0)
        self.assertEqual(gain_db({"lufs": -9.3, "p99": -1.0, "peak": 0.0}, -9.0), 0.0)


class TestRelayLevels(unittest.TestCase):
    def relay_with(self, x):
        r = Relay(sr=SR, channels=2, capacity_s=60.0)
        r.write((x * 32767).astype(np.int16))
        return r

    def read_all(self, stream, frames):
        out = []
        while sum(len(b) for b in out) < frames * 4:
            out.append(stream.read(8192, timeout=0))
        return np.frombuffer(b"".join(out)[:frames * 4], dtype=np.int16).reshape(-1, 2).astype(np.float64) / 32767

    def test_a_song_plays_at_its_level_gliding_into_it(self):
        r = self.relay_with(sine(-20.0, 8.0))
        r.set_level(2.0, 2.0)                                                      # the second song, from 2 s: +6 dB
        y = self.read_all(r.open_stream(0.0, pace=False), 8 * SR)
        rms = lambda a, b: float(np.sqrt(np.mean(y[int(a * SR):int(b * SR)] ** 2)))
        self.assertAlmostEqual(rms(0.5, 1.9) / (10 ** (-20 / 20) / np.sqrt(2)), 1.0, delta=0.02)   # untouched before
        self.assertAlmostEqual(rms(4.0, 8.0) / rms(0.5, 1.9), 2.0, delta=0.03)                      # twice as loud after
        self.assertGreater(rms(2.05, 2.15), rms(0.5, 1.9) * 1.05)                                    # on its way up...
        self.assertLess(rms(2.05, 2.15), rms(4.0, 8.0) * 0.95)                                       # ...not jumped

    def test_a_stream_opened_inside_a_song_starts_at_its_level(self):
        r = self.relay_with(sine(-20.0, 6.0))
        r.set_level(0.0, 0.5)
        y = self.read_all(r.open_stream(3.0, pace=False), SR)
        self.assertAlmostEqual(float(np.abs(y).max()), 0.5 * 10 ** (-20 / 20), delta=0.003)

    def test_a_boosted_songs_peaks_stay_under_the_ceiling(self):
        x = sine(-12.0, 6.0)
        x[3 * SR:3 * SR + 200] *= 3.5                                              # one hit near full scale
        r = self.relay_with(x)
        r.set_level(0.0, 10 ** (6 / 20))
        y = self.read_all(r.open_stream(0.0, pace=False), 6 * SR)
        self.assertLessEqual(float(np.abs(y).max()), 10 ** (-0.3 / 20) + 1e-3)
        body = float(np.abs(y[int(1.0 * SR):int(2.5 * SR)]).max())               # the music around it untouched
        self.assertAlmostEqual(body, 10 ** (-6 / 20), delta=0.01)

    def test_switched_off_everything_plays_as_it_is(self):
        r = self.relay_with(sine(-20.0, 3.0))
        y = self.read_all(r.open_stream(0.0, pace=False), 3 * SR)
        self.assertAlmostEqual(float(np.abs(y).max()), 10 ** (-20 / 20), delta=0.002)


class TestJamLevels(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_levels_are_kept_and_what_played_wins_over_the_copy(self):
        from types import SimpleNamespace
        from lightshow.studio.jam import JamEngine, JamLibrary, JamSettings
        lib = JamLibrary(self.dir)
        lib.save_level("abc", "copy", {"lufs": -15.0, "p99": -10.0, "peak": -6.0})
        self.assertEqual(JamLibrary(self.dir).level("abc")["copy"]["lufs"], -15.0)           # on disk
        e = JamEngine.__new__(JamEngine)
        e.library, e.cfg = lib, JamSettings()
        g, why = e._level_for("abc")
        self.assertAlmostEqual(g, 10 ** (6 / 20), places=3)
        self.assertIn("YouTube copy", why)
        lib.save_level("abc", "relay", {"lufs": -12.0, "p99": -10.0, "peak": -6.0})
        g, why = e._level_for("abc")
        self.assertAlmostEqual(g, 10 ** (3 / 20), places=3)
        self.assertIn("as it played", why)
        self.assertEqual(e._level_for("never-heard"), (1.0, ""))
        e.cfg = JamSettings(even_levels=False)
        self.assertEqual(e._level_for("abc"), (1.0, ""))

    def test_the_next_songs_level_is_set_where_this_one_ends(self):
        import threading
        from types import SimpleNamespace
        from lightshow.studio.jam import JamEngine, JamLibrary, JamSettings, JamTrack
        lib = JamLibrary(self.dir)
        lib.save_level("next", "relay", {"lufs": -15.0, "p99": -10.0, "peak": -6.0})
        e = JamEngine.__new__(JamEngine)
        e.library, e.cfg, e.lock, e._planned_level = lib, JamSettings(), threading.RLock(), None
        e.relay = Relay(sr=SR, channels=2, capacity_s=600.0)
        e.relay.write(np.zeros((100 * SR, 2), dtype=np.int16))                    # 100 s captured
        track = SimpleNamespace(id="now", duration=110.0, name="Now")
        e.tracks, e.queue = [JamTrack(track, 0.0, "learned", serial=1)], [{"id": "next"}]
        e.plan_levels()
        self.assertEqual(len(e.relay.levels), 1)
        self.assertAlmostEqual(e.relay.levels[0][0] / SR, 109.7, delta=0.01)
        self.assertAlmostEqual(e.relay.levels[0][1], 10 ** (6 / 20), places=3)
        e.plan_levels()
        self.assertEqual(len(e.relay.levels), 1)                                  # once per song


if __name__ == "__main__":
    unittest.main()
