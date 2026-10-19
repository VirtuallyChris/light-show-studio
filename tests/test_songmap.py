"""Know the song: its parts, its tension and its moments."""

import unittest

import numpy as np

from lightshow.studio import songmap


def song(n_s=120, fps=30, bar=2.0):
    a = {"feature_fps": fps, "bpm": 120.0, "downbeats": [i * bar for i in range(int(n_s / bar))], "beats": [i * bar / 4 for i in range(int(n_s / bar) * 4)]}
    return a


class TestParts(unittest.TestCase):
    def test_a_chorus_heard_again_is_the_same_part_and_counted(self):
        rng = np.random.default_rng(3)
        fps, afps, n = 30, 43.0, 120
        a = song(n, fps)
        verse, chorus = rng.standard_normal((32, int(16 * afps))), rng.standard_normal((32, int(16 * afps))) + 1.5
        bands = np.zeros((32, int(n * afps)), dtype=np.float32)
        layout = [(0, verse), (16, chorus), (32, verse), (48, chorus), (64, rng.standard_normal((32, int(16 * afps)))), (80, chorus), (96, verse)]
        for t0, blk in layout:
            bands[:, int(t0 * afps):int(t0 * afps) + blk.shape[1]] = blk
        a["align_bands"], a["align_fps"] = bands.tolist(), afps
        a["energy"] = [0.5] * (n * fps)
        a["curves"] = {"vocals": [0.5] * (n * fps), "drums_present": [0.8] * (n * fps), "bass": [0.5] * (n * fps)}
        a["sections"] = [{"start": t, "end": t + 16.0, "type": "drop" if blk is chorus else "verse"} for t, blk in layout]
        ps = songmap.parts(a)
        self.assertEqual([p["part"] for p in ps], ["A", "B", "A", "B", "C", "B", "A"])
        self.assertEqual([(p["occ"], p["of"]) for p in ps if p["part"] == "B"], [(1, 3), (2, 3), (3, 3)])


class TestTension(unittest.TestCase):
    def test_a_snare_build_winds_up_and_its_drop_is_the_release(self):
        fps, n = 30, 80
        a = song(n, fps)
        en = np.full(n * fps, 0.4); en[40 * fps:60 * fps] = 0.85
        dr = np.full(n * fps, 0.5); dr[40 * fps:60 * fps] = 0.95
        bass = np.full(n * fps, 0.5); bass[28 * fps:40 * fps] = 0.0; bass[40 * fps:60 * fps] = 0.7
        a["energy"], a["curves"] = list(en), {"drums_present": list(dr), "bass": list(bass)}
        snares = [t for t in np.arange(28, 32, 1.0)] + [t for t in np.arange(32, 36, 0.5)] + [t for t in np.arange(36, 40, 0.25)]
        a["events"] = {"snare": [[float(t), 1.0] for t in snares]}
        a["sections"] = [{"start": 0.0, "end": 28.0, "type": "verse"}, {"start": 28.0, "end": 40.0, "type": "buildup"}, {"start": 40.0, "end": 60.0, "type": "drop"}, {"start": 60.0, "end": 80.0, "type": "verse"}]
        ten = songmap.tension(a)
        self.assertTrue(any(abs(r["t"] - 40.0) < 0.01 for r in ten["releases"]), ten["releases"])
        v = ten["values"]; k = lambda t: int((t - ten["t0"]) / ten["dt"])
        self.assertGreater(np.mean(v[k(36):k(40)]), np.mean(v[k(10):k(20)]) + 0.3)      # wound up before the drop


class TestMoments(unittest.TestCase):
    def test_the_top_moments_are_kept_a_phrase_apart(self):
        a = song(100)
        a["energy"] = [0.5] * 3000; a["curves"] = {}
        ten = {"releases": [{"t": 40.0, "strength": 0.9}, {"t": 42.0, "strength": 0.5}, {"t": 80.0, "strength": 0.4}]}
        m = songmap.moments(a, [], ten)
        self.assertEqual([x["t"] for x in m], [40.0, 80.0])


if __name__ == "__main__":
    unittest.main()
