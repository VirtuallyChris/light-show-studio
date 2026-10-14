"""Learn ahead: queued songs learned from a copy before they play. Fakes only: nothing is downloaded."""

import os
import threading
import unittest

from lightshow.studio.fetch_ahead import FetchAhead
from lightshow.studio.spotify import Track


def track(i, secs=180.0):
    return Track(id=f"t{i}", name=f"Song {i}", artists=["Band"], duration=secs, uri=f"spotify:track:t{i}")


class FakeLibrary:
    def __init__(self, own=()):
        self.learned, self.own, self.saved = {}, set(own), []
        self.lock = threading.Lock()

    def has_learned(self, tid):
        return tid in self.learned

    def match_project(self, t):
        return "my show" if t.id in self.own else None

    def save_learned(self, t, a, source="heard"):
        with self.lock:
            self.learned[t.id] = a
            self.saved.append((t.id, source))


class TestFetchAhead(unittest.TestCase):
    def make(self, lengths=None, missing=()):
        lib, folders, order = FakeLibrary(own={"t9"}), [], []
        lengths = lengths or {}

        def download(t, folder):
            folders.append(folder)
            order.append(t.id)
            if t.id in missing:
                raise LookupError("no copy found")
            path = os.path.join(folder, t.id + ".m4a")
            open(path, "wb").close()
            return path

        f = FetchAhead(lib, download=download, to_flac=lambda p: p, length=lambda p: lengths.get(os.path.basename(p)[:-4], 180.0),
                       analyze=lambda p, cache: {"bpm": 120.0, "from": os.path.basename(p)}, is_available=lambda: True, log=lambda m: None,
                       search=lambda t: [], search_music=lambda t: [])
        return f, lib, folders, order

    def wait(self, cond, secs=5.0):
        ev = threading.Event()
        for _ in range(int(secs / 0.02)):
            if cond():
                return True
            ev.wait(0.02)
        return cond()

    def test_queued_songs_are_learned_in_order_and_the_copies_deleted(self):
        f, lib, folders, order = self.make()
        n = f.submit([track(1), track(2), track(9), track(3)])
        self.assertEqual(n, 3)                                            # your own show needs nothing
        self.assertTrue(self.wait(lambda: len(lib.learned) == 3 and f.idle()))
        self.assertEqual(order, ["t1", "t2", "t3"])                       # the next to play first
        self.assertEqual({s for _, s in lib.saved}, {"youtube"})
        self.assertTrue(all(not os.path.exists(d) for d in folders))      # only the numbers stay
        self.assertEqual(f.submit([track(1)]), 0)                         # learned already: not again

    def test_another_version_or_no_copy_is_left_to_the_jam(self):
        f, lib, folders, _ = self.make(lengths={"t2": 240.0}, missing={"t3"})
        f.submit([track(1), track(2), track(3)])
        self.assertTrue(self.wait(f.idle))
        self.assertEqual(set(lib.learned), {"t1"})
        why = {x["label"]: x["why"] for x in f.status()["failed"]}
        self.assertIn("another version", why["Band - Song 2"])
        self.assertIn("no copy", why["Band - Song 3"])
        self.assertEqual(f.submit([track(2)]), 0)                         # not tried again straight away
        self.assertTrue(all(not os.path.exists(d) for d in folders))

    def test_the_jams_queue_goes_ahead_of_a_playlist(self):
        f, lib, _, order = self.make()
        gate = threading.Event()
        real = f.download
        f.download = lambda t, folder: (gate.wait(2), real(t, folder))[1]
        f.submit([track(1), track(2), track(3)], first=False)             # a playlist
        self.assertTrue(self.wait(lambda: f.busy["download"] is not None))
        f.submit([track(7)], first=True)                                  # the jam's next song
        gate.set()
        self.assertTrue(self.wait(lambda: len(lib.learned) == 4 and f.idle()))
        self.assertEqual(order, ["t1", "t7", "t2", "t3"])

    def test_a_song_added_to_the_end_of_the_queue_waits_its_turn(self):
        f, lib, _, order = self.make()
        gate = threading.Event()
        real = f.download
        f.download = lambda t, folder: (gate.wait(2), real(t, folder))[1]
        f.submit([track(1), track(2), track(3)])                          # the queue
        self.assertTrue(self.wait(lambda: f.busy["download"] is not None))
        f.submit([track(2), track(3), track(4)])                          # a song played, one added at the end
        self.assertEqual([t.id for t in f.waiting], ["t2", "t3", "t4"])   # not jumping the line
        self.assertEqual(f.state_of("t1"), ("analyzing", ""))
        self.assertEqual(f.state_of("t4"), ("queued", ""))
        gate.set()
        self.assertTrue(self.wait(lambda: len(lib.learned) == 4 and f.idle()))
        self.assertEqual(order, ["t1", "t2", "t3", "t4"])
        self.assertIsNone(f.state_of("t4"))

    def test_a_refused_download_is_tried_again_at_once(self):
        f, lib, _, order = self.make()
        f.retry_pause = 0.0
        real, refusals = f.download, [2]
        def flaky(t, folder):
            if refusals[0]:
                refusals[0] -= 1
                raise LookupError("YT-DLP download error - HTTP 403")     # YouTube says no, now and then
            return real(t, folder)
        f.download = flaky
        f.submit([track(1)])
        self.assertTrue(self.wait(lambda: f.idle()))
        self.assertIn("t1", lib.learned)                                  # the third try got it
        self.assertEqual(f.status()["failed"], [])

    def test_without_spotdl_nothing_happens(self):
        f = FetchAhead(FakeLibrary(), is_available=lambda: False)
        self.assertEqual(f.submit([track(1)]), 0)
        self.assertFalse(f.status()["available"])


