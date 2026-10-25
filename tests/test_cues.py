"""Cues: a button that fades the music out, plays a saved show, and brings the music back.

Rehearsals only: nothing here touches a speaker, a light or Spotify."""

import unittest

from unittest import mock

from lightshow.studio import cues as cues_module
from lightshow.studio.cues import CueRunner, Rehearsal, clean

# (the music's 2 s fade back after every cue, shortened here so the tests do not wait on it)
def setUpModule():
    global _short
    _short = mock.patch.object(cues_module, "MUSIC_BACK_S", 0.2)
    _short.start()


def tearDownModule():
    _short.stop()


class Store:
    def __init__(self):
        self.cfg = {}

    def load(self):
        return dict(self.cfg)

    def save(self, cfg):
        self.cfg = dict(cfg)


class Watch(Rehearsal):
    def __init__(self, length):
        super().__init__(lambda name: length)


class TestCues(unittest.TestCase):
    def runner(self):
        st = Store()
        r = CueRunner(st.load, st.save, lambda name: 0.3)
        r.replace([{"id": "door", "name": "Doorbell", "show": "Ding"},
                   {"id": "shot", "name": "Shot o'clock", "show": "Shots"},
                   {"id": "empty", "name": "Nothing yet", "show": ""}])
        return r

    def test_the_two_to_start_with(self):
        st = Store()
        names = [c["name"] for c in CueRunner(st.load, st.save, lambda n: 0).list()]
        self.assertEqual(names, ["Doorbell", "Shot o'clock"])

    def test_cuts_to_the_show_plays_it_to_the_end_then_the_music_comes_back(self):
        """The user: no fade out, no overlapping; the show to its end, then the music fades back in over 2 s."""
        r, h = self.runner(), Watch(0.3)
        r.run("door", h)
        r.thread.join(5)
        self.assertEqual(h.calls, ["pause", ("show", "Ding"), "resume"])
        self.assertEqual(r.status()["phase"], "done")
        self.assertEqual(r.status()["steps"], ["pause", "show", "resume", "fade_in", "done"])

    def test_ending_early_brings_the_music_back(self):
        r, h = self.runner(), Watch(30)
        r.run("door", h)
        import time
        for _ in range(50):
            if r.status().get("phase") == "show":
                break
            time.sleep(0.05)
        r.stop()
        r.thread.join(5)
        self.assertIn("stop show", h.calls)
        self.assertEqual(h.calls[-1], "resume")

    def test_a_cue_needs_a_show_and_one_at_a_time(self):
        r = self.runner()
        with self.assertRaises(ValueError):
            r.run("empty", Watch(1))
        r.run("door", Watch(1))
        with self.assertRaises(RuntimeError):
            r.run("shot", Watch(1))
        r.stop()
        r.thread.join(5)

    def test_a_rehearsal_is_the_default_and_says_so(self):
        r = self.runner()
        st = r.run("shot")
        self.assertTrue(st["rehearsal"])
        r.thread.join(5)

    def test_settings_are_kept_in_range_and_the_old_fades_dropped(self):
        c = clean([{"name": "x" * 50, "fade_out": {"on": True, "s": 99}, "fade_in": {"s": "soon"}}])[0]
        self.assertEqual(len(c["name"]), 30)
        self.assertNotIn("fade_out", c)                                    # (no per-cue fades any more)
        self.assertNotIn("fade_in", c)

    def test_a_cue_saved_with_one_show_has_a_list_of_one(self):
        c = clean([{"id": "doorbell", "name": "Doorbell", "show": "Thriller End Laugh"}])[0]
        self.assertEqual(c["shows"], ["Thriller End Laugh"])

    def test_several_shows_each_once_before_any_again(self):
        st = Store()
        r = CueRunner(st.load, st.save, lambda name: 0)
        shows = ["Haunted_Mansion", "Werewolf", "Psycho", "Thriller End Laugh"]
        cue = r.replace([{"id": "doorbell", "name": "Doorbell", "shows": shows + ["Psycho", ""]}])[0]
        self.assertEqual(cue["shows"], shows)                              # (no repeats, no blanks)
        self.assertEqual(cue["show"], "Haunted_Mansion")
        picks = [r.pick(cue) for _ in range(40)]
        for k in range(0, 40, 4):
            self.assertEqual(sorted(picks[k:k + 4]), sorted(shows))        # every round: each one once
        self.assertTrue(all(a != b for a, b in zip(picks, picks[1:])))     # never the same twice running
        self.assertEqual(r.pick({"id": "one", "shows": ["Ding"], "show": "Ding"}), "Ding")

    def test_a_ring_plays_one_of_its_shows(self):
        st = Store()
        r = CueRunner(st.load, st.save, lambda name: 0.05)
        r.replace([{"id": "door", "name": "Doorbell", "shows": ["A", "B"], "fade_out": {"on": False, "s": 0}, "fade_in": {"on": False, "s": 0}}])
        played = []
        for _ in range(4):
            h = Watch(0.05)
            r.run("door", hooks=h)
            r.thread.join(5)
            played += [c[1] for c in h.calls if isinstance(c, tuple) and c[0] == "show"]
        self.assertEqual(sorted(played[:2]), ["A", "B"])
        self.assertEqual(sorted(played[2:]), ["A", "B"])



class FakeCast:
    def __init__(self, connected=True):
        self.cast = object() if connected else None
        self.name = "Everywhere" if connected else ""
        self.levels, self.calls = [], []

    def status(self):
        return {"volume": 0.5 if self.cast else None}

    def set_volume(self, v):
        self.levels.append(v)

    def play(self, url, path, title, start=0.0):
        self.calls.append(("play", url, path, title, start))

    def stop(self):
        self.calls.append("stop")


