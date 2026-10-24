"""The studio's own window (server.py open_app_window): it opens filling the screen, and a short sound effect can
be analyzed (a doorbell show's 4 s howl)."""

import json
import os
import tempfile
import unittest

import numpy as np

from lightshow.studio.server import _start_maximized


class StartMaximizedTest(unittest.TestCase):
    def test_the_remembered_window_is_marked_maximized(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Default"))
            path = os.path.join(d, "Default", "Preferences")
            place = {"bottom": 990, "left": 10, "maximized": False, "right": 1690, "top": 10}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"browser": {"app_window_placement": {"127": {"0": {"0": {"1_/": place}}}}}, "other": {"maximized": False}}, fh)
            _start_maximized(d)
            with open(path, encoding="utf-8") as fh:
                prefs = json.load(fh)
            self.assertTrue(prefs["browser"]["app_window_placement"]["127"]["0"]["0"]["1_/"]["maximized"])
            self.assertFalse(prefs["other"]["maximized"])            # nothing else touched

    def test_no_profile_yet_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            _start_maximized(d)                                    # (first run: no Preferences)


class ShortSoundTest(unittest.TestCase):
    def test_a_four_second_sound_effect_is_analyzed(self):
        from lightshow.studio.analysis import analyze_audio
        sr = 22050
        t = np.arange(int(4 * sr)) / sr
        y = (0.4 * np.sin(2 * np.pi * np.cumsum(300 + 250 * np.sin(np.pi * t / 4)) / sr)).astype(np.float32)
        a = analyze_audio(y, sr, events=False)
        self.assertGreaterEqual(a["duration"], 4)
        with self.assertRaises(ValueError):
            analyze_audio(y[: sr // 4], sr, events=False)            # a quarter second: still too short


if __name__ == "__main__":
    unittest.main()
