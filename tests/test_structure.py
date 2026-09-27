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
        self.assertTrue(all(sg["label"] in structure_model.LABEL_MAP for sg in segs))

    @classmethod
    def tearDownClass(cls):
        structure_model.WORKER.stop()


if __name__ == "__main__":
    unittest.main()
