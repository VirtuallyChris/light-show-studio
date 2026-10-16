"""What makes one kind of song different: 808s, hat rolls, wobbles, a key change. Synthetic songs only."""

import unittest

import numpy as np

from lightshow.studio import flavor

SR = 22050


def tone(freq, secs, decay=None, sr=SR):
    t = np.arange(int(secs * sr)) / sr
    env = np.exp(-t / decay) if decay else np.ones_like(t)
    return (np.sin(2 * np.pi * freq * t) * env).astype(np.float32)


class TestFlavor(unittest.TestCase):
    def test_a_held_808_is_found_and_a_pluck_is_not(self):
        b = np.zeros(SR * 8, dtype=np.float32)
        hits = []
        for k, t in enumerate((1.0, 3.0, 5.0)):                       # held, low notes
            n = tone(45, 1.2, decay=1.5)
            b[int(t * SR):int(t * SR) + len(n)] += n
            hits.append((t, 1.0))
        p = tone(90, 0.3, decay=0.05)                                  # a short pluck
        b[int(6.5 * SR):int(6.5 * SR) + len(p)] += p
        hits.append((6.5, 1.0))
        found = flavor.find_808s(b, SR, hits)
        self.assertEqual([round(t) for t, _, _ in found], [1, 3, 5])
        self.assertTrue(all(h >= 0.35 for _, _, h in found))

    def test_a_hat_roll_is_found_but_steady_sixteenths_are_not(self):
        bpm, beats = 140.0, np.arange(0, 16, 60 / 140.0)
        rng = np.random.default_rng(2)
        d = np.zeros(SR * 16, dtype=np.float32)
        burst = lambda: (rng.normal(0, 1, int(0.012 * SR)) * np.hanning(int(0.012 * SR))).astype(np.float32)
        hp = lambda x: np.diff(x, prepend=x[:1]).astype(np.float32)   # bright, like a hi-hat
        period = 60 / bpm
        for t in np.arange(0.0, 6.0, period / 4):                     # steady sixteenths
            i = int(t * SR); x = hp(burst()); d[i:i + len(x)] += x
        for t in np.arange(8.0, 8.0 + 2 * period, period / 8):        # a two-beat roll in 32nds
            i = int(t * SR); x = hp(burst()); d[i:i + len(x)] += x
        rolls = flavor.find_hat_rolls(d, SR, beats)
        self.assertEqual(len(rolls), 1)
        self.assertAlmostEqual(rolls[0][0], 8.0, delta=0.1)
        self.assertGreaterEqual(rolls[0][2], 6)

    def test_a_wobble_is_found_at_its_own_speed(self):
        bpm = 140.0
        beats = np.arange(0, 40, 60 / bpm)
        t = np.arange(int(40 * SR)) / SR
        lfo = 0.5 - 0.5 * np.cos(2 * np.pi * (bpm / 60 * 2) * t)        # two wobbles a beat
        b = (np.sin(2 * np.pi * 55 * t) * (0.1 + 0.9 * lfo)).astype(np.float32)
        w = flavor.find_wobbles(b, SR, beats)
        self.assertTrue(w)
        self.assertEqual(w[0][2], 2)
        self.assertGreater(w[0][1] - w[0][0], 20)

    def test_plucked_eighth_notes_are_no_wobble(self):
        bpm = 140.0
        beats = np.arange(0, 40, 60 / bpm)
        b = np.zeros(int(40 * SR), dtype=np.float32)
        for t in np.arange(0, 39.5, 60 / bpm / 2):                    # a bass playing eighths, each plucked
            n = tone(55, 0.2, decay=0.06)
            b[int(t * SR):int(t * SR) + len(n)] += n
        self.assertEqual(flavor.find_wobbles(b, SR, beats), [])

    def test_a_steady_bass_is_no_wobble(self):
        beats = np.arange(0, 40, 60 / 120.0)
        b = tone(55, 40)
        self.assertEqual(flavor.find_wobbles(b, SR, beats), [])

    def test_a_key_change_up_near_the_end(self):
        def chord(root_midi, secs):
            f = lambda m: 440.0 * 2 ** ((m - 69) / 12)
            return sum(tone(f(root_midi + k), secs) for k in (0, 4, 7)) / 3
        prog = lambda key: np.concatenate([chord(key + r, 2.0) for r in (0, 5, 7, 0)])   # I IV V I
        song = np.concatenate([prog(60) for _ in range(13)] + [prog(62) for _ in range(5)])  # C major, then D
        kc = flavor.find_key_change(song.astype(np.float32), SR, len(song) / SR)
        self.assertIsNotNone(kc)
        self.assertEqual(kc["up"], 2)
        self.assertAlmostEqual(kc["time"], 13 * 8.0, delta=10)

    def test_no_key_change_in_a_song_that_stays_put(self):
        def chord(root_midi, secs):
            f = lambda m: 440.0 * 2 ** ((m - 69) / 12)
            return sum(tone(f(root_midi + k), secs) for k in (0, 4, 7)) / 3
        song = np.concatenate([np.concatenate([chord(60 + r, 2.0) for r in (0, 5, 7, 0)]) for _ in range(16)])
        self.assertIsNone(flavor.find_key_change(song.astype(np.float32), SR, len(song) / SR))


