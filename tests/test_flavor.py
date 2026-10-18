"""What makes one kind of song different: 808s, hat rolls, wobbles, a key change. Synthetic songs only."""

import unittest

import numpy as np

from lightshow.studio import flavor

SR = 22050


def tone(freq, secs, decay=None, sr=SR):
    t = np.arange(int(secs * sr)) / sr
    env = np.exp(-t / decay) if decay else np.ones_like(t)
    return (np.sin(2 * np.pi * freq * t) * env).astype(np.float32)


class TestFlavor(unittest.TestCase):
    def test_a_held_808_is_found_and_a_pluck_is_not(self):
        b = np.zeros(SR * 8, dtype=np.float32)
        hits = []
        for k, t in enumerate((1.0, 3.0, 5.0)):                       # held, low notes
            n = tone(45, 1.2, decay=1.5)
            b[int(t * SR):int(t * SR) + len(n)] += n
            hits.append((t, 1.0))
        p = tone(90, 0.3, decay=0.05)                                  # a short pluck
        b[int(6.5 * SR):int(6.5 * SR) + len(p)] += p
        hits.append((6.5, 1.0))
        found = flavor.find_808s(b, SR, hits)
        self.assertEqual([round(t) for t, _, _ in found], [1, 3, 5])
        self.assertTrue(all(h >= 0.35 for _, _, h in found))

    def test_a_hat_roll_is_found_but_steady_sixteenths_are_not(self):
        bpm, beats = 140.0, np.arange(0, 16, 60 / 140.0)
        rng = np.random.default_rng(2)
        d = np.zeros(SR * 16, dtype=np.float32)
        burst = lambda: (rng.normal(0, 1, int(0.012 * SR)) * np.hanning(int(0.012 * SR))).astype(np.float32)
        hp = lambda x: np.diff(x, prepend=x[:1]).astype(np.float32)   # bright, like a hi-hat
        period = 60 / bpm
        for t in np.arange(0.0, 6.0, period / 4):                     # steady sixteenths
            i = int(t * SR); x = hp(burst()); d[i:i + len(x)] += x
        for t in np.arange(8.0, 8.0 + 2 * period, period / 8):        # a two-beat roll in 32nds
            i = int(t * SR); x = hp(burst()); d[i:i + len(x)] += x
        rolls = flavor.find_hat_rolls(d, SR, beats)
        self.assertEqual(len(rolls), 1)
        self.assertAlmostEqual(rolls[0][0], 8.0, delta=0.1)
        self.assertGreaterEqual(rolls[0][2], 6)

    def test_a_wobble_is_found_at_its_own_speed(self):
        bpm = 140.0
        beats = np.arange(0, 40, 60 / bpm)
        t = np.arange(int(40 * SR)) / SR
        lfo = 0.5 - 0.5 * np.cos(2 * np.pi * (bpm / 60 * 2) * t)        # two wobbles a beat
        b = (np.sin(2 * np.pi * 55 * t) * (0.1 + 0.9 * lfo)).astype(np.float32)
        w = flavor.find_wobbles(b, SR, beats)
        self.assertTrue(w)
        self.assertEqual(w[0][2], 2)
        self.assertGreater(w[0][1] - w[0][0], 20)

    def test_plucked_eighth_notes_are_no_wobble(self):
        bpm = 140.0
        beats = np.arange(0, 40, 60 / bpm)
        b = np.zeros(int(40 * SR), dtype=np.float32)
        for t in np.arange(0, 39.5, 60 / bpm / 2):                    # a bass playing eighths, each plucked
            n = tone(55, 0.2, decay=0.06)
            b[int(t * SR):int(t * SR) + len(n)] += n
        self.assertEqual(flavor.find_wobbles(b, SR, beats), [])

    def test_a_steady_bass_is_no_wobble(self):
        beats = np.arange(0, 40, 60 / 120.0)
        b = tone(55, 40)
        self.assertEqual(flavor.find_wobbles(b, SR, beats), [])

    def test_a_key_change_up_near_the_end(self):
        def chord(root_midi, secs):
            f = lambda m: 440.0 * 2 ** ((m - 69) / 12)
            return sum(tone(f(root_midi + k), secs) for k in (0, 4, 7)) / 3
        prog = lambda key: np.concatenate([chord(key + r, 2.0) for r in (0, 5, 7, 0)])   # I IV V I
        song = np.concatenate([prog(60) for _ in range(13)] + [prog(62) for _ in range(5)])  # C major, then D
        kc = flavor.find_key_change(song.astype(np.float32), SR, len(song) / SR)
        self.assertIsNotNone(kc)
        self.assertEqual(kc["up"], 2)
        self.assertAlmostEqual(kc["time"], 13 * 8.0, delta=10)

    def test_no_key_change_in_a_song_that_stays_put(self):
        def chord(root_midi, secs):
            f = lambda m: 440.0 * 2 ** ((m - 69) / 12)
            return sum(tone(f(root_midi + k), secs) for k in (0, 4, 7)) / 3
        song = np.concatenate([np.concatenate([chord(60 + r, 2.0) for r in (0, 5, 7, 0)]) for _ in range(16)])
        self.assertIsNone(flavor.find_key_change(song.astype(np.float32), SR, len(song) / SR))


