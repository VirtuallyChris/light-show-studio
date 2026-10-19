"""Lyrics with word times: run with  .venv\\Scripts\\python -m unittest tests.test_lyrics -v

Only the parts that need no model: reading LRC, picking the LRCLIB record, moving the lyrics' lines
onto the copy, and writing the file. No network (LRCLIB is a stand-in) and no graphics card.
"""

import json
import os
import shutil
import tempfile
import unittest

import numpy as np

from lightshow.studio import lyrics as L


class TestWords(unittest.TestCase):
    def test_words_lose_their_punctuation_but_keep_apostrophes(self):
        self.assertEqual(L.clean("(hold"), "hold")
        self.assertEqual(L.clean("right,"), "right")
        self.assertEqual(L.clean("don’t"), "don't")
        self.assertEqual(L.clean("oh-oh-oh"), "oh-oh-oh")
        self.assertEqual(L.clean("..."), "")

    def test_the_aligner_reads_plain_letters_and_spelled_numbers(self):
        self.assertEqual(L.norm("Café"), "cafe")
        self.assertEqual(L.norm("oh-oh"), "ohoh")
        self.assertEqual(L.norm("22"), "twentytwo")
        self.assertEqual(L.spoken_number(7), "seven")
        self.assertEqual(L.spoken_number(40), "forty")
        self.assertEqual(L.spoken_number(1999), "one nine nine nine")
        self.assertEqual(L.norm("'til"), "til")

    def test_backing_vocals_in_parentheses_are_marked(self):
        words = L.split_line("Come on (come on, yeah) go")
        self.assertEqual([w["w"] for w in words], ["Come", "on", "come", "on", "yeah", "go"])
        self.assertEqual([w["bg"] for w in words], [False, False, True, True, True, False])

    def test_latin_or_not(self):
        self.assertTrue(L.latin("Sometimes I just can't take it"))
        self.assertTrue(L.latin("Corazón, mañana"))
        self.assertFalse(L.latin("사랑해 너를"))


class TestLrc(unittest.TestCase):
    def test_lines_with_their_times_in_order(self):
        text = "[ar: Someone]\n[00:12.50] First line\n[00:10.00]Earlier line\n[00:15.00]\n[01:02.25]Late line"
        lines = L.parse_lrc(text)
        self.assertEqual([(x["t"], x["text"]) for x in lines], [(10.0, "Earlier line"), (12.5, "First line"), (62.25, "Late line")])

    def test_one_line_under_several_stamps_is_sung_each_time(self):
        lines = L.parse_lrc("[00:30.00][01:30.00]Chorus line\n[01:00.00]Verse")
        self.assertEqual([(x["t"], x["text"]) for x in lines], [(30.0, "Chorus line"), (60.0, "Verse"), (90.0, "Chorus line")])

    def test_word_stamps_and_section_labels(self):
        lines = L.parse_lrc("[00:05.00]<00:05.00>Hello <00:05.60>there\n[00:08.00][Chorus]")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "Hello there")
        self.assertEqual(lines[0]["word_t"], [5.0, 5.6])

    def test_plain_lyrics(self):
        lines = L.plain_lines("[Verse 1]\nOne line\n\n  Two   line \n")
        self.assertEqual([x["text"] for x in lines], ["One line", "Two line"])
        self.assertTrue(all(x["t"] is None for x in lines))

    def test_synced_lyrics_win_over_plain(self):
        lines, synced = L.lyric_lines({"syncedLyrics": "[00:01.00]a b", "plainLyrics": "a b"})
        self.assertTrue(synced)
        lines, synced = L.lyric_lines({"syncedLyrics": None, "plainLyrics": "a b"})
        self.assertFalse(synced)
        self.assertEqual(L.lyric_lines(None), ([], False))


