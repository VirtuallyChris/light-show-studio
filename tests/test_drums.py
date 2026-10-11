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


def pattern_song(bpm, bars, per, kicks_at, snares_at, hats_per_beat, seed=5):
    """A groove with `per` beats a bar: kick and snare at the given beat positions (fractions allowed),
    hats `hats_per_beat` times a beat. Returns the mix, the drums alone, beats, length and the truth."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    n = int(bars * per * beat * SR) + SR
    drums = np.zeros(n, dtype=np.float32)
    tk = np.arange(int(0.2 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-tk * 30)) * tk) * np.exp(-tk * 14)
    ts = np.arange(int(0.18 * SR)) / SR
    snare = 0.6 * np.sin(2 * np.pi * 200 * ts) * np.exp(-ts * 25) + 0.8 * rng.standard_normal(len(ts)) * np.exp(-ts * 22)
    th = np.arange(int(0.04 * SR)) / SR
    from scipy.signal import butter, lfilter
    b, a = butter(4, 7000 / (SR / 2), "high")
    hat = lfilter(b, a, rng.standard_normal(len(th)) * np.exp(-th / 0.008))

    def put(sig, t, g):
        i = int(t * SR); j = min(n, i + len(sig))
        drums[i:j] += (sig[: j - i] * g).astype(np.float32)
    K, Sn = [], []
    for bar in range(bars):
        t0 = 0.5 + bar * per * beat
        for k in kicks_at:
            put(kick, t0 + k * beat, 0.9); K.append(t0 + k * beat)
        for k in snares_at:
            put(snare, t0 + k * beat, 0.5); Sn.append(t0 + k * beat)
        for h in range(per * hats_per_beat):
            put(hat, t0 + h * beat / hats_per_beat, 0.2)
    beats = [0.5 + i * beat for i in range(bars * per)]
    return drums, beats, n / SR, np.array(K), np.array(Sn)


class TestLayoutsAndStems(unittest.TestCase):
    def cal(self, y, beats):
        import librosa
        from lightshow.studio.events import calibrate_drums
        S = np.abs(librosa.stft(y, hop_length=512))
        return calibrate_drums(S, librosa.fft_frequencies(sr=SR), SR, 512, beats, 0)

    def test_a_shuffle_is_heard_in_triplets(self):
        y, beats, *_ = pattern_song(90, 16, 4, kicks_at=(0, 2, 2 + 2 / 3), snares_at=(1, 3), hats_per_beat=3)
        self.assertEqual(self.cal(y, beats)["grid"]["slots_per_beat"], 3)

    def test_a_waltz_is_heard_in_three(self):
        y, beats, *_ = pattern_song(120, 20, 3, kicks_at=(0,), snares_at=(1, 2), hats_per_beat=2)
        self.assertEqual(self.cal(y, beats)["grid"]["beats_per_cycle"], 3)

    def test_no_drums_in_the_drum_track_means_no_drums(self):
        import librosa
        from lightshow.studio.events import detect_events
        t = np.arange(int(40 * SR)) / SR
        mix = (0.3 * np.sin(2 * np.pi * 110 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 1.7 * t))).astype(np.float32)   # a strummed-ish tone
        quiet = (0.002 * np.random.default_rng(1).standard_normal(len(t))).astype(np.float32)
        beats = list(np.arange(0.5, 39, 0.5))
        out = detect_events(None, beats, 40.0, audio=(mix, SR), stems={"drums": quiet, "bass": mix, "sr": SR})
        self.assertFalse(out["drum_calibration"]["has_drums"])
        self.assertEqual(out["events"]["kick"], [])
        self.assertEqual(out["events"]["snare"], [])

    def test_the_drum_track_wins_when_the_mix_is_crowded(self):
        from lightshow.studio.events import detect_events
        drums, beats, dur, K, _ = pattern_song(100, 16, 4, kicks_at=(0, 2, 2.5), snares_at=(1, 3), hats_per_beat=2)
        t = np.arange(len(drums)) / SR
        bass = (0.8 * np.sin(2 * np.pi * 55 * t) * (np.sin(2 * np.pi * 100 / 60 * 2 * t) > 0)).astype(np.float32)   # a loud pumping sub bass on the off beats
        out = detect_events(None, beats, dur, audio=(drums + bass, SR), stems={"drums": drums, "bass": bass, "sr": SR})
        cal = out["drum_calibration"]
        self.assertTrue(cal["has_drums"])
        self.assertEqual(cal["kick_source"], "stem")
        recall, extra = score(out["events"]["kick"], K)
        self.assertGreater(recall, 0.9)
        self.assertLess(extra, len(K) * 0.15)


class TestRolls(unittest.TestCase):
    def test_a_snare_roll_is_found_and_becomes_the_buildup(self):
        import librosa
        from lightshow.studio.events import find_rolls
        from lightshow.studio.analysis import buildups_from_rolls
        drums, beats, dur, _, _ = pattern_song(128, 12, 4, kicks_at=(0, 1, 2, 3), snares_at=(1, 3), hats_per_beat=2)
        beat = 60 / 128
        rng = np.random.default_rng(9)
        ts = np.arange(int(0.08 * SR)) / SR
        hit = (0.6 * np.sin(2 * np.pi * 220 * ts) + 0.7 * rng.standard_normal(len(ts))) * np.exp(-ts * 40)
        r0 = 0.5 + 8 * 4 * beat                       # bars 9 and 10: a roll in 16ths, then 32nds
        t, step = r0, beat / 4
        while t < r0 + 8 * beat:
            i = int(t * SR); drums[i:i + len(hit)] += (0.5 * hit[: len(drums) - i]).astype(np.float32)
            t += step if t < r0 + 4 * beat else step / 2
        S = np.abs(librosa.stft(drums, hop_length=512))
        hits, spans = find_rolls(S, librosa.fft_frequencies(sr=SR), SR, 512, dur, beats)
        self.assertTrue(spans)
        self.assertLess(abs(spans[0][0] - r0), beat * 1.01)
        self.assertGreater(len(hits), 40)
        drop = r0 + 8 * beat
        sections = [{"start": 0.0, "end": drop, "type": "verse"}, {"start": drop, "end": dur, "type": "drop"}]
        out, added = buildups_from_rolls(sections, spans, beats, 0)
        self.assertEqual(added, 1)
        self.assertEqual([x["type"] for x in out], ["verse", "buildup", "drop"])
        self.assertLess(abs(out[1]["start"] - r0), beat * 1.01)
        self.assertEqual(sections[0]["type"], "verse")            # the original list is left alone
