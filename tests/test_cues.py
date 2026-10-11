"""Cues: a button that fades the music out, plays a saved show, and brings the music back.

Rehearsals only: nothing here touches a speaker, a light or Spotify."""

import unittest

from lightshow.studio.cues import CueRunner, Rehearsal, clean


class Store:
    def __init__(self):
        self.cfg = {}

    def load(self):
        return dict(self.cfg)

    def save(self, cfg):
        self.cfg = dict(cfg)


class Watch(Rehearsal):
    """A rehearsal that also keeps every volume it was set to, in order, with the steps between."""

    def __init__(self, length):
        super().__init__(lambda name: length)
        self.trace = []

    def set_volume(self, v):
        super().set_volume(v)
        self.trace.append(round(v, 3))

    def pause_music(self):
        super().pause_music()
        self.trace.append("pause")

    def resume_music(self):
        super().resume_music()
        self.trace.append("resume")


class TestCues(unittest.TestCase):
    def runner(self):
        st = Store()
        r = CueRunner(st.load, st.save, lambda name: 0.3)
        r.replace([{"id": "door", "name": "Doorbell", "show": "Ding", "fade_out": {"on": True, "s": 0.2}, "fade_in": {"on": True, "s": 0.3}},
                   {"id": "shot", "name": "Shot o'clock", "show": "Shots", "fade_out": {"on": False, "s": 2}, "fade_in": {"on": False, "s": 2}},
                   {"id": "empty", "name": "Nothing yet", "show": ""}])
        return r

    def test_the_two_to_start_with(self):
        st = Store()
        names = [c["name"] for c in CueRunner(st.load, st.save, lambda n: 0).list()]
        self.assertEqual(names, ["Doorbell", "Shot o'clock"])

    def test_fades_out_plays_the_show_then_fades_back_in(self):
        r, h = self.runner(), Watch(0.3)
        r.run("door", h)
        r.thread.join(5)
        self.assertEqual(h.calls, ["pause", ("show", "Ding"), "resume"])
        i_pause, i_resume = h.trace.index("pause"), h.trace.index("resume")
        down = h.trace[:i_pause]
        self.assertEqual(down[-1], 0.0)                                   # faded all the way out first
        self.assertTrue(all(a >= b for a, b in zip(down, down[1:])))
        self.assertEqual(h.trace[i_pause + 1], 0.4)                       # the show plays at the party's level
        self.assertEqual(h.trace[i_resume - 1], 0.0)                      # silent when the music comes back...
        self.assertEqual(h.trace[-1], 0.4)                                # ...and faded back to where it was
        self.assertEqual(r.status()["phase"], "done")
        self.assertEqual(r.status()["steps"], ["fade_out", "pause", "show", "resume", "fade_in", "done"])

    def test_without_fades_the_music_just_stops_and_starts(self):
        r, h = self.runner(), Watch(0.2)
        r.run("shot", h)
        r.thread.join(5)
        self.assertEqual(r.status()["steps"], ["pause", "show", "resume", "done"])
        self.assertNotIn(0.0, h.trace)

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
        self.assertEqual(h.trace[-1], 0.4)

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

    def test_settings_are_kept_in_range(self):
        c = clean([{"name": "x" * 50, "fade_out": {"on": True, "s": 99}, "fade_in": {"s": "soon"}}])[0]
        self.assertEqual(len(c["name"]), 30)
        self.assertEqual(c["fade_out"]["s"], 30.0)
        self.assertEqual(c["fade_in"], {"on": True, "s": 2.0})


if __name__ == "__main__":
    unittest.main()
