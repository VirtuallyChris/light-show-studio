"""Recording and playback in their own processes: run with  .venv\\Scripts\\python -m unittest tests.test_audio_io -v

The sound card is stood in for (a ramp generator and a sink that keeps real
time), so nothing is played or recorded. The point is the busy studio: three
threads of pure Python work run beside the audio, the kind of load that
stopped the in-process player altogether.
"""

import threading
import time
import unittest

import numpy as np

from lightshow.studio.audio_io import FAKE_SINK, FAKE_SOURCE, ProcessCapture, ProcessOutput
from lightshow.studio.relay import Relay


class Busy:
    def __enter__(self):
        self.stop = threading.Event()

        def burn():
            while not self.stop.is_set():
                s = 0
                for i in range(200000):
                    s += i * i
        self.threads = [threading.Thread(target=burn, daemon=True) for _ in range(3)]
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()


class TestProcessOutput(unittest.TestCase):
    def test_keeps_real_time_while_the_studio_is_busy(self):
        relay = Relay(sr=44100, channels=2, capacity_s=60)
        ramp = ((np.arange(44100 * 40) % 30000) + 1).astype(np.int16)
        relay.write(np.stack([ramp, ramp], axis=1))
        out = ProcessOutput(FAKE_SINK)
        try:
            with Busy():
                out.start_stream(relay.open_stream(0.0))
                self.assertEqual(out.error, "")
                time.sleep(2.0)
                t1, p1 = time.perf_counter(), out.position()
                time.sleep(6.0)
                t2, p2 = time.perf_counter(), out.position()
            rate = (p2 - p1) / (t2 - t1)
            self.assertAlmostEqual(rate, 1.0, delta=0.02, msg=f"rate {rate:.4f}")
        finally:
            out.stop()

    def test_a_skip_starts_over_and_pause_holds(self):
        relay = Relay(sr=44100, channels=2, capacity_s=60)
        relay.write(np.zeros((44100 * 30, 2), dtype=np.int16))
        out = ProcessOutput(FAKE_SINK)
        try:
            out.start_stream(relay.open_stream(0.0))
            time.sleep(1.5)
            self.assertGreater(out.position(), 1.0)
            out.start_stream(relay.open_stream(10.0))                   # a skip
            time.sleep(0.3)
            self.assertLess(out.position(), 0.6)
            time.sleep(0.5)
            out.pause()
            time.sleep(0.2)
            held = out.position()
            time.sleep(0.8)
            self.assertAlmostEqual(out.position(), held, delta=0.06)   # the queued piece may finish
            out.resume()
            time.sleep(0.6)
            self.assertGreater(out.position(), held + 0.3)
        finally:
            out.stop()


class TestProcessCapture(unittest.TestCase):
    def test_records_without_gaps_while_the_studio_is_busy(self):
        relay = Relay(sr=44100, channels=2, capacity_s=60)
        cap = ProcessCapture(relay, FAKE_SOURCE)
        try:
            with Busy():
                cap.start()
                self.assertIsNone(cap.error)
                t0 = time.monotonic()
                time.sleep(6.0)
                elapsed = time.monotonic() - t0
            time.sleep(0.3)
            x = relay.frames(0.0, relay.capture_time)[:, 0].astype(np.int64)
            steps = np.diff(x)
            broken = int(np.sum((steps != 1) & (steps != -29999)))      # the ramp wraps at 30000
            self.assertEqual(broken, 0, f"{broken} breaks in the recording")
            self.assertAlmostEqual(relay.capture_time, elapsed, delta=0.6)
            self.assertEqual(cap.lost, 0.0)
        finally:
            cap.stop()


if __name__ == "__main__":
    unittest.main()
