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


class TestDiscogsStyles(unittest.TestCase):
    """MAEST's labels ("Genre---Style", Discogs) on the same scale as AudioSet's."""

    def test_rock_and_pop_rock_stay_groove(self):
        g = style_from({"Rock---Alternative Rock": 0.56, "Rock---Pop Punk": 0.47, "Rock---Pop Rock": 0.40}, 0.85)
        self.assertEqual((g["style"], g["model"]), ("groove", "maest"))
        self.assertEqual(g["genres"], ["Alternative Rock", "Pop Punk", "Pop Rock"])       # shown without the genre
        take_mine = {"Electronic---Dance-pop": 0.61, "Rock---Pop Punk": 0.22, "Electronic---Tropical House": 0.18}
        self.assertEqual(style_from(take_mine, 0.85)["style"], "groove")                  # AST: "Dubstep", dance

    def test_dance_music_and_bass_music(self):
        self.assertEqual(style_from({"Electronic---Electro House": 0.49, "Electronic---House": 0.44,
                                     "Electronic---Dance-pop": 0.42}, 0.85)["style"], "dance")
        self.assertEqual(style_from({"Electronic---Dubstep": 0.6, "Electronic---Drum n Bass": 0.3}, 0.85)["style"], "rave")
        self.assertIn("Drum and bass", style_from({"Electronic---Drum n Bass": 0.7}, 0.85)["genres"])

    def test_a_style_not_placed_takes_its_genres_place(self):
        self.assertEqual(style_from({"Classical---Baroque": 0.8}, 0.6)["style"], "chill")
        self.assertEqual(style_from({"Non-Music---Spoken Word": 0.9}, 0.6)["style"], "chill")

    def test_a_genre_heard_before_maest_is_heard_again(self):
        from unittest import mock
        from lightshow.studio import genre
        old = {"genre": {"style": "dance", "source": "listening", "scores": {"Dubstep": 0.12, "Pop music": 0.07}},
               "mood": {"drive": 0.8}}
        with mock.patch.object(genre.WORKER, "classify_file", return_value={"Rock---Pop Rock": 0.6}) as ask:
            self.assertTrue(genre.add_genre(old, "song.mp3"))
            self.assertEqual((old["genre"]["style"], old["genre"]["model"]), ("groove", "maest"))
            self.assertFalse(genre.add_genre(old, "song.mp3"))                          # and only once
        self.assertEqual(ask.call_count, 1)

    def test_an_old_ast_genre_still_restyles(self):
        from lightshow.studio.genre import restyle
        g = restyle({"style": "rave", "source": "listening", "scores": {"Rock music": 0.6, "Grunge": 0.2}, "genres": ["Rock music"]}, 0.7)
        self.assertEqual((g["style"], g["model"]), ("groove", "ast"))


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
        self.assertTrue(labels)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in labels.values()))
        top = max(labels, key=labels.get)
        if "---" in top:                                                   # MAEST: Discogs styles (a synth song: mostly electronic,
            top5 = sorted(labels, key=labels.get, reverse=True)[:5]        # a soundtrack or chiptune maybe)
            self.assertGreaterEqual(sum(n.startswith("Electronic---") for n in top5), 3, f"top styles were {top5}")
        else:                                                              # AST, where MAEST could not load
            self.assertIn(top, {"Electronic music", "Techno", "House music", "Electronic dance music", "Dubstep", "Drum and bass",
                                "Electronica", "Dance music", "Trance music", "Video game music"}, f"top label was {top}")

    @classmethod
    def tearDownClass(cls):
        WORKER.stop()


if __name__ == "__main__":
    unittest.main()