if __name__ == "__main__":
    unittest.main()


class TestDropTiming(unittest.TestCase):
    """A drop starts where its bass comes in, as it is heard (Great Good Fine Ok's last drop)."""

    def song(self, bass_from):
        beats = np.arange(0, 40, 0.5)                                  # 120 bpm
        t = np.arange(int(40 * SR)) / SR
        drums = (np.sin(2 * np.pi * 60 * t) * (np.mod(t, 0.5) < 0.1)).astype(np.float32)
        bass = (np.sin(2 * np.pi * 55 * t) * (t >= bass_from)).astype(np.float32)
        return beats, {"drums": drums, "bass": bass, "sr": SR}

    def sections(self, drop_at):
        return [{"start": 0.0, "end": drop_at, "type": "verse"}, {"start": drop_at, "end": 40.0, "type": "drop"}]

    def test_a_drop_moves_to_the_beat_its_bass_comes_in(self):
        from lightshow.studio.analysis import refine_drops
        beats, stems = self.song(bass_from=16.5)                       # the bass lands one beat after the bar line
        secs, drops, moves = refine_drops(self.sections(16.0), [16.0], beats, stems)
        self.assertEqual(secs[1]["start"], 16.5)
        self.assertEqual(secs[0]["end"], 16.5)
        self.assertEqual(drops, [16.5])
        self.assertEqual(moves[0][2], 1)

    def test_a_drop_whose_bass_is_already_playing_stays(self):
        from lightshow.studio.analysis import refine_drops
        beats, stems = self.song(bass_from=8.0)
        secs, drops, moves = refine_drops(self.sections(16.0), [16.0], beats, stems)
        self.assertEqual((secs[1]["start"], drops, moves), (16.0, [16.0], []))