class TestFindingTheRecording(unittest.TestCase):
    """Which copy is the recording Spotify plays: YouTube Music's song first, then a careful YouTube search."""

    def test_youtube_musics_own_song_is_taken_by_artist_name_and_length(self):
        from lightshow.studio.fetch_ahead import pick_ytmusic
        t = Track(id="x", name="Just Friends", artists=["The Strike"], duration=206.0)
        res = [{"id": "other", "duration": 160, "title": "Just Friends", "artists": ["SMSHNG HRTS"]},
               {"id": "live", "duration": 205, "title": "Just Friends (Live)", "artists": ["The Strike"]},
               {"id": "right", "duration": 207, "title": "Just Friends", "artists": ["The Strike"]}]
        self.assertEqual(pick_ytmusic(t, res), "https://music.youtube.com/watch?v=right")
        self.assertIsNone(pick_ytmusic(t, res[:2]))                       # another artist, or a live take: no

    def test_youtube_only_from_the_artists_own_channel_and_never_a_live_take(self):
        from lightshow.studio.fetch_ahead import pick_alternative
        t = Track(id="x", name="Voices Carry", artists=["'Til Tuesday"], duration=264.0)
        res = [{"id": "video", "duration": 259, "channel": "TilTuesdayVEVO", "title": "'Til Tuesday - Voices Carry (Official Video)"},
               {"id": "fan", "duration": 264, "channel": "Infinite Music", "title": "'Til Tuesday - Voices Carry (Lyrics)"},
               {"id": "topic", "duration": 264, "channel": "'Til Tuesday - Topic", "title": "Voices Carry"},
               {"id": "live", "duration": 263, "channel": "Til Tuesday", "title": "Voices Carry (Live at The Ritz)"}]
        self.assertEqual(pick_alternative(t, res), "https://www.youtube.com/watch?v=topic")

    def test_youtube_music_goes_first_and_spotdls_pick_is_the_fallback(self):
        lib, got = FakeLibrary(), []
        def download_from(t, folder, url):
            got.append(url)
            path = os.path.join(folder, t.id + ".m4a"); open(path, "wb").close(); return path
        f = FetchAhead(lib, download=lambda t, folder: self.fail("spotDL's own pick was not needed"), to_flac=lambda p: p,
                       length=lambda p: 180.0, analyze=lambda p, c: {"bpm": 1.0}, is_available=lambda: True, log=lambda m: None,
                       search=lambda t: [], download_from=download_from,
                       search_music=lambda t: [{"id": "abc", "duration": 180, "title": t.name, "artists": t.artists}])
        f.submit([track(1)])
        ev = threading.Event()
        for _ in range(250):
            if f.idle() and "t1" in lib.learned:
                break
            ev.wait(0.02)
        self.assertEqual(got, ["https://music.youtube.com/watch?v=abc"])
        self.assertIn("t1", lib.learned)


class TestErrorsAreShown(unittest.TestCase):
    """A song whose analysis had anything fail says so: "error" where it would say "analyzed"."""

    def test_a_genre_model_that_does_not_answer_is_noted(self):
        from unittest import mock

        from lightshow.studio import genre
        a = {"mood": {"drive": 0.6}, "problems": []}
        with mock.patch.object(genre.WORKER, "classify_file", lambda p: None), mock.patch.object(genre.WORKER, "available", lambda: True),                 mock.patch.object(genre.WORKER, "failed", "CUDA out of memory"):
            genre.add_genre(a, "C:/x/song.flac")
        self.assertEqual(a["genre"]["source"], "energy")
        self.assertEqual(a["problems"], ["the genre could not be heard (CUDA out of memory)"])

    def test_a_song_heard_as_no_music_is_not_an_error(self):
        from unittest import mock

        from lightshow.studio import genre
        a = {"mood": {"drive": 0.6}, "problems": []}
        with mock.patch.object(genre.WORKER, "classify_file", lambda p: {"Laughter": 0.9}), mock.patch.object(genre.WORKER, "available", lambda: True):
            genre.add_genre(a, "C:/x/song.flac")
        self.assertEqual(a["problems"], [])                               # it answered: a laugh simply has no genre


class TestLearnedWhilePlaying(unittest.TestCase):
    """The jam hands its queue over, and a first listen learned ahead meanwhile takes the full show."""

    def test_the_queue_is_handed_over_and_a_playing_song_upgraded(self):
        import shutil
        import tempfile

        from tests.test_jam import Sim, song
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=10.0)
            e = sim.engine
            got = []
            e.on_queue = got.append
            sim.spotify.upcoming = [track(5)]
            sim.play(a, ta)
            sim.run(14.0, analyse=False)
            e.refresh_queue()
            self.assertEqual([t.id for t in got[-1]], [ta.id, "t5"])     # the song playing (a first listen) and what is queued
            cur = e.current
            self.assertEqual(cur.kind, "live")
            e.library.save_learned(ta, {"bpm": 128.0, "beats": [], "sections": [], "drops": [], "duration": ta.duration}, source="youtube")
            cur.checked_at = -99.0
            self.assertIn("upgraded", e.analyse() or "")
            self.assertEqual(cur.kind, "learned")
            self.assertTrue(cur.saved)                                    # the copy's analysis is kept, not what was heard
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