class FakeSpotify:
    def __init__(self, playing):
        from types import SimpleNamespace
        self.signed_in, self.np, self.calls = True, SimpleNamespace(playing=playing), []

    def now_playing(self):
        return self.np

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")


class FakeJam:
    def __init__(self, kind=None, playing=True):
        from types import SimpleNamespace
        self.spotify = FakeSpotify(playing)
        self.calls = []
        self.engine = None if kind is None else SimpleNamespace(
            output=kind, hold=lambda: self.calls.append("hold"),
            fade_out=lambda s: (self.calls.append(("fade out", s)), 0.05)[1],
            release=lambda restart, fade_in=0.0: self.calls.append(("release", restart, fade_in)))

    def _output_kind(self, out):
        return out


class TestLiveCues(unittest.TestCase):
    """The real hooks, against fake speakers, a fake jam and a fake Spotify: nothing plays."""

    def run_cue(self, hooks):
        st = Store()
        r = CueRunner(st.load, st.save, lambda n: 0.2, live=lambda: hooks)
        r.replace([{"id": "door", "name": "Doorbell", "show": "Ding"}])
        st_ = r.run("door")
        r.thread.join(5)
        return st_, r.status()

    def hooks(self, cast, jam):
        from lightshow.studio.cues import LiveHooks
        return LiveHooks(cast, jam, lambda name: (f"http://pc/api/audio/{name}", f"C:/shows/{name}/song.mp3"), lambda n: 0.2)

    def test_a_jam_on_cast_holds_plays_on_its_speakers_and_comes_back(self):
        cast, jam = FakeCast(), FakeJam("cast")
        first, last = self.run_cue(self.hooks(cast, jam))
        self.assertFalse(first["rehearsal"])
        self.assertEqual((first["via"], first["device"]), ("cast", "Everywhere"))
        # a straight cut, and the speakers get the jam back fading in (MUSIC_BACK_S, shortened here)
        self.assertEqual(jam.calls, ["hold", ("release", True, 0.2)])
        self.assertEqual(cast.calls, [("play", "http://pc/api/audio/Ding", "C:/shows/Ding/song.mp3", "Ding", 0.0)])
        self.assertEqual(cast.levels, [])                                    # the speakers' volume never touched: no chimes
        self.assertEqual(last["steps"], ["pause", "show", "resume", "fade_in", "done"])

    def test_a_jam_on_this_pc_leaves_the_song_to_the_page(self):
        cast, jam = FakeCast(connected=False), FakeJam("pc")
        first, last = self.run_cue(self.hooks(cast, jam))
        self.assertEqual(first["via"], "pc")
        self.assertEqual(cast.calls, [])
        self.assertEqual(jam.calls, ["hold", ("release", True, 0.2)])     # this PC's stream fades back in too
        self.assertEqual(last["steps"], ["pause", "show", "resume", "fade_in", "done"])

    def test_without_a_jam_spotify_is_paused_only_if_it_was_playing(self):
        jam = FakeJam(None, playing=True)
        _, last = self.run_cue(self.hooks(FakeCast(connected=False), jam))
        self.assertEqual(jam.spotify.calls, ["pause", "resume"])
        self.assertEqual(last["steps"], ["pause", "show", "resume", "done"])   # no audio of ours to fade
        quiet = FakeJam(None, playing=False)
        self.run_cue(self.hooks(FakeCast(connected=False), quiet))
        self.assertEqual(quiet.spotify.calls, [])

    def test_in_a_jam_the_song_plays_in_the_stream_to_its_end_then_the_music_fades_in(self):
        import numpy as np
        from types import SimpleNamespace
        cast, jam = FakeCast(), FakeJam("cast")
        pos = {"t": 0.0}
        jam.engine.streaming = True
        jam.engine.cue_insert = lambda pcm, fade_in: (jam.calls.append(("insert", len(pcm), fade_in)), True)[1]
        jam.engine.cue_position = lambda: (pos.__setitem__("t", pos["t"] + 0.05), pos["t"])[1]
        jam.engine.cue_over = lambda: jam.calls.append("over")
        from lightshow.studio.cues import LiveHooks
        hooks = LiveHooks(cast, jam, lambda name: ("u", "p"), lambda n: 0.5, pcm=lambda name: (np.zeros((500, 2), dtype=np.int16), 1000))
        first, last = self.run_cue(hooks)
        self.assertEqual(jam.calls, [("insert", 500, 0.2), "over"])       # no pause, no hold: the song was in the stream
        self.assertGreaterEqual(pos["t"], 0.5)                             # over only once the song had played to its end
        self.assertEqual(cast.calls, [])                                   # the speakers never loaded the show's song
        self.assertTrue(last["stream"])
        self.assertEqual(last["steps"], ["show", "fade_in", "done"])

    def test_a_song_not_ready_plays_the_old_way(self):
        cast, jam = FakeCast(), FakeJam("cast")
        jam.engine.streaming = True
        from lightshow.studio.cues import LiveHooks
        hooks = LiveHooks(cast, jam, lambda name: ("u", "p"), lambda n: 0.2, pcm=lambda name: None)
        _, last = self.run_cue(hooks)
        self.assertEqual(last["steps"], ["pause", "show", "resume", "fade_in", "done"])

    def test_a_silent_preview_jam_shows_only_the_lights(self):
        cast, jam = FakeCast(), FakeJam("none")
        first, last = self.run_cue(self.hooks(cast, jam))
        self.assertEqual(first["via"], "none")
        self.assertEqual(cast.calls, [])
        self.assertEqual(jam.calls, ["hold", ("release", False, 0.0)])
        self.assertEqual(last["steps"], ["pause", "show", "resume", "done"])


if __name__ == "__main__":
    unittest.main()