class TestSettleSections(unittest.TestCase):
    """Sections corrected from the stems and the hook (Higher Power, Baggy Jeans)."""

    def _song(self, n_s=210, fps=30, bpm=89.0):
        beat = 60.0 / bpm
        return {"feature_fps": fps, "bpm": bpm, "downbeat_phase": 0, "beats": [round(i * beat, 4) for i in range(int(n_s / beat))]}

    def test_drops_follow_the_hook(self):
        from lightshow.studio.analysis import hook_drops
        a = self._song(); fps = 30; bar = 4 * 60.0 / 89.0
        mel = np.full(210 * fps, 0.35)
        for t0, t1 in ((103.0, 124.5), (151.5, 198.0)):          # the choruses: the melody up
            mel[int(t0 * fps):int(t1 * fps)] = 0.8
        a["curves"] = {"other": list(mel), "lead": list(mel)}
        a["hook"] = {"length": bar, "times": [105.6, 116.4, 151.5, 162.3, 173.1, 183.9]}
        snares = [[t, 1.0, 0] for t in np.arange(14.0, 200.0, 2 * 60 / 89.0) if not 58 <= t < 74]   # no backbeat 58-74
        a["events"] = {"snare": snares}
        a["sections"] = [{"start": 0.0, "end": 16.7, "type": "intro"}, {"start": 16.7, "end": 51.7, "type": "verse"},
                         {"start": 51.7, "end": 73.3, "type": "drop"}, {"start": 73.3, "end": 100.3, "type": "verse"},
                         {"start": 100.3, "end": 200.0, "type": "drop"}, {"start": 200.0, "end": 208.0, "type": "outro"}]
        a["drops"] = [51.7, 100.3]
        self.assertTrue(hook_drops(a))
        got = [(round(s["start"]), s["type"]) for s in a["sections"]]
        self.assertEqual([t for _, t in got], ["intro", "verse", "drop", "verse", "drop", "outro"])
        self.assertAlmostEqual(a["sections"][2]["start"], 100.3, delta=0.1)        # its own start stands (the bar lines decide)
        self.assertAlmostEqual(a["sections"][3]["start"], 124.5, delta=1.5)        # the bridge, no longer inside a drop
        self.assertAlmostEqual(a["sections"][4]["start"], 151.5, delta=1.5)
        self.assertEqual(len(a["drops"]), 2)
        self.assertEqual(hook_drops(a), [])                                         # and again: nothing more to change

    def test_a_drop_lands_where_the_bass_and_full_drums_come_back(self):
        """Forester's All I Need: a snare roll without bass, then bass and full drums on the bar: the drop."""
        from lightshow.studio.analysis import drop_landings
        fps, bpm = 30, 120.0
        a = self._song(120, fps, bpm); bar = 2.0
        a["downbeats"] = [i * bar for i in range(60)]
        n = 120 * fps
        bass, dp, en = np.full(n, 0.3), np.full(n, 0.5), np.full(n, 0.55)
        for t0, t1 in ((24, 32), (64, 72)):                        # the rolls: no bass
            bass[t0 * fps:t1 * fps] = 0.0
        for t0, t1 in ((32, 48), (72, 96)):                        # the drops: bass, full drums, louder
            bass[t0 * fps:t1 * fps], dp[t0 * fps:t1 * fps], en[t0 * fps:t1 * fps] = 0.4, 1.0, 0.7
        dp[48 * fps:64 * fps] = 0.2                                 # the verse between: the drums fall away
        a["curves"] = {"bass": list(bass), "drums_present": list(dp)}
        a["energy"] = list(en)
        a["events"] = {"snare": [[t, 1.0, 0] for t in np.arange(24, 32, 0.25)] + [[t, 1.0, 0] for t in np.arange(64, 72, 0.25)]}
        a["sections"] = [{"start": 0.0, "end": 64.0, "type": "verse"}, {"start": 64.0, "end": 110.0, "type": "drop"},
                         {"start": 110.0, "end": 120.0, "type": "outro"}]
        a["drops"] = [64.0]
        self.assertTrue(drop_landings(a))
        got = [(s["type"], round(s["start"])) for s in a["sections"]]
        self.assertEqual(got, [("verse", 0), ("buildup", 24), ("drop", 32), ("verse", 48), ("buildup", 64), ("drop", 72), ("outro", 110)])
        self.assertEqual(a["drops"], [32.0, 72.0])

    def test_a_buildup_where_nothing_builds_is_not_one(self):
        """New Scream: a flat stretch called a buildup is more verse; a real one (snares speeding up) stays."""
        from lightshow.studio.analysis import flat_buildups
        fps, bpm = 30, 120.0
        a = self._song(120, fps, bpm); a["bpm"] = bpm
        en = np.full(120 * fps, 0.5); en[48 * fps:64 * fps] = 0.8; en[96 * fps:] = 0.8
        a["energy"] = list(en)
        snares = [[t, 1.0, 0] for t in np.arange(1.0, 120, 1.0)]                         # a backbeat all through
        snares += [[t, 1.0, 0] for t in np.arange(88.25, 96, 0.25)]                     # and a roll into the second drop
        a["events"] = {"snare": snares}
        a["sections"] = [{"start": 0.0, "end": 40.0, "type": "verse"}, {"start": 40.0, "end": 48.0, "type": "buildup"},
                         {"start": 48.0, "end": 64.0, "type": "drop"}, {"start": 64.0, "end": 80.0, "type": "verse"},
                         {"start": 80.0, "end": 96.0, "type": "buildup"}, {"start": 96.0, "end": 120.0, "type": "drop"}]
        a["drops"] = [48.0, 96.0]
        a["energy"][40 * fps:48 * fps] = [0.8] * (8 * fps)                             # the first is already the drop's loudness
        got = flat_buildups(a)
        self.assertEqual([c[1] for c in got], ["flat buildup: the drop starts here"])
        self.assertEqual([(s["type"], round(s["start"])) for s in a["sections"]],
                         [("verse", 0), ("drop", 40), ("verse", 64), ("buildup", 80), ("drop", 96)])
        self.assertEqual(a["drops"], [40.0, 96.0])
        a["sections"][1:2] = [{"start": 40.0, "end": 48.0, "type": "buildup"}, {"start": 48.0, "end": 64.0, "type": "drop"}]
        a["energy"][40 * fps:48 * fps] = [0.5] * (8 * fps)                             # flat at the verse's loudness: more verse
        a["energy"][48 * fps:64 * fps] = [0.55] * (16 * fps)                           # and the drop lands no louder
        flat_buildups(a)
        self.assertEqual([(s["type"], round(s["start"])) for s in a["sections"]][:2], [("verse", 0), ("drop", 48)])
        self.assertEqual(a["sections"][0]["end"], 48.0)

    def _levels(self, a, parts, fps=30):
        """parts: (t0, t1, energy, bass, drums) stretches over the song's curves"""
        n = int(max(p[1] for p in parts) * fps)
        en, bass, dr = np.full(n, 0.3), np.zeros(n), np.full(n, 0.2)
        for t0, t1, e, b, d in parts:
            en[int(t0 * fps):int(t1 * fps)], bass[int(t0 * fps):int(t1 * fps)], dr[int(t0 * fps):int(t1 * fps)] = e, b, d
        a["energy"], a["curves"] = list(en), {"bass": list(bass), "drums_present": list(dr)}

    def test_a_song_with_no_drop_gets_its_loudest_parts(self):
        """WET DREAM: loud stretches with bass between quiet ones, and no drop found: they are its drops."""
        from lightshow.studio.analysis import drops_where_none
        a = self._song(100, 30, 120.0); a["genre"] = {"style": "dance"}
        self._levels(a, [(0, 10, 0.2, 0.0, 0.1), (10, 30, 0.8, 0.5, 0.9), (30, 50, 0.45, 0.0, 0.6), (50, 70, 0.78, 0.5, 0.9),
                         (70, 90, 0.5, 0.05, 0.6), (90, 100, 0.2, 0.0, 0.1)])
        a["sections"] = [{"start": 0.0, "end": 10.0, "type": "intro"}] + [{"start": t, "end": t + 20.0, "type": "verse"} for t in (10.0, 30.0, 50.0, 70.0)] + [{"start": 90.0, "end": 100.0, "type": "outro"}]
        self.assertEqual(len(drops_where_none(a)), 2)
        self.assertEqual([x["type"] for x in a["sections"]], ["intro", "drop", "verse", "drop", "verse", "outro"])
        self.assertEqual(a["drops"], [10.0, 50.0])
        a["genre"] = {"style": "chill"}; a["sections"][1]["type"] = a["sections"][3]["type"] = "verse"
        self.assertEqual(drops_where_none(a), [])                                      # a calm song may have none

    def test_a_chorus_that_comes_back_is_a_drop(self):
        """A verse that sounds like the drop and is as big is that drop again; a quiet one that sounds alike is not."""
        from lightshow.studio.analysis import drops_by_sound
        a = self._song(120, 30, 120.0)
        self._levels(a, [(0, 20, 0.5, 0.2, 0.7), (20, 40, 0.85, 0.6, 0.9), (40, 60, 0.5, 0.2, 0.7), (60, 80, 0.84, 0.6, 0.9),
                         (80, 100, 0.4, 0.0, 0.1), (100, 120, 0.5, 0.2, 0.7)])
        rng = np.random.default_rng(1); fr = 43.0; n = int(120 * fr)
        bands = rng.standard_normal((32, n)).astype(np.float32)
        chorus = rng.standard_normal((32, int(20 * fr))).astype(np.float32)
        for t0 in (20, 60, 80):                                                         # the same chorus three times (the last stripped down)
            bands[:, int(t0 * fr):int(t0 * fr) + chorus.shape[1]] = chorus
        a["align_bands"], a["align_fps"] = bands.tolist(), fr
        a["sections"] = [{"start": 0.0, "end": 20.0, "type": "verse"}, {"start": 20.0, "end": 40.0, "type": "drop"}, {"start": 40.0, "end": 60.0, "type": "verse"},
                         {"start": 60.0, "end": 80.0, "type": "verse"}, {"start": 80.0, "end": 100.0, "type": "breakdown"}, {"start": 100.0, "end": 120.0, "type": "verse"}]
        a["drops"] = [20.0]
        self.assertEqual(len(drops_by_sound(a)), 1)
        self.assertEqual([x["type"] for x in a["sections"]], ["verse", "drop", "verse", "drop", "breakdown", "verse"])
        self.assertEqual(a["drops"], [20.0, 60.0])

    def test_a_drop_runs_over_a_verse_as_big_as_it(self):
        from lightshow.studio.analysis import drop_neighbors
        a = self._song(80, 30, 120.0)
        self._levels(a, [(0, 20, 0.5, 0.2, 0.6), (20, 30, 0.9, 0.6, 0.95), (30, 50, 0.9, 0.62, 0.95), (50, 80, 0.5, 0.2, 0.6)])
        a["sections"] = [{"start": 0.0, "end": 20.0, "type": "verse"}, {"start": 20.0, "end": 30.0, "type": "verse"},
                         {"start": 30.0, "end": 50.0, "type": "drop"}, {"start": 50.0, "end": 80.0, "type": "verse"}]
        a["drops"] = [30.0]
        self.assertEqual(len(drop_neighbors(a)), 1)
        self.assertEqual([(x["type"], x["start"], x["end"]) for x in a["sections"]], [("verse", 0.0, 20.0), ("drop", 20.0, 50.0), ("verse", 50.0, 80.0)])
        self.assertEqual(a["drops"], [20.0])

    def test_a_drop_out_of_a_pause_starts_at_the_hit(self):
        from lightshow.studio.analysis import drop_after_pause
        a = self._song(80); fps = 30
        e = np.full(80 * fps, 0.55)
        e[int(52.8 * fps):int(53.47 * fps)] = 0.2                                   # the pause
        e[int(53.47 * fps):] = 0.85                                                 # the hit, and the drop goes on
        a["energy"] = list(e)
        a["sections"] = [{"start": 10.0, "end": 54.37, "type": "verse"}, {"start": 54.37, "end": 80.0, "type": "drop"}]
        a["drops"] = [54.37]
        self.assertTrue(drop_after_pause(a))
        self.assertLess(a["sections"][1]["start"], 54.0)
        self.assertAlmostEqual(a["sections"][1]["start"], 53.47, delta=0.3)
        self.assertEqual(a["sections"][0]["end"], a["sections"][1]["start"])

    def test_a_breakdown_with_drums_is_a_verse(self):
        from lightshow.studio.analysis import drummed_breakdowns
        a = self._song(60); fps = 30
        a["curves"] = {"drums_present": [0.8] * (60 * fps)}
        a["events"] = {"snare": [[t, 1.0, 0] for t in np.arange(0.0, 60.0, 60 / 89.0)]}
        a["sections"] = [{"start": 0.0, "end": 11.5, "type": "intro"}, {"start": 11.5, "end": 43.6, "type": "breakdown"}]
        self.assertTrue(drummed_breakdowns(a))
        self.assertEqual(a["sections"][1]["type"], "verse")
        quiet = {**self._song(60), "curves": {"drums_present": [0.05] * (60 * fps)}, "events": {"snare": []},
                 "sections": [{"start": 11.5, "end": 43.6, "type": "breakdown"}]}
        self.assertEqual(drummed_breakdowns(quiet), [])


