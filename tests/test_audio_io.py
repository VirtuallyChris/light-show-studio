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
                t0 = time.perf_counter()                            # (before it starts: it plays before start_stream() returns)
                out.start_stream(relay.open_stream(0.0))
                self.assertEqual(out.error, "")
                started = time.perf_counter()
                time.sleep(8.0)
                played, t1 = out.position(), time.perf_counter()
            # With the clock overall. (Not between two readings: when this process hogs its own interpreter the fake
            # device may wait for audio and then catch up, so a first reading can come late and only look like running fast.)
            self.assertGreater(played, t1 - started - 0.6, "playback fell behind the clock")
            # never ahead of the clock by more than the device's own 0.3 s buffer and a piece
            self.assertLess(played, t1 - t0 + 0.4)
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
                t0 = time.monotonic()                    # (before it starts: it records from before start() returns)
                cap.start()
                self.assertIsNone(cap.error)
                started = time.monotonic()
                time.sleep(6.0)
            time.sleep(0.3)
            second, t2 = relay.capture_time, time.monotonic()
            x = relay.frames(0.0, relay.capture_time)[:, 0].astype(np.int64)
            steps = np.diff(x)
            broken = int(np.sum((steps != 1) & (steps != -29999)))      # the ramp wraps at 30000
            self.assertEqual(broken, 0, f"{broken} breaks in the recording")
            # With the clock overall. (Not between two readings: while this process is hogged, the reading lags and
            # then catches up, which only looks like recording fast.) It was recording by the time start() returned.
            self.assertGreater(second, t2 - started - 0.5, "the recording fell behind the clock")
            # never ahead of the clock by more than the recorder's own 0.25 s margin
            self.assertLess(second, t2 - t0 + 0.3)
            self.assertEqual(cap.lost, 0.0)
        finally:
            cap.stop()


PARENT = """
import os, sys, time
import numpy as np
from lightshow.studio.audio_io import FAKE_SINK, FAKE_SOURCE, ProcessCapture, ProcessOutput
from lightshow.studio.relay import Relay
if __name__ == "__main__":
    relay = Relay(sr=44100, channels=2, capacity_s=30)
    relay.write(np.zeros((44100 * 10, 2), dtype=np.int16))
    cap = ProcessCapture(Relay(sr=44100, channels=2, capacity_s=30), FAKE_SOURCE); cap.start()
    out = ProcessOutput(FAKE_SINK); out.start_stream(relay.open_stream(0.0))
    print(cap.proc.pid, out.proc.pid, flush=True)
    time.sleep(60)
"""


class TestOrphans(unittest.TestCase):
    def test_audio_processes_end_when_the_studio_is_killed(self):
        import os
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        parent = subprocess.Popen([sys.executable, "-c", PARENT], cwd=root, stdout=subprocess.PIPE, text=True)
        try:
            pids = [int(x) for x in parent.stdout.readline().split()]
            self.assertEqual(len(pids), 2)
            parent.kill()                                   # no chance to stop its children
            parent.wait(5)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and any(_alive(p) for p in pids):
                time.sleep(0.2)
            self.assertEqual([p for p in pids if _alive(p)], [], "audio processes outlived the studio")
        finally:
            if parent.poll() is None:
                parent.kill()


def _alive(pid: int) -> bool:
    import os
    import subprocess
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


class TestPriority(unittest.TestCase):
    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows scheduling")
    def test_sound_goes_first_and_analysis_steps_back(self):
        from lightshow.studio import priority
        out = {}

        def work():
            out.update(audio=priority.audio_thread(), high=priority.high_thread(), low=priority.low_thread())
        t = threading.Thread(target=work)
        t.start()
        t.join()
        self.assertEqual(out, {"audio": True, "high": True, "low": True})


if __name__ == "__main__":
    unittest.main()
