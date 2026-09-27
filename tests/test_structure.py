"""Sections and tempo: run with  .venv\\Scripts\\python -m unittest tests.test_structure -v

Synthetic audio only; nothing is played. The real SongFormer is not needed except
for one test, which is skipped if the model environment is missing.
"""

import os
import shutil
import tempfile
import unittest
import warnings

import numpy as np

from lightshow.studio import structure_model
from lightshow.studio.analysis import fix_half_tempo

warnings.filterwarnings("ignore")
SR = 22050


def drums(bpm, seconds, kick_every=2, snare=True):
    """Kick on every `kick_every`-th beat, snare (if any) on the beats in between."""
    period, n = 60.0 / bpm, int(seconds * SR)
    y = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(0)
    tk = np.arange(int(0.15 * SR)) / SR
    kick = (np.sin(2 * np.pi * (50 + 80 * np.exp(-tk * 30)) * tk) * np.exp(-tk * 18)).astype(np.float32)
    sn = (rng.standard_normal(int(0.12 * SR)) * np.exp(-np.arange(int(0.12 * SR)) / (0.03 * SR))).astype(np.float32)
    # a hi-hat lives up high, above the snare: noise differentiated three times is mostly treble
    hat = np.diff(rng.standard_normal(int(0.03 * SR) + 3), n=3) * np.exp(-np.arange(int(0.03 * SR)) / (0.005 * SR))
    hat = (0.2 * hat / np.abs(hat).max()).astype(np.float32)
    beats = np.arange(0, seconds - 0.5, period)
    for k, t in enumerate(beats):
        i = int(t * SR)
        y[i:i + len(hat)] += hat[: n - i]
        if k % kick_every == 0:
            y[i:i + len(kick)] += kick[: n - i]
        elif snare:
            y[i:i + len(sn)] += 0.6 * sn[: n - i]
    return y / (np.abs(y).max() + 1e-9), beats


class TestTempo(unittest.TestCase):
    def test_a_backbeat_is_not_halved(self):
        y, beats = drums(125.6, 40, kick_every=2, snare=True)          # kick on 1 and 3, snare on 2 and 4
        bpm, out = fix_half_tempo(y, SR, 125.6, beats)
        self.assertEqual((bpm, len(out)), (125.6, len(beats)))

    def test_a_grid_that_counts_double_is_halved(self):
        y, beats = drums(176.0, 40, kick_every=2, snare=False)         # a kick every other beat, nothing between
        bpm, out = fix_half_tempo(y, SR, 176.0, beats)
        self.assertEqual(bpm, 88.0)
        self.assertEqual(len(out), (len(beats) + 1) // 2)


class TestSongFormerPath(unittest.TestCase):
    def test_the_worker_is_always_given_a_full_path(self):
        d = tempfile.mkdtemp()
        seen = []
        real = structure_model.WORKER.ask_file
        real_available = structure_model.available
        try:
            open(os.path.join(d, "song.wav"), "wb").write(b"RIFF")
            structure_model.WORKER.ask_file = lambda p: seen.append(p) or [{"start": 0.0, "end": 10.0, "label": "verse"}]
            structure_model.available = lambda: True
            cwd = os.getcwd()
            os.chdir(d)
            try:
                segs, err = structure_model.run_model("song.wav", os.path.join(d, "cache"))
            finally:
                os.chdir(cwd)
            self.assertTrue(os.path.isabs(seen[0]), seen)
            self.assertEqual((len(segs), err), (1, ""))
            again, _ = structure_model.run_model(os.path.join(d, "song.wav"), os.path.join(d, "cache"))
            self.assertEqual(len(seen), 1)                               # the second time comes from the cache
        finally:
            structure_model.WORKER.ask_file = real
            structure_model.available = real_available
            shutil.rmtree(d, ignore_errors=True)

@unittest.skipUnless(structure_model.available(), "the model environment is not installed")
class TestRealSongFormer(unittest.TestCase):
    def test_songformer_answers_for_audio_from_memory(self):
        from lightshow.studio.jam_synth import make_song
        s = make_song(bpm=128.0, sr=24000, seed=1)
        segs = structure_model.run_audio(s["audio"].mean(axis=1), 24000)
        self.assertTrue(segs, structure_model.WORKER.last_error)
        self.assertAlmostEqual(segs[-1]["end"], s["duration"], delta=2.0)
        labels = {"intro", "verse", "pre-chorus", "chorus", "bridge", "inst", "outro", "silence"}   # SongFormer's vocabulary
        self.assertTrue(all(sg["label"] in labels for sg in segs), {sg["label"] for sg in segs})

    @classmethod
    def tearDownClass(cls):
        structure_model.WORKER.stop()


if __name__ == "__main__":
    unittest.main()


class TestTiming(unittest.TestCase):
    """The loudness detector's sections, with buildup and drop starts retimed by SongFormer."""

    beats = [i * 0.5 for i in range(400)]                   # 120 bpm: bar lines every 2 s

    def sections(self):
        return [{"start": 0.0, "end": 20.0, "type": "intro"}, {"start": 20.0, "end": 60.0, "type": "breakdown"},
                {"start": 60.0, "end": 90.0, "type": "verse"}, {"start": 90.0, "end": 100.0, "type": "buildup"},
                {"start": 100.0, "end": 140.0, "type": "drop"}]

    def test_lifts_move_to_songformer_and_onto_the_bar(self):
        segs = [{"start": 0.0, "label": "intro"}, {"start": 16.9, "label": "intro"}, {"start": 93.85, "label": "pre-chorus"},
                {"start": 103.9, "label": "chorus"}]
        secs, drops, moved = structure_model.time_with_songformer(self.sections(), [100.0], segs, self.beats, 0, 140.0)
        self.assertEqual([s["start"] for s in secs], [0.0, 20.0, 60.0, 94.0, 104.0])   # 93.85 and 103.9 land on bar lines
        self.assertEqual(drops, [104.0])                                               # the marker moved with its drop
        self.assertEqual(moved, 2)
        self.assertEqual([s["type"] for s in secs], [s["type"] for s in self.sections()])  # labels never change
        for a, b in zip(secs, secs[1:]):
            self.assertEqual(a["end"], b["start"])

    def test_quiet_transitions_keep_the_loudness_timing(self):
        segs = [{"start": 0.0, "label": "intro"}, {"start": 16.9, "label": "intro"}]        # SongFormer thinks 17 s
        secs, _, moved = structure_model.time_with_songformer(self.sections(), [], segs, self.beats, 0, 140.0)
        self.assertEqual(secs[1]["start"], 20.0)                                       # the breakdown stays at 20 s
        self.assertEqual(moved, 0)

    def test_nothing_moves_further_than_the_reach_or_across_a_neighbour(self):
        far = [{"start": 0.0, "label": "intro"}, {"start": 110.0, "label": "chorus"}]        # 10 s from the drop
        secs, _, moved = structure_model.time_with_songformer(self.sections(), [], far, self.beats, 0, 140.0)
        self.assertEqual(moved, 0)
        crossing = [{"start": 0.0, "label": "intro"}, {"start": 86.5, "label": "pre-chorus"}]  # would swallow the verse's end? no: 4 s back is fine
        secs, _, _ = structure_model.time_with_songformer(self.sections(), [], crossing, self.beats, 0, 140.0)
        self.assertLess(secs[2]["start"], secs[3]["start"])

    def test_without_songformer_nothing_changes(self):
        secs, drops, moved = structure_model.time_with_songformer(self.sections(), [100.0], None, self.beats, 0, 140.0)
        self.assertEqual((secs, drops, moved), (self.sections(), [100.0], 0))
