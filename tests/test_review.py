"""Review mode (review.py): which learned songs can be reviewed, and what the user's word on their sections keeps."""

import json
import os
import tempfile
import unittest

from lightshow.studio.review import Review


class FakeLibrary:
    def __init__(self, root, shows=()):
        self.dir = root
        self.learned_dir = os.path.join(root, "learned")
        self.shows = set(shows)
        os.makedirs(self.learned_dir)
        os.makedirs(os.path.join(root, "copies"))

    def add(self, tid, name, artists, copy=True):
        with open(os.path.join(self.learned_dir, tid + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "track": {"id": tid, "name": name, "artists": artists, "duration": 200.0}, "analysis": {}}, fh)
        if copy:
            open(os.path.join(self.dir, "copies", tid + ".flac"), "wb").close()

    def match_project(self, track):
        return track.name if track.name in self.shows else None


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = FakeLibrary(self.tmp.name, shows={"Has A Show"})
        self.lib.add("aaa", "Zed", ["B Band"])
        self.lib.add("bbb", "Has A Show", ["C"])
        self.lib.add("ccc", "No Copy", ["D"], copy=False)
        self.lib.add("ddd", "Alpha", ["A Artist"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_songs_are_learned_with_a_copy_and_no_show(self):
        songs = Review(self.lib).songs()
        self.assertEqual([s["id"] for s in songs], ["ddd", "aaa"])          # by artist and title
        self.assertEqual(songs[0]["label"], "A Artist - Alpha")

    def test_reviewed_songs_come_last_whatever_the_ranking_says(self):
        with open(os.path.join(self.tmp.name, "review_order.json"), "w") as fh:
            json.dump({"aaa": 0.9, "ddd": 0.1}, fh)
        r = Review(self.lib)
        self.assertEqual([s["id"] for s in r.songs()], ["aaa", "ddd"])                # the most doubted first
        r.keep("aaa", [{"type": "verse", "start": 0, "end": 10}], ok=False)
        self.assertEqual([s["id"] for s in r.songs()], ["ddd", "aaa"])                # reviewed: last

    def test_keep_writes_the_whole_song_and_its_drops(self):
        r = Review(self.lib)
        r.keep("aaa", [{"type": "drop", "start": 40, "end": 60}, {"type": "verse", "start": 0, "end": 40}], ok=False)
        with open(r.truth_path, encoding="utf-8") as fh:
            truth = json.load(fh)
        self.assertEqual(truth["aaa"]["drops"], [40.0])
        self.assertEqual([s["type"] for s in truth["aaa"]["sections"]], ["verse", "drop"])
        self.assertEqual(truth["aaa"]["reviewed"], "corrected")
        self.assertIn("_about", truth)
        self.assertEqual(r.songs()[1]["reviewed"], "corrected")

    def test_claudes_review_is_marked_and_the_users_own_replaces_it(self):
        r = Review(self.lib)
        secs = [{"type": "verse", "start": 0, "end": 10}]
        r.keep("aaa", secs, ok=False, by="claude")
        self.assertEqual((r.truth()["aaa"]["by"], r.truth()["aaa"]["reviewed"]), ("claude", "claude"))
        r.keep("aaa", secs, ok=True)                                  # the user agrees: theirs now, unmarked
        self.assertNotIn("by", r.truth()["aaa"])
        self.assertEqual(r.truth()["aaa"]["reviewed"], "right as found")

    def test_a_suggestion_comes_first_and_goes_once_the_user_has_decided(self):
        r = Review(self.lib)
        secs = [{"type": "verse", "start": 0, "end": 10}]
        r.propose("aaa", secs)
        songs = r.songs()
        self.assertEqual(songs[0]["id"], "aaa")                       # waiting for a yes or no: first
        self.assertTrue(songs[0]["proposal"])
        self.assertEqual(r.proposals()["aaa"]["sections"], [{"type": "verse", "start": 0.0, "end": 10.0}])
        self.assertNotIn("aaa", r.truth())                           # a suggestion is not a review
        r.keep("aaa", secs, ok=False)                                # Done: the user's review
        self.assertEqual(r.proposals(), {})
        self.assertEqual(r.truth()["aaa"]["reviewed"], "corrected")

    def test_a_test_copy_never_writes(self):
        r = Review(self.lib, read_only=True)
        r.keep("aaa", [{"type": "verse", "start": 0, "end": 10}], ok=True)
        self.assertFalse(os.path.exists(r.truth_path))

    def test_unknown_type_is_refused(self):
        with self.assertRaises(ValueError):
            Review(self.lib).keep("aaa", [{"type": "chorus", "start": 0, "end": 10}], ok=False)


    def test_every_song_lists_all_learned_songs_with_a_copy_and_their_shows(self):
        songs = Review(self.lib).every_song()
        self.assertEqual([(s["id"], s["show"]) for s in songs], [("ddd", None), ("aaa", None), ("bbb", "Has A Show")])       # by artist and title

    def test_a_hand_edited_party_song_becomes_a_show_of_its_own(self):
        # "if i start adding a bunch of additional lights then it would be a custom show fully"
        self.lib.shows_dir = os.path.join(self.tmp.name, "shows")
        linked = {}
        self.lib.link = lambda tid, name: linked.__setitem__(tid, name)
        name = Review(self.lib).make_custom("aaa", {"name": "x", "lanes": [{"clips": [{"effect": "solid"}]}]})
        self.assertEqual(name, "B Band - Zed")
        pdir = os.path.join(self.lib.shows_dir, name)
        self.assertTrue(os.path.exists(os.path.join(pdir, "song.flac")))                # the song comes with it
        with open(os.path.join(pdir, "project.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["lanes"][0]["clips"][0]["effect"], "solid")
        self.assertEqual(linked, {"aaa": name})                                          # the party plays it

    def test_undone_back_to_the_jams_show_the_custom_show_is_set_aside(self):
        # "if i undo it should re grey out": the show made for the song goes (to the Recycle Bin), unpinned
        from lightshow.studio import review as review_mod
        self.lib.shows_dir = os.path.join(self.tmp.name, "shows")
        linked = {}
        self.lib.link = lambda tid, name: linked.pop(tid, None) if name is None else linked.__setitem__(tid, name)
        self.lib.links = lambda: linked
        r = Review(self.lib)
        name = r.make_custom("aaa", {"lanes": []})
        recycled = []
        old, review_mod._recycle = review_mod._recycle, lambda path, aside: recycled.append(path)
        try:
            self.assertFalse(r.drop_custom("aaa", "Some Other Show"))                     # only the show pinned to it
            self.assertTrue(r.drop_custom("aaa", name))
        finally:
            review_mod._recycle = old
        self.assertEqual(linked, {})
        self.assertEqual(recycled, [os.path.join(self.lib.shows_dir, name)])

    def test_a_test_copy_makes_no_show(self):
        self.lib.shows_dir = os.path.join(self.tmp.name, "shows")
        Review(self.lib, read_only=True).make_custom("aaa", {"lanes": []})
        self.assertFalse(os.path.exists(self.lib.shows_dir))

    def test_a_link_to_a_deleted_show_is_ignored(self):
        from lightshow.studio.party import PartyLibrary
        from lightshow.studio.spotify import Track
        lib = PartyLibrary(os.path.join(self.tmp.name, "real"))
        lib.link("t1", "Gone Show")
        self.assertIsNone(lib.match_project(Track(id="t1", name="Some Song", artists=["X"], duration=100.0)))
        os.makedirs(os.path.join(lib.shows_dir, "Gone Show"))
        open(os.path.join(lib.shows_dir, "Gone Show", "project.json"), "w").close()
        self.assertEqual(lib.match_project(Track(id="t1", name="Some Song", artists=["X"], duration=100.0)), "Gone Show")


class BeatFixTest(unittest.TestCase):
    def test_shift_beats_moves_the_grid_and_keeps_the_bars(self):
        from lightshow.studio.party import shift_beats
        a = {"beats": [0.2 + 0.5 * i for i in range(16)], "downbeat_phase": 0, "downbeats": []}
        bar_one = a["beats"][4]
        shift_beats(a, -0.3)                                   # the first beat goes before the start
        self.assertAlmostEqual(a["beats"][0], 0.4)
        self.assertIn(round(bar_one - 0.3, 4), a["downbeats"])
        shift_beats(a, 0.5)                                    # room for a beat at the start again
        self.assertAlmostEqual(a["beats"][0], 0.4)
        self.assertIn(round(bar_one + 0.2, 4), a["downbeats"])

    def test_a_grid_moved_under_review_becomes_the_songs_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = FakeLibrary(tmp)
            lib.add("aaa", "Zed", ["B"])
            fixes = {}
            lib.beat_fixes = lambda: dict(fixes)
            lib.set_beat_fix = lambda tid, s: fixes.__setitem__(tid, s)
            r = Review(lib)
            r.keep("aaa", [{"type": "verse", "start": 0, "end": 10}], ok=False, beat_shift=0.16)
            r.keep("aaa", [{"type": "verse", "start": 0, "end": 10}], ok=False, beat_shift=0.01)
            self.assertAlmostEqual(fixes["aaa"], 0.17)


class ReviewedSectionsPlayTest(unittest.TestCase):
    def test_the_users_sections_replace_the_rules(self):
        from lightshow.studio.analysis import settle_sections
        a = {"sections": [{"type": "drop", "start": 0.0, "end": 10.0}], "drops": [0.0], "beats": [], "duration": 10.0,
             "sections_truth": [{"type": "verse", "start": 0.0, "end": 10.0}]}
        settle_sections(a)
        self.assertEqual([s["type"] for s in a["sections"]], ["verse"])
        self.assertEqual(a["drops"], [])                        # a song with no drops at all, as the user said

    def test_done_without_changes_keeps_a_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = FakeLibrary(tmp)
            lib.add("aaa", "Zed", ["B"])
            r = Review(lib)
            secs = [{"type": "verse", "start": 0.0, "end": 10.0}]
            r.keep("aaa", secs, ok=False)
            r.keep("aaa", secs, ok=True)
            self.assertEqual(r.truth()["aaa"]["reviewed"], "corrected")


if __name__ == "__main__":
    unittest.main()
