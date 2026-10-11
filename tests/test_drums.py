"""The kick and snare are found where they are played: run with  .venv\\Scripts\\python -m unittest tests.test_drums -v

A synthetic groove with a known pattern: kick on 1, 3 and the "and" of 3, snare on
2 and 4, hi-hats on every eighth note (which must not be taken for snares).
"""

import unittest

import numpy as np

from lightshow.studio.events import detect_events

SR = 22050


def groove(bpm=100.0, bars=16, seed=3):
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    n = int(bars * 4 * beat * SR) + SR
    y = np.zeros(n, dtype=np.float32)
    tk = np.arange(int(0.2 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-tk * 30)) * tk) * np.exp(-tk * 14)
    ts = np.arange(int(0.18 * SR)) / SR
    snare = (0.6 * np.sin(2 * np.pi * 200 * ts) * np.exp(-ts * 25)
             + 0.8 * rng.standard_normal(len(ts)) * np.exp(-ts * 22))
    th = np.arange(int(0.04 * SR)) / SR
    hat = rng.standard_normal(len(th)) * np.exp(-th / 0.008)
    from scipy.signal import butter, lfilter
    b, a = butter(4, 7000 / (SR / 2), "high")
    hat = lfilter(b, a, hat)
    pad_t = np.arange(n) / SR
    y += 0.05 * np.sin(2 * np.pi * 220 * pad_t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.25 * pad_t))   # something tonal
    kicks, snares = [], []

    def put(sig, t, g):
        i = int(t * SR)
        j = min(n, i + len(sig))
        y[i:j] += (sig[: j - i] * g).astype(np.float32)

    for bar in range(bars):
        t0 = 0.5 + bar * 4 * beat
        for k in (0, 2, 2.5):
            put(kick, t0 + k * beat, 0.9)
            kicks.append(t0 + k * beat)
        for k in (1, 3):
            put(snare, t0 + k * beat, 0.5)
            snares.append(t0 + k * beat)
        for h in range(8):
            put(hat, t0 + h * beat / 2, 0.2)
    beats = [0.5 + i * beat for i in range(bars * 4)]
    return y, beats, n / SR, np.array(kicks), np.array(snares)


def score(found, truth, tol=0.04):
    found = np.array([f[0] for f in found])
    hit = sum(1 for t in truth if len(found) and np.min(np.abs(found - t)) < tol)
    extra = sum(1 for f in found if np.min(np.abs(truth - f)) >= tol)
    return hit / len(truth), extra


class TestDrums(unittest.TestCase):
    def test_kick_and_snare_are_found_and_hats_are_not_snares(self):
        y, beats, dur, kicks, snares = groove()
        out = detect_events(None, beats, dur, audio=(y, SR))
        ev, cal = out["events"], out["drum_calibration"]
        # the bands were learned from this groove: a low kick band, and a snare band the kick does not fill
        self.assertIsNotNone(cal)
        self.assertLessEqual(cal["kick_band"][1], 200)
        self.assertIn("snare_band", cal)
        self.assertEqual(sorted(cal["snare_pos"]), [4, 12])             # 2 and 4 of the bar
        k_recall, k_extra = score(ev["kick"], kicks)
        s_recall, s_extra = score(ev["snare"], snares)
        print(f"kick {k_recall:.0%} found, {k_extra} extra; snare {s_recall:.0%} found, {s_extra} extra")
        self.assertGreater(k_recall, 0.9)
        self.assertGreater(s_recall, 0.9)
        self.assertLess(k_extra, len(kicks) * 0.1)
        self.assertLess(s_extra, len(snares) * 0.1)             # the hats did not turn into snares


if __name__ == "__main__":
    unittest.main()
