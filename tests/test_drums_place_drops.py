"""The drums, the bass and the voice placing drops, bar by bar (analysis.drums_and_bass_place_drops, long_buildups)."""

import unittest

import numpy as np

from lightshow.studio.analysis import drums_and_bass_place_drops, long_buildups, verses_without_synths

FPS = 30
BAR = 2.0


def song(levels, sections):
    """A made-up song: levels is one dict per 2 s bar (e, bass, dp, drum, voc, other), sections as (type, start, end)."""
    n = len(levels) * int(BAR * FPS)
    curves = {k: np.zeros(n) for k in ("bass", "drums_present", "drum", "vocals", "other")}
    energy = np.zeros(n)
    for i, lv in enumerate(levels):
        sl = slice(int(i * BAR * FPS), int((i + 1) * BAR * FPS))
        energy[sl] = lv["e"]
        for k, name in (("bass", "bass"), ("dp", "drums_present"), ("drum", "drum"), ("voc", "vocals"), ("other", "other")):
            curves[name][sl] = lv[k]
    return {"feature_fps": FPS, "bpm": 120.0, "energy": energy.tolist(), "curves": {k: v.tolist() for k, v in curves.items()},
            "downbeats": [i * BAR for i in range(len(levels) + 1)],
            "sections": [{"type": t, "start": s, "end": e} for t, s, e in sections], "drops": [s for t, s, e in sections if t == "drop"]}


FULL = {"e": 1.0, "bass": 0.7, "dp": 0.95, "drum": 0.7, "voc": 0.6, "other": 0.6}
VERSE = {"e": 0.7, "bass": 0.5, "dp": 0.8, "drum": 0.3, "voc": 0.7, "other": 0.4}
QUIET = {"e": 0.4, "bass": 0.0, "dp": 0.05, "drum": 0.0, "voc": 0.6, "other": 0.5}


class DrumsPlaceDrops(unittest.TestCase):
    def types(self, a):
        return [(s["type"], s["start"]) for s in a["sections"]]

    def test_instrumental_drop_in_a_verse(self):
        inst = {**FULL, "voc": 0.0, "drum": 0.8}
        a = song([inst] * 5 + [VERSE] * 8 + [FULL] * 8, [("verse", 0.0, 26.0), ("drop", 26.0, 42.0)])
        drums_and_bass_place_drops(a)
        self.assertEqual(self.types(a)[:2], [("drop", 0.0), ("verse", 10.0)])

    def test_drop_ends_where_its_drums_stop(self):
        gone = {**FULL, "dp": 0.1, "drum": 0.0}
        a = song([VERSE] * 4 + [FULL] * 8 + [gone] * 2 + [QUIET] * 2, [("verse", 0.0, 8.0), ("drop", 8.0, 28.0), ("outro", 28.0, 32.0)])
        drums_and_bass_place_drops(a)
        self.assertEqual(self.types(a)[-1], ("outro", 24.0))

    def test_drop_starts_with_its_bass(self):
        build = {**FULL, "bass": 0.0, "e": 0.75}
        a = song([VERSE] * 4 + [build] * 3 + [FULL] * 8, [("verse", 0.0, 8.0), ("drop", 8.0, 30.0)])
        drums_and_bass_place_drops(a)
        self.assertEqual(self.types(a), [("verse", 0.0), ("buildup", 8.0), ("drop", 14.0)])

    def test_bassless_start_as_loud_as_the_drop_stays_the_drop(self):
        loud = {**FULL, "bass": 0.0}
        a = song([VERSE] * 4 + [loud] * 3 + [FULL] * 8, [("verse", 0.0, 8.0), ("drop", 8.0, 30.0)])
        drums_and_bass_place_drops(a)
        self.assertEqual(self.types(a), [("verse", 0.0), ("drop", 8.0)])

    def test_long_buildup_starts_where_the_drums_come_in(self):
        drums = {**QUIET, "dp": 0.9, "drum": 0.5, "e": 0.6}
        pause = {**QUIET, "dp": 0.1}
        a = song([QUIET] * 10 + [drums] * 3 + [pause] + [FULL] * 6, [("buildup", 0.0, 28.0), ("drop", 28.0, 40.0)])
        long_buildups(a)
        self.assertEqual(self.types(a), [("breakdown", 0.0), ("buildup", 20.0), ("drop", 28.0)])

    def test_a_verse_without_synths_in_a_long_drop(self):
        verse = {**FULL, "other": 0.1, "voc": 0.8}
        a = song([FULL] * 8 + [verse] * 8 + [FULL] * 10, [("drop", 0.0, 52.0)])
        verses_without_synths(a)
        self.assertEqual(self.types(a), [("drop", 0.0), ("verse", 16.0), ("drop", 32.0)])

    def test_a_short_drop_keeps_its_quiet_bars(self):
        verse = {**FULL, "other": 0.1, "voc": 0.8}
        a = song([FULL] * 4 + [verse] * 6 + [FULL] * 4, [("drop", 0.0, 28.0)])
        verses_without_synths(a)
        self.assertEqual(self.types(a), [("drop", 0.0)])


if __name__ == "__main__":
    unittest.main()
