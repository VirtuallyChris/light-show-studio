"""Jam output on this PC: run with  .venv\\Scripts\\python -m unittest tests.test_local_output -v

The sound device is stood in for by a fake that keeps time like one; nothing is played.
"""

import time
import unittest

import numpy as np

from lightshow.studio.jam_service import JamService
from lightshow.studio.local_output import LocalOutput
from lightshow.studio.relay import Relay


class FakeDevice:
    """Accepts audio at the real rate, the way a sound card's play() blocks until there is room."""

    def __init__(self, sr):
        self.sr, self.frames, self.started = sr, [], None

    def __call__(self, device_name, sr, channels):
        self.device_name, self.channels = device_name, channels
        return self

    def __enter__(self):
        self.started = time.monotonic()
        return self

    def __exit__(self, *a):
        pass

    def play(self, frames):
        self.frames.append(np.array(frames))
        due = self.started + sum(len(f) for f in self.frames) / self.sr - 0.05     # a small device buffer
        time.sleep(max(0.0, due - time.monotonic()))


class TestLocalOutput(unittest.TestCase):
    def setUp(self):
        self.sr = 8000
        self.relay = Relay(sr=self.sr, channels=2, capacity_s=30)
        ramp = ((np.arange(self.sr * 6) % 20000) + 1).astype(np.int16)             # never zero, so silence stands out
        self.relay.write(np.stack([ramp, ramp], axis=1))
        self.dev = FakeDevice(self.sr)
        self.out = LocalOutput("Headphones (Test)", offset_s=0.08, player_factory=self.dev)

    def tearDown(self):
        self.out.stop()

    def played(self):
        return np.concatenate(self.dev.frames) if self.dev.frames else np.zeros((0, 2))

    def test_plays_the_stream_in_order_and_keeps_time(self):
        self.out.start_stream(self.relay.open_stream(1.0))                        # the room starts 1 s into the capture
        time.sleep(0.8)
        pos = self.out.position()
        self.assertAlmostEqual(pos, 0.8, delta=0.15)                               # keeps time with the clock
        got = (self.played()[:, 0] * 32768).round().astype(int)
        self.assertEqual(got[0], (self.sr * 1) % 20000 + 1)                          # starts where it was asked
        self.assertTrue(np.all(np.diff(got[: self.sr // 2]) == 1))                  # in order, nothing skipped
        self.assertEqual(self.dev.device_name, "Headphones (Test)")
        self.assertEqual((self.out.offset, self.out.name), (0.08, "This PC: Headphones (Test)"))

    def test_pause_holds_the_place_and_resume_carries_on(self):
        stream = self.relay.open_stream(0.0)
        self.out.start_stream(stream)
        time.sleep(0.4)
        self.out.pause()
        time.sleep(0.1)
        held, cursor = self.out.position(), stream.cursor
        time.sleep(0.5)
        self.assertAlmostEqual(self.out.position(), held, delta=0.01)             # no time passes while paused
        self.assertEqual(stream.cursor, cursor)                                     # nothing was read from the stream
        self.out.resume()
        time.sleep(0.4)
        self.assertGreater(self.out.position(), held + 0.25)

    def test_a_new_stream_starts_over_and_stop_ends_the_player(self):
        self.out.start_stream(self.relay.open_stream(0.0))
        time.sleep(0.3)
        first = self.out.thread
        self.out.start_stream(self.relay.open_stream(3.0))                        # a skip
        time.sleep(0.2)
        self.assertFalse(first.is_alive())
        self.assertLess(self.out.position(), 0.35)
        self.out.stop()
        self.assertIsNone(self.out.thread)

    def test_a_broken_device_is_reported_not_raised(self):
        def broken(*a):
            raise RuntimeError("device unplugged")
        out = LocalOutput(None, player_factory=broken)
        out.start_stream(self.relay.open_stream(0.0))
        time.sleep(0.2)
        self.assertIn("device unplugged", out.error)
        out.stop()


class TestSameDevice(unittest.TestCase):
    def test_the_jam_cannot_play_on_the_output_it_records(self):
        same = JamService._same_device
        self.assertTrue(same("Speakers (Realtek)", "Speakers (Realtek)"))
        self.assertTrue(same(None, None))
        self.assertFalse(same("Monitor (NVIDIA)", "Headphones (USB)"))


if __name__ == "__main__":
    unittest.main()
