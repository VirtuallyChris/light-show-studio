"""Genre and show style: run with  .venv\\Scripts\\python -m unittest tests.test_genre -v

The style rules are tested on their own. The Jam engine is tested with a
stand-in for the model, so no model or GPU is needed. One test runs the real
model on a synthetic song if the model environment is installed; it reads
audio from memory and plays nothing.
"""

import json
import shutil
import tempfile
import unittest
import warnings

import numpy as np

from lightshow.studio.genre import WORKER, spotify_level, style_from
from lightshow.studio.spotify import SpotifyClient, Track, parse_track
from tests.test_jam import SR, Sim, song

warnings.filterwarnings("ignore")


class TestStyle(unittest.TestCase):
    def test_rock_stays_calmer_than_edm_however_loud(self):
        rock = {"Rock music": 0.6, "Grunge": 0.2, "Exciting music": 0.3, "Angry music": 0.3}
        self.assertEqual(style_from(rock, drive=0.99)["style"], "groove")      # a loud rock song is still rock
        self.assertEqual(style_from({"Dubstep": 0.6, "Electronic music": 0.4}, drive=0.8)["style"], "rave")
        self.assertEqual(style_from({"Dubstep": 0.5, "Electronic music": 0.6}, drive=0.8)["style"], "dance")   # more electronic than dubstep
        self.assertEqual(style_from({"Drum and bass": 0.7}, drive=0.7)["style"], "rave")

    def test_the_four_styles(self):
        self.assertEqual(style_from({"Ambient music": 0.5, "New-age music": 0.3}, 0.3)["style"], "chill")
        self.assertEqual(style_from({"Jazz": 0.4, "Tender music": 0.3}, 0.5)["style"], "chill")
        self.assertEqual(style_from({"Pop music": 0.5, "Happy music": 0.2}, 0.6)["style"], "groove")
        self.assertEqual(style_from({"House music": 0.5, "Electronic music": 0.4}, 0.6)["style"], "dance")
        self.assertEqual(style_from({"Heavy metal": 0.6}, 0.8)["style"], "dance")

    def test_energy_moves_a_song_only_a_little(self):
        house = {"House music": 0.5}
        self.assertEqual(style_from(house, 0.2)["style"], "dance")             # a sleepy house track is still dance
        self.assertLessEqual(abs(style_from(house, 1.0)["level"] - style_from(house, 0.0)["level"]), 0.9 + 1e-6)

    def test_with_nothing_to_go_on_it_falls_back_to_energy(self):
        self.assertEqual(style_from(None, 0.9)["style"], "groove")             # loud, but nothing else known: groove
        self.assertEqual(style_from({}, 0.5)["style"], "groove")
        self.assertEqual(style_from({}, 0.5)["source"], "energy")

    def test_spotify_genres(self):
        self.assertEqual(spotify_level(["melodic dubstep", "edm"]), 2.6)
        self.assertIsNone(spotify_level(["zzz unknown"]))
        g = style_from(None, 0.6, ["indie rock", "modern rock"])
        self.assertEqual((g["style"], g["source"]), ("groove", "spotify"))
        self.assertEqual(style_from({"Pop music": 0.4}, 0.6, ["dubstep"])["source"], "listening+spotify")


class TestSpotifyGenres(unittest.TestCase):
    def test_artist_genres_are_fetched_once_per_artist(self):
        calls = []

        def transport(method, url, headers, body):
            calls.append(url)
            if url.endswith("/token"):
                return 200, b'{"access_token":"A","expires_in":3600}'
            return 200, json.dumps({"id": url.rsplit("/", 1)[1], "genres": ["riddim", "dubstep"]}).encode()

        c = SpotifyClient({"client_id": "x", "refresh_token": "r"}, transport=transport)
        t = parse_track({"id": "t1", "type": "track", "name": "Song", "duration_ms": 1000,
                         "artists": [{"id": "a1", "name": "Artist"}]})
        self.assertEqual(t.artist_ids, ["a1"])
        self.assertEqual(c.artist_genres(t), ["riddim", "dubstep"])
        self.assertEqual(c.artist_genres(t), ["riddim", "dubstep"])
        self.assertEqual(sum(1 for u in calls if "/artists/" in u), 1)


class TestJamGenre(unittest.TestCase):
    def test_a_first_listen_gets_its_style_from_memory_and_keeps_it(self):
        d = tempfile.mkdtemp()
        try:
            heard = []

            def fake_model(y, sr):                                  # stands in for the real model
                heard.append((len(y), sr, y.dtype))
                return {"Dubstep": 0.6, "Electronic music": 0.5}

            a, ta = song(140.0, 7)
            sim = Sim(d, target=20.0)
            sim.engine.genre_fn = fake_model
            sim.spotify.artist_genres = lambda t: ["brostep"]
            sim.play(a, ta)
            sim.run(a["duration"] - 1)
            jt = sim.engine.current
            self.assertEqual(len(heard), 1)                         # asked once, not every pass
            self.assertEqual(heard[0][1], 16000)
            self.assertGreaterEqual(heard[0][0], 30 * 16000)        # half a minute of the song
            self.assertEqual(jt.genre["style"], "rave")
            self.assertEqual(jt.genre["source"], "listening+spotify")
            self.assertEqual(sim.engine.analysis_json(jt.key)["analysis"]["genre"]["style"], "rave")
            self.assertEqual(jt.analysis["genre"]["style"], "rave")  # carried through later passes, so it is learned too
            self.assertEqual(sim.engine.state()["ahead"]["style"], "rave")
        finally:
            shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(WORKER.available(), "the model environment is not installed")
class TestRealModel(unittest.TestCase):
    def test_the_model_runs_on_audio_from_memory(self):
        a, _ = song(140.0, 7)
        mono = a["audio"].mean(axis=1)
        from scipy.signal import resample_poly
        y = resample_poly(mono, 16000, SR).astype(np.float32)
        labels = WORKER.classify_audio(y, 16000)
        self.assertIsNotNone(labels, WORKER.failed)
        self.assertIn("Dubstep", labels)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in labels.values()))
        top = max(labels, key=labels.get)
        self.assertIn(top, {"Electronic music", "Techno", "House music", "Electronic dance music", "Dubstep", "Drum and bass",
                            "Electronica", "Dance music", "Trance music", "Video game music"}, f"top label was {top}: {sorted(labels.items(), key=lambda kv: -kv[1])[:5]}")

    @classmethod
    def tearDownClass(cls):
        WORKER.stop()


if __name__ == "__main__":
    unittest.main()