class TestLookup(unittest.TestCase):
    REC = {"id": 1, "trackName": "Higher Power", "artistName": "Coldplay", "duration": 212.0, "syncedLyrics": "[00:01.00]x", "plainLyrics": "x"}

    def test_the_closest_length_of_the_right_song(self):
        recs = [dict(self.REC, id=1, duration=260.0),                         # another version, much longer
                dict(self.REC, id=2, duration=211.0),
                dict(self.REC, id=3, trackName="Lower Power", duration=211.0),
                dict(self.REC, id=4, artistName="Someone Else", duration=211.0)]
        self.assertEqual(L.pick_record(recs, "Higher Power", "Coldplay", 211.3)["id"], 2)

    def test_synced_preferred_and_extra_words_in_titles_ignored(self):
        recs = [dict(self.REC, id=1, syncedLyrics=None), dict(self.REC, id=2, duration=214.0)]
        self.assertEqual(L.pick_record(recs, "Higher Power - Remastered 2021", "Coldplay", 212.0)["id"], 2)
        self.assertIsNone(L.pick_record([dict(self.REC, plainLyrics=None, syncedLyrics=None)], "Higher Power", "Coldplay", 212))

    def test_get_first_then_search(self):
        asked = []

        def get(url):
            asked.append(url)
            if "/get?" in url:
                return 404, b'{"message": "not found"}'
            return 200, json.dumps([self.REC]).encode()

        rec = L.lookup("Higher Power", "Coldplay", "Higher Power", 211.3, get=get)
        self.assertEqual(rec["id"], 1)
        self.assertIn("/get?", asked[0])
        self.assertIn("duration=211", asked[0])
        self.assertIn("/search?", asked[1])

    def test_nothing_found(self):
        self.assertIsNone(L.lookup("x", "y", get=lambda url: (200, b"[]")))


def heard_from(lines, offset, every=0.4):
    """What Whisper would hear: each lyric word at its line's time plus `offset`, `every` s apart."""
    out = []
    for ln in lines:
        for k, w in enumerate(L.split_line(ln["text"])):
            t = ln["t"] + offset + every * k
            out.append({"t": t, "end": t + 0.3, "w": w["w"].lower(), "p": 0.9})
    return out


LINES = [{"t": 10.0, "text": "I see the red light shining"},
         {"t": 14.0, "text": "Fire in the sky tonight"},
         {"t": 18.0, "text": "We jump into the gold"},
         {"t": 22.0, "text": "And never come back down"},
         {"t": 60.0, "text": "I see the red light shining"},
         {"t": 64.0, "text": "Fire in the sky tonight"},
         {"t": 68.0, "text": "We jump into the gold"},
         {"t": 72.0, "text": "And never come back down"}]


class TestPlacing(unittest.TestCase):
    def test_a_copy_with_an_intro_moves_every_line(self):
        heard = heard_from(LINES, offset=6.0)
        found = L.match_heard(LINES, heard)
        self.assertGreater(len(found), 30)
        starts, overall = L.place_lines(LINES, True, found, 200.0, heard)
        self.assertAlmostEqual(overall, 6.0)
        self.assertEqual([round(s, 2) for s in starts], [ln["t"] + 6.0 for ln in LINES])

    def test_an_edit_moves_the_parts_before_and_after_differently(self):
        heard = heard_from(LINES[:4], 2.0) + heard_from(LINES[4:], -20.0)       # 22 s cut out after the first half
        starts, _ = L.place_lines(LINES, True, L.match_heard(LINES, heard), 200.0, heard, slack=22.0)
        self.assertEqual([round(s, 2) for s in starts], [12.0, 16.0, 20.0, 24.0, 40.0, 44.0, 48.0, 52.0])

    def test_a_repeated_chorus_is_not_pinned_on_the_wrong_one(self):
        # Whisper missed the first chorus, and the matching over the whole song paired its lines with
        # the second chorus (Party Monster, Bring Me To Life). That pairing only counts toward the
        # song's one offset; near where the lyrics put the first chorus nothing was heard, so it keeps it.
        lines = LINES + [{"t": 30.0, "text": "one more line heard"}, {"t": 40.0, "text": "and yet another one"},
                         {"t": 90.0, "text": "a line after the chorus"}]
        lines.sort(key=lambda x: x["t"])
        heard = sorted(heard_from([ln for ln in lines if ln["t"] > 25], 1.0), key=lambda h: h["t"])
        found = {}
        for li, ln in enumerate(lines):
            true_t = ln["t"] + 1.0 + (50.0 if ln["t"] < 25 else 0.0)            # the first chorus pinned 50 s late
            found[(li, 0)] = true_t
        offs, overall = L.line_offsets(lines, found, heard)
        self.assertAlmostEqual(overall, 1.0)
        self.assertEqual([round(d, 2) for d in offs], [1.0] * len(lines))

    def test_wrong_pairs_outvoting_the_right_ones_do_not_move_the_song(self):
        # In Camera: most lines' first words were paired with a later repeat, 26.5 s on; the median of
        # those pairs moved the whole song. Heard where the lyrics put them, the lines say no offset.
        heard = heard_from(LINES, 0.0)
        found = {(li, 0): ln["t"] + (26.5 if li % 3 else 0.0) for li, ln in enumerate(LINES)}
        offs, overall = L.line_offsets(LINES, found, heard)
        self.assertEqual(overall, 0.0)
        self.assertEqual([round(d, 2) for d in offs], [0.0] * len(LINES))

    def test_lone_common_words_do_not_count(self):
        heard = [{"t": 3.0, "end": 3.2, "w": "the", "p": 0.5}, {"t": 99.0, "end": 99.2, "w": "and", "p": 0.5}]
        self.assertEqual(L.match_heard(LINES, heard), {})
        self.assertEqual(L.line_offsets(LINES, {}), ([0.0] * len(LINES), None))

    def test_plain_lyrics_placed_where_they_were_heard_and_between(self):
        plain = [{"t": None, "text": ln["text"]} for ln in LINES]
        heard = heard_from(LINES[:2], 5.0) + heard_from(LINES[3:], 5.0)        # the third line not heard
        starts, overall = L.place_lines(plain, False, L.match_heard(plain, heard), 200.0)
        self.assertIsNone(overall)
        self.assertAlmostEqual(starts[0], 15.0)
        self.assertAlmostEqual(starts[1], 19.0)
        self.assertTrue(starts[1] < starts[2] < starts[3])
        self.assertAlmostEqual(starts[3], 27.0)
        self.assertEqual(starts, sorted(starts))

    def test_plain_lyrics_nobody_heard_cannot_be_placed(self):
        plain = [{"t": None, "text": ln["text"]} for ln in LINES]
        self.assertEqual(L.place_lines(plain, False, {}, 200.0), ([], None))

    def test_windows_stop_short_of_a_long_gap(self):
        starts = [10.0, 14.0, 60.0]
        lines = [{"text": "one two three"}, {"text": "four five"}, {"text": "six"}]
        win = L.windows(starts, lines, 65.0)
        self.assertEqual(win[0], (9.4, 14.4))
        self.assertAlmostEqual(win[1][1], 14.0 + 2.0 + 1.8)                    # not on to 60 s
        self.assertEqual(win[2][1], 62.9)