class TestStemAttacks(unittest.TestCase):
    def test_kicks_and_snares_from_the_drum_tracks_attacks(self):
        import librosa
        from lightshow.studio.events import stem_attack_hits
        sr, hop = 22050, 512
        y = np.zeros(sr * 6, dtype=np.float32)
        t = np.arange(int(0.08 * sr)) / sr
        kick = (np.sin(2 * np.pi * 60 * t) * np.exp(-t * 40)).astype(np.float32)
        snare = (np.random.default_rng(0).standard_normal(len(t)) * np.exp(-t * 60)).astype(np.float32)
        snare = librosa.effects.preemphasis(snare, coef=0.97).astype(np.float32)
        kicks_at, snares_at = [0.5, 1.7, 2.1, 3.9], [1.1, 2.9, 4.6]               # no steady pattern at all
        for at in kicks_at:
            y[int(at * sr):int(at * sr) + len(t)] += kick
        for at in snares_at:
            y[int(at * sr):int(at * sr) + len(t)] += 0.5 * snare
        S = np.abs(librosa.stft(y, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr)
        k, sn = stem_attack_hits(S, freqs, sr, hop)
        for at in kicks_at:
            self.assertTrue(any(abs(h[0] - at) < 0.05 for h in k), f"kick at {at}")
        for at in snares_at:
            self.assertTrue(any(abs(h[0] - at) < 0.05 for h in sn), f"snare at {at}")


class TestGapBeforeSection(unittest.TestCase):
    def test_a_verse_starts_where_its_music_comes_back(self):
        from lightshow.studio.analysis import gap_before_section
        fps, beat = 30, 60 / 107.1
        e = np.full(80 * fps, 0.6)
        e[int(23.0 * fps):int(25.0 * fps)] = 0.15                                    # the intro rings out
        a = {"feature_fps": fps, "energy": list(e), "downbeat_phase": 0, "beats": [round(i * beat, 4) for i in range(140)],
             "sections": [{"start": 0.0, "end": 22.97, "type": "intro"}, {"start": 22.97, "end": 60.0, "type": "verse"}]}
        self.assertTrue(gap_before_section(a))
        self.assertAlmostEqual(a["sections"][1]["start"], 25.2, delta=0.4)
        self.assertEqual(a["sections"][0]["end"], a["sections"][1]["start"])
        self.assertEqual(gap_before_section(a), [])                                  # and stays

    def test_a_long_break_stays_its_own(self):
        from lightshow.studio.analysis import gap_before_section
        fps, beat = 30, 0.5
        e = np.full(80 * fps, 0.6)
        e[int(20.0 * fps):int(25.0 * fps)] = 0.1                                     # five quiet seconds
        a = {"feature_fps": fps, "energy": list(e), "downbeat_phase": 0, "beats": [i * beat for i in range(160)],
             "sections": [{"start": 0.0, "end": 20.0, "type": "intro"}, {"start": 20.0, "end": 60.0, "type": "verse"}]}
        self.assertEqual(gap_before_section(a), [])


class TestDrumlessDrops(unittest.TestCase):
    """A "drop" with the drums silent, in a song that has drums, is a lead-in, not a drop."""

    def test_a_drop_with_no_drums_becomes_a_verse(self):
        from lightshow.studio.analysis import drumless_drops
        fps = 30
        dp = [0.6] * (40 * fps) + [0.05] * (26 * fps) + [0.7] * (26 * fps)   # drums, then none, then drums
        a = {"feature_fps": fps, "curves": {"drums_present": dp}, "drops": [40.0, 66.0],
             "sections": [{"start": 0.0, "end": 40.0, "type": "verse"}, {"start": 40.0, "end": 66.0, "type": "drop"},
                          {"start": 66.0, "end": 92.0, "type": "drop"}]}
        self.assertEqual(drumless_drops(a), [(40.0, "verse")])
        self.assertEqual([s["type"] for s in a["sections"]], ["verse", "verse", "drop"])
        self.assertEqual(a["drops"], [66.0])

    def test_a_drop_lands_where_its_drums_come_back_after_the_build(self):
        from lightshow.studio.analysis import drumless_drops
        fps = 30
        # drums, a drumless 12 s, drums at half (the build), a 2 s stop, then the drums for good
        dp = [0.7] * (20 * fps) + [0.02] * (12 * fps) + [0.5] * (14 * fps) + [0.1] * (2 * fps) + [0.8] * (30 * fps)
        a = {"feature_fps": fps, "curves": {"drums_present": dp}, "drops": [20.0], "beats": [i * 0.5 for i in range(200)],
             "sections": [{"start": 0.0, "end": 20.0, "type": "verse"}, {"start": 20.0, "end": 78.0, "type": "drop"}]}
        drumless_drops(a)
        self.assertEqual([s["type"] for s in a["sections"]], ["verse", "verse", "drop"])
        self.assertAlmostEqual(a["sections"][2]["start"], 48.0, delta=0.6)       # after the stop, not where the build began
        self.assertEqual(len(a["drops"]), 1)
        self.assertAlmostEqual(a["drops"][0], 48.0, delta=0.6)

    def test_a_song_without_drums_keeps_its_drops(self):
        from lightshow.studio.analysis import drumless_drops
        a = {"feature_fps": 30, "curves": {"drums_present": [0.05] * 3000}, "drops": [40.0],
             "sections": [{"start": 40.0, "end": 66.0, "type": "drop"}]}
        self.assertEqual(drumless_drops(a), [])
        self.assertEqual(a["sections"][0]["type"], "drop")
