"""learning a playlist: a party playlist learned ahead of time. Fakes only: no Spotify, nothing plays."""

import threading
import unittest
from types import SimpleNamespace

from lightshow.studio import playlist_learning as pl
from lightshow.studio.playlist_learning import PreListen, playlist_id
from lightshow.studio.spotify import Track


def track(i, secs=180.0):
    return Track(id=f"t{i}", name=f"Song {i}", artists=["Band"], duration=secs, uri=f"spotify:track:t{i}")


class FakeLibrary:
    def __init__(self, learned=(), own=()):
        self.learned, self.own = set(learned), set(own)

    def has_learned(self, tid):
        return tid in self.learned

    def match_project(self, t):
        return "my show" if t.id in self.own else None


class FakeSpotify:
    def __init__(self, tracks):
        self.tracks, self.played, self.paused, self.last_error = tracks, None, False, ""
        self.np = SimpleNamespace(track=None, playing=False, device="Light Show Studio")

    def playlist_tracks(self, pid):
        self.pid = pid
        return list(self.tracks)

    def queue(self):
        return list(self.tracks)

    def play_uris(self, dev, uris):
        self.played = (dev, list(uris))
        return True

    def now_playing(self):
        return self.np

    def pause(self):
        self.paused = True


class TestPreListen(unittest.TestCase):
    def setUp(self):
        self.saved = (pl.CHECK_S, pl.TAKEOVER_S)
        pl.CHECK_S, pl.TAKEOVER_S = 0.02, 0.06
        self.party = []

    def tearDown(self):
        pl.CHECK_S, pl.TAKEOVER_S = self.saved

    def make(self, tracks, lib):
        sp = FakeSpotify(tracks)
        p = PreListen(sp, lib, start_party=lambda: self.party.append("start"), stop_party=lambda: self.party.append("stop"),
                      device_id=lambda: "studio-device")
        return p, sp

    def wait(self, p, cond, secs=3.0):
        ev = threading.Event()
        for _ in range(int(secs / 0.02)):
            if cond():
                return True
            ev.wait(0.02)
        return cond()

    def test_it_plays_only_what_is_not_learned_and_stops_when_all_are(self):
        lib = FakeLibrary(learned={"t1"}, own={"t3"})
        p, sp = self.make([track(1), track(2), track(3), track(4)], lib)
        st = p.start("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc")
        self.assertEqual((st["total"], st["skipped"]), (2, 2))            # one learned already, one is your own show
        self.assertTrue(self.wait(p, lambda: sp.played is not None))
        self.assertEqual(sp.played, ("studio-device", ["spotify:track:t2", "spotify:track:t4"]))
        self.assertEqual(sp.pid, "37i9dQZF1DXcBWIGoYBM5M")
        lib.learned |= {"t2"}
        self.assertTrue(self.wait(p, lambda: p.status()["learned"] == 1))
        lib.learned |= {"t4"}
        self.assertTrue(self.wait(p, lambda: not p.status()["running"]))
        self.assertIn("learned all 2", p.status()["note"])
        self.assertTrue(sp.paused)
        self.assertEqual(self.party, ["start", "stop"])

    def test_a_phone_taking_spotify_back_stops_it(self):
        p, sp = self.make([track(1), track(2)], FakeLibrary())
        p.start("queue")
        self.assertTrue(self.wait(p, lambda: sp.played is not None))
        sp.np = SimpleNamespace(track=track(9), playing=True, device="iPhone")
        self.assertTrue(self.wait(p, lambda: not p.status()["running"]))
        self.assertIn("iPhone", p.status()["note"])
        self.assertFalse(sp.paused)                                       # never pauses someone else's music
        self.assertEqual(self.party, ["start", "stop"])

    def test_nothing_to_learn(self):
        p, sp = self.make([track(1)], FakeLibrary(learned={"t1"}))
        st = p.start("queue")
        self.assertFalse(st["running"])
        self.assertEqual(self.party, [])
        self.assertIsNone(sp.played)

    def test_playlist_links(self):
        self.assertEqual(playlist_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x"), "37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(playlist_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")
        self.assertIsNone(playlist_id("not a link"))
        with self.assertRaises(ValueError):
            PreListen(FakeSpotify([]), FakeLibrary(), lambda: None, lambda: None, lambda: "d").start("not a link")


if __name__ == "__main__":
    unittest.main()