class TestResult(unittest.TestCase):
    def test_words_in_silence_are_dropped(self):
        level = [0.0] * 100 + [0.5] * 100                                     # quiet first 2 s, singing after
        words = [{"t": 0.5, "end": 0.8, "w": "ghost"}, {"t": 2.5, "end": 2.8, "w": "real"}]
        self.assertEqual([w["w"] for w in L.audible(words, level)], ["real"])

    def test_aligned_words_in_silence_lose_their_confidence(self):
        level = [0.0] * 100 + [0.5] * 100
        words = L.hushed([{"t": 0.5, "end": 0.8, "w": "lost", "p": 0.4}, {"t": 2.5, "end": 2.8, "w": "sung", "p": 0.4}], level)
        self.assertEqual([w["p"] for w in words], [0.0, 0.4])
        self.assertEqual([w["w"] for w in words], ["lost", "sung"])

    def test_vocal_level(self):
        v = np.concatenate([np.zeros(L.SR), 0.5 * np.ones(L.SR)]).astype(np.float32)
        lv = L.vocal_level(v)
        self.assertEqual(len(lv), 2 * L.LEVEL_FPS)
        self.assertAlmostEqual(lv[0], 0.0)
        self.assertAlmostEqual(lv[-1], 0.5, places=5)

    def test_words_never_go_back_in_time(self):
        words = L.tidy([{"t": 1.0, "end": 1.2}, {"t": 0.9, "end": 0.95}, {"t": 2.0, "end": 2.5}])
        self.assertEqual([(w["t"], w["end"]) for w in words], [(1.0, 1.2), (1.0, 1.0), (2.0, 2.5)])

    def test_whisper_words_as_the_output(self):
        out = L.heard_only([{"t": 1.23456, "end": 1.5, "w": "hey", "p": 0.87654}, {"t": 2, "end": 2.1, "w": "", "p": 1}])
        self.assertEqual(out, [{"t": 1.235, "end": 1.5, "w": "hey", "p": 0.877}])

    def test_written_whole(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "lyrics", "abc.json")
            data = L.result([{"t": 1.0, "end": 1.2, "w": "rojo", "p": 0.9, "line": 0}], [{"t": 1.0, "end": 1.2, "text": "rojo"}],
                            source="lrclib", language="es", model="m")
            L.write(path, data)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["abc.json"])        # no .tmp left behind
            back = L.read(path)
            self.assertEqual(back["words"][0]["w"], "rojo")
            self.assertEqual(back["version"], L.VERSION)
            for key in ("language", "model", "source", "lines", "made_at"):
                self.assertIn(key, back)
            with open(path, "w") as f:
                f.write("{half")
            self.assertIsNone(L.read(path))
            self.assertIsNone(L.read(os.path.join(d, "missing.json")))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
