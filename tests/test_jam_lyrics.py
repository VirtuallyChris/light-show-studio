"""The lyrics a learned song's show gets (JamLibrary.lyrics): slimmed, missing is None, read again when rewritten."""

import json
import os
import tempfile
import time
import unittest

from lightshow.studio.jam import JamLibrary


class TestJamLyrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = JamLibrary(self.tmp.name)
        os.makedirs(os.path.join(self.lib.dir, "lyrics"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, tid, doc):
        with open(os.path.join(self.lib.dir, "lyrics", tid + ".json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def test_slim_copy(self):
        self.write("abc123", {"version": 1, "source": "lrclib", "model": "x", "agreement": 0.7,
                              "words": [{"t": 1.0, "end": 1.4, "w": "Red", "p": 0.3, "line": 0},
                                        {"t": 2.0, "end": 2.2, "w": "lights", "p": 0.2, "line": 0, "bg": 1},
                                        {"t": 3.0, "end": 3.1, "w": "", "line": 0}],
                              "lines": [{"t": 1.0, "end": 2.2, "text": "Red lights", "extra": 5}]})
        got = self.lib.lyrics("abc123")
        self.assertEqual(got["source"], "lrclib")
        self.assertEqual(got["words"], [{"t": 1.0, "end": 1.4, "w": "Red", "line": 0, "p": 0.3},
                                        {"t": 2.0, "end": 2.2, "w": "lights", "line": 0, "bg": 1, "p": 0.2}])
        self.assertEqual(got["lines"], [{"t": 1.0, "end": 2.2, "text": "Red lights"}])
        self.assertNotIn("model", got)

    def test_none_without_words_or_file(self):
        self.assertIsNone(self.lib.lyrics("nothere"))
        self.write("inst", {"source": "none", "words": [], "lines": []})
        self.assertIsNone(self.lib.lyrics("inst"))

    def test_read_again_when_rewritten(self):
        self.write("s1", {"words": [{"t": 1.0, "end": 1.2, "w": "one", "line": 0}], "lines": []})
        self.assertEqual(self.lib.lyrics("s1")["words"][0]["w"], "one")
        path = os.path.join(self.lib.dir, "lyrics", "s1.json")
        self.write("s1", {"words": [{"t": 1.0, "end": 1.2, "w": "two", "line": 0}], "lines": []})
        later = time.time() + 5
        os.utime(path, (later, later))
        self.assertEqual(self.lib.lyrics("s1")["words"][0]["w"], "two")


if __name__ == "__main__":
    unittest.main()