if __name__ == "__main__":
    unittest.main()


class TestDropTiming(unittest.TestCase):
    """A drop starts where its bass comes in, as it is heard (Great Good Fine Ok's last drop)."""

    def song(self, bass_from):
        beats = np.arange(0, 40, 0.5)                                  # 120 bpm
        t = np.arange(int(40 * SR)) / SR
        drums = (np.sin(2 * np.pi * 60 * t) * (np.mod(t, 0.5) < 0.1)).astype(np.float32)
        bass = (np.sin(2 * np.pi * 55 * t) * (t >= bass_from)).astype(np.float32)
        return beats, {"drums": drums, "bass": bass, "sr": SR}

    def sections(self, drop_at):
        return [{"start": 0.0, "end": drop_at, "type": "verse"}, {"start": drop_at, "end": 40.0, "type": "drop"}]

    def test_a_drop_moves_to_the_beat_its_bass_comes_in(self):
        from lightshow.studio.analysis import refine_drops
        beats, stems = self.song(bass_from=16.5)                       # the bass lands one beat after the bar line
        secs, drops, moves = refine_drops(self.sections(16.0), [16.0], beats, stems)
        self.assertEqual(secs[1]["start"], 16.5)
        self.assertEqual(secs[0]["end"], 16.5)
        self.assertEqual(drops, [16.5])
        self.assertEqual(moves[0][2], 1)

    def test_a_drop_whose_bass_is_already_playing_stays(self):
        from lightshow.studio.analysis import refine_drops
        beats, stems = self.song(bass_from=8.0)
        secs, drops, moves = refine_drops(self.sections(16.0), [16.0], beats, stems)
        self.assertEqual((secs[1]["start"], drops, moves), (16.0, [16.0], []))


class TestDrumlessDrops(unittest.TestCase):
    """A "drop" with the drums silent, in a song that has drums, is a lead-in, not a drop."""

    def test_a_drop_with_no_drums_becomes_a_verse(self):
        from lightshow.studio.analysis import drumless_drops
        fps = 30
        dp = [0.6] * (40 * fps) + [0.05] * (26 * fps) + [0.7] * (26 * fps)   # drums, then none, then drums
        a = {"feature_fps": fps, "curves": {"drums_present": dp}, "drops": [40.0, 66.0],
             "sections": [{"start": 0.0, "end": 40.0, "type": "verse"}, {"start": 40.0, "end": 66.0, "type": "drop"},
                          {"start": 66.0, "end": 92.0, "type": "drop"}]}
        self.assertEqual(drumless_drops(a), [(40.0, "verse")])
        self.assertEqual([s["type"] for s in a["sections"]], ["verse", "verse", "drop"])
        self.assertEqual(a["drops"], [66.0])

    def test_a_drop_lands_where_its_drums_come_back_after_the_build(self):
        from lightshow.studio.analysis import drumless_drops
        fps = 30
        # drums, a drumless 12 s, drums at half (the build), a 2 s stop, then the drums for good
        dp = [0.7] * (20 * fps) + [0.02] * (12 * fps) + [0.5] * (14 * fps) + [0.1] * (2 * fps) + [0.8] * (30 * fps)
        a = {"feature_fps": fps, "curves": {"drums_present": dp}, "drops": [20.0], "beats": [i * 0.5 for i in range(200)],
             "sections": [{"start": 0.0, "end": 20.0, "type": "verse"}, {"start": 20.0, "end": 78.0, "type": "drop"}]}
        drumless_drops(a)
        self.assertEqual([s["type"] for s in a["sections"]], ["verse", "verse", "drop"])
        self.assertAlmostEqual(a["sections"][2]["start"], 48.0, delta=0.6)       # after the stop, not where the build began
        self.assertEqual(len(a["drops"]), 1)
        self.assertAlmostEqual(a["drops"][0], 48.0, delta=0.6)

    def test_a_song_without_drums_keeps_its_drops(self):
        from lightshow.studio.analysis import drumless_drops
        a = {"feature_fps": 30, "curves": {"drums_present": [0.05] * 3000}, "drops": [40.0],
             "sections": [{"start": 40.0, "end": 66.0, "type": "drop"}]}
        self.assertEqual(drumless_drops(a), [])
        self.assertEqual(a["sections"][0]["type"], "drop")
