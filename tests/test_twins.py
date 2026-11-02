"""The same recording under two Spotify ids: found by name, artist, length and the audio; the user's sections carried
over, moved onto the other copy's timeline."""

import json
import os
import tempfile
import unittest

import numpy as np

from lightshow.studio.party import PartyLibrary, _analysis_version


def write(lib, tid, name, artists, duration, energy):
    os.makedirs(lib.learned_dir, exist_ok=True)
    with open(os.path.join(lib.learned_dir, tid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"version": _analysis_version(), "track": {"id": tid, "name": name, "artists": artists, "duration": duration},
                   "analysis": {"energy": list(energy), "feature_fps": 30.0, "beats": [], "sections": []}}, fh)


class TwinsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = PartyLibrary(self.tmp.name)
        rng = np.random.default_rng(1)
        self.e = np.abs(rng.normal(size=30 * 200))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_audio_later_in_its_copy_is_a_twin_with_its_offset(self):
        write(self.lib, "AAA", "Bombs Away", ["Your Neighbors"], 200.0, self.e)
        write(self.lib, "BBB", "Bombs Away", ["Your Neighbors"], 200.5, np.r_[np.zeros(60), self.e[:-60]])   # 2 s later
        self.assertEqual([t for t, _ in self.lib.twins("BBB")], ["AAA"])
        self.assertAlmostEqual(dict(self.lib.twins("BBB"))["AAA"], 2.0, places=1)
        with open(os.path.join(self.lib.dir, "section_truth.json"), "w", encoding="utf-8") as fh:
            json.dump({"AAA": {"sections": [{"type": "verse", "start": 0.0, "end": 40.0}, {"type": "drop", "start": 40.0, "end": 200.0}]}}, fh)
        secs = self.lib.reviewed_sections("BBB")
        self.assertAlmostEqual(secs[1]["start"], 42.0, places=1)

    def test_a_different_recording_of_the_same_name_is_not(self):
        write(self.lib, "AAA", "Pay No Mind", ["Madeon"], 200.0, self.e)
        write(self.lib, "BBB", "Pay No Mind", ["Madeon"], 200.5, np.abs(np.random.default_rng(7).normal(size=30 * 200)))
        self.assertEqual(self.lib.twins("BBB"), [])

    def test_a_live_album_title_of_the_same_audio_is_a_twin(self):
        write(self.lib, "AAA", "Found", ["Woodlands Worship"], 288.4, self.e)
        write(self.lib, "BBB", "Found - Live", ["Woodlands Worship"], 289.0, np.r_[np.zeros(48), self.e[:-48]])
        self.assertEqual([t for t, _ in self.lib.twins("BBB")], ["AAA"])

    def test_a_remix_is_not_even_with_the_same_length(self):
        write(self.lib, "AAA", "Found", ["Woodlands Worship"], 288.4, self.e)
        write(self.lib, "BBB", "Found - Remix", ["Woodlands Worship"], 288.4, self.e)
        self.assertEqual(self.lib.twins("BBB"), [])

    def test_another_length_is_not(self):
        write(self.lib, "AAA", "Pay No Mind", ["Madeon"], 204.0, self.e)
        write(self.lib, "BBB", "Pay No Mind", ["Madeon"], 249.0, self.e)
        self.assertEqual(self.lib.twins("BBB"), [])


if __name__ == "__main__":
    unittest.main()
