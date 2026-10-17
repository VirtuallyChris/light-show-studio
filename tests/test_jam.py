"""Jam mode, tested without a sound: run with  .venv\\Scripts\\python -m unittest tests.test_jam -v

A fake clock, a fake Spotify, synthetic songs fed in place of the sound card and
an imaginary perfect speaker. Nothing is played, no lights are driven and the
network is never touched.
"""

import json
import os
import shutil
import struct
import tempfile
import unittest
import urllib.parse
import warnings

import numpy as np

from lightshow.studio.jam import JamEngine, JamLibrary, JamSettings, VirtualOutput, align_start, live_bands
from lightshow.studio.loopback import FileCapture, Gate
from lightshow.studio.relay import Relay, wav_stream_header
from lightshow.studio.spotify import NowPlaying, SpotifyClient, Track
from tests.synth import make_song

warnings.filterwarnings("ignore")
SR = 22050          # a low capture rate keeps the tests quick; the code is rate-agnostic


def _touch(path, data=b""):
    with open(path, "wb") as fh:
        fh.write(data)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeSpotify:
    """Answers like the real client, from a script the test controls."""

    signed_in = True

    def __init__(self, clock, lag=0.0):
        self.clock, self.lag = clock, lag          # lag: how stale Spotify's answer is, as real ones are
        self.track, self.started, self.playing, self.held = None, 0.0, False, 0.0
        self.upcoming = []

    def play(self, track, at=0.0):
        self.track, self.started, self.playing, self.held = track, self.clock() - at, True, at

    def pause(self):
        if self.playing:
            self.held, self.playing = self.clock() - self.started, False
        return True

    def resume(self):
        if not self.playing:
            self.started, self.playing = self.clock() - self.held, True
        return True

    def now_playing(self):
        if self.track is None:
            return NowPlaying(None, fetched_at=self.clock())
        prog = (self.clock() - self.started) if self.playing else self.held
        return NowPlaying(self.track, max(0.0, prog - self.lag), self.playing, self.clock(), "This PC")

    def queue(self):
        return list(self.upcoming)


class Sim:
    """Steps capture, clock, engine and speaker together, half a second at a time."""

    def __init__(self, shows_dir, target=20.0, lag=0.0, **settings):
        self.clock = Clock()
        self.relay = Relay(sr=SR, channels=2, capacity_s=400, clock=self.clock)
        self.gate = Gate()
        self.spotify = FakeSpotify(self.clock, lag)
        self.out = VirtualOutput(self.clock)
        self.library = JamLibrary(shows_dir)
        cfg = JamSettings(target_delay=target, analyse_every=14.0, events_every=99, capture_latency=0.0, **settings)
        self.engine = JamEngine(self.relay, self.spotify, self.library, self.out, self.gate, cfg, self.clock, cache_dir=False)
        self.capture = FileCapture(self.relay, np.zeros((1, 2), dtype=np.float32), self.gate)
        self.did, self.ticks = [], 0

    def play(self, song, track, at=0.0):
        self.capture.load(song["audio"][int(at * SR):])
        self.spotify.play(track, at)

    def run(self, seconds, analyse=True, until=None):
        for _ in range(int(round(seconds / 0.5))):
            self.capture.step(0.5, silent=not self.spotify.playing)
            self.clock.t += 0.5
            self.ticks += 1
            if self.ticks % 2 == 0:
                self.engine.poll()
                if not self.engine.streaming and self.engine.auto_start and self.engine.buffered() >= self.engine.cfg.target_delay:
                    self.engine.start_streaming()
            self.out.pump()
            if analyse and self.ticks % 2 == 0:
                did = self.engine.analyse()
                if did:
                    self.did.append((self.clock.t, did))
            if until and until():
                return


def song(bpm, seed):
    s = make_song(bpm=bpm, sr=SR, seed=seed)
    return s, Track(id=f"trk{seed:04d}", name=f"Test Tune {seed}", artists=["The Synthetics"], duration=s["duration"])


# ============================================================================== relay
class TestRelay(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.relay = Relay(sr=1000, channels=2, capacity_s=10, clock=self.clock)

    def ramp(self, n, start=0):
        v = (np.arange(start, start + n) % 30000).astype(np.int16)
        return np.stack([v, v], axis=1)

    def test_stream_is_the_capture_delayed(self):
        self.relay.write(self.ramp(5000))
        s = self.relay.open_stream(2.0, pace=False)
        got = np.frombuffer(s.read(1000), dtype=np.int16).reshape(-1, 2)
        self.assertEqual(got[0, 0], 2000)
        self.assertAlmostEqual(s.capture_time_at(0.5), 2.5, places=3)

    def test_time_before_the_recording_can_be_asked_for_as_silence(self):
        self.relay.write(self.ramp(3000, start=1))
        self.assertEqual(len(self.relay.frames(-1.0, 1.0)), 1000)               # clipped, as before
        x = self.relay.frames(-1.0, 1.0, pad=True)
        self.assertEqual(len(x), 2000)
        self.assertEqual((int(x[:1000].max()), int(x[1000, 0])), (0, 1))        # silence, then the first frame

    def test_never_reads_past_the_capture_head(self):
        self.relay.write(self.ramp(1000))
        s = self.relay.open_stream(0.0, pace=False)
        self.assertEqual(len(s.read(5000, timeout=0)) // 4, 1000)
        tail = s.read(5000, timeout=0)                 # caught up: silence, not a stall
        self.assertTrue(len(tail) > 0 and not any(tail))

    def test_gap_pushes_the_room_further_behind(self):
        self.relay.write(self.ramp(6000))
        s = self.relay.open_stream(0.0, pace=False)
        s.add_gap(2.0, 1.5)
        while s.cursor < 4000:
            s.read(700, timeout=0)
        self.assertAlmostEqual(s.capture_time_at(1.0), 1.0, places=2)
        self.assertIsNone(s.capture_time_at(2.5))       # inside the inserted silence
        self.assertAlmostEqual(s.capture_time_at(4.5), 3.0, places=2)   # 1.5 s later than it would have been

    def test_wraps_around_without_losing_order(self):
        for k in range(25):
            self.relay.write(self.ramp(1000, k * 1000))
        self.assertAlmostEqual(self.relay.oldest_time, 15.0)
        x = self.relay.frames(20.0, 20.01)
        self.assertEqual(int(x[0, 0]), 20000)

    def test_trims_trailing_silence_but_not_music(self):
        self.relay.write(self.ramp(3000, 100))
        self.relay.write(np.zeros((1500, 2), dtype=np.int16))
        cut = self.relay.trim_tail_silence(max_seconds=3.0)
        self.assertGreater(cut, 1.4)
        self.assertLess(cut, 1.6)
        self.assertGreater(int(np.abs(self.relay.frames(2.9, 3.0)).max()), 0)

    def test_header_is_an_endless_pcm_wav(self):
        h = wav_stream_header(44100, 2)
        self.assertEqual(h[:4] + h[8:16], b"RIFFWAVEfmt ")
        fmt, ch, sr = struct.unpack("<HHI", h[20:28])
        self.assertEqual((fmt, ch, sr, len(h)), (1, 2, 44100, 44))


# ============================================================================ spotify
class TestSpotify(unittest.TestCase):
    def client(self, responses):
        self.calls = []

        def transport(method, url, headers, body):
            self.calls.append((method, url, headers, body))
            return responses.pop(0)

        self.store = {"client_id": "abc123"}
        return SpotifyClient(self.store, transport=transport, clock=Clock())

    def test_sign_in_uses_pkce_and_no_secret(self):
        c = self.client([(200, json.dumps({"access_token": "A", "refresh_token": "R", "expires_in": 3600}).encode())])
        q = urllib.parse.parse_qs(urllib.parse.urlparse(c.auth_url("http://127.0.0.1:8765/api/spotify/callback")).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertNotIn("client_secret", q)
        c.finish_auth("thecode", q["state"][0], "http://127.0.0.1:8765/api/spotify/callback")
        form = urllib.parse.parse_qs(self.calls[0][3].decode())
        self.assertIn("code_verifier", form)
        self.assertNotIn("client_secret", form)
        self.assertEqual(self.store["refresh_token"], "R")

    def test_rejects_a_callback_it_did_not_start(self):
        c = self.client([])
        c.auth_url("http://127.0.0.1:8765/cb")
        with self.assertRaises(PermissionError):
            c.finish_auth("code", "not-the-state", "http://127.0.0.1:8765/cb")

    def test_reads_now_playing_and_refreshes_a_dead_token(self):
        item = {"id": "t1", "type": "track", "name": "Song", "artists": [{"name": "Band"}], "duration_ms": 200000}
        c = self.client([(200, b'{"access_token":"A1","expires_in":3600}'), (401, b"{}"),
                         (200, b'{"access_token":"A2","expires_in":3600}'),
                         (200, json.dumps({"item": item, "progress_ms": 61500, "is_playing": True, "device": {"name": "PC"}}).encode())])
        self.store["refresh_token"] = "R"
        n = c.now_playing()
        self.assertEqual((n.track.id, n.track.label, n.progress, n.playing, n.device), ("t1", "Band - Song", 61.5, True, "PC"))
        self.assertEqual(self.calls[-1][2]["Authorization"], "Bearer A2")

    def test_nothing_playing_is_not_an_error(self):
        c = self.client([(200, b'{"access_token":"A","expires_in":3600}'), (204, b"")])
        self.store["refresh_token"] = "R"
        self.assertIsNone(c.now_playing().track)


# ============================================================================ library
class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("Porter Robinson - Cheerleader (Lyrics)", "Great Good Fine Ok - Holding You", "If Youre Too Shy (Let Me Know)"):
            os.makedirs(os.path.join(self.dir, name))
            _touch(os.path.join(self.dir, name, "project.json"), b"{}")
            _touch(os.path.join(self.dir, name, "song.mp3"))
        self.lib = JamLibrary(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_matches_saved_shows_by_title_and_artist(self):
        m = self.lib.match_project
        self.assertEqual(m(Track("1", "Cheerleader", ["Porter Robinson"], 200)), "Porter Robinson - Cheerleader (Lyrics)")
        self.assertEqual(m(Track("2", "Holding You", ["Great Good Fine Ok"], 200)), "Great Good Fine Ok - Holding You")
        self.assertIsNone(m(Track("3", "Cheerleader", ["OMI"], 180)))          # same title, different song
        self.assertIsNone(m(Track("4", "Something Else", ["Porter Robinson"], 200)))

    def test_a_manual_link_wins_and_can_say_no(self):
        t = Track("9", "If You're Too Shy (Let Me Know)", ["The 1975"], 300)
        self.assertIsNone(self.lib.match_project(t))                           # artist is not in the folder name
        self.lib.link("9", "If Youre Too Shy (Let Me Know)")
        self.assertEqual(JamLibrary(self.dir).match_project(t), "If Youre Too Shy (Let Me Know)")
        self.lib.link("9", "")
        self.assertIsNone(self.lib.match_project(t))

    def test_learning_keeps_numbers_not_audio(self):
        t = Track("77", "Tune", ["Band"], 60)
        self.lib.save_learned(t, {"bpm": 128.0, "beats": [0.0, 0.47], "align_bands": [[0.1, 0.2]], "sections": []})
        doc = self.lib.learned("77")
        self.assertEqual(doc["plays"], 1)
        self.assertEqual(doc["analysis"]["bpm"], 128.0)
        self.assertFalse(any(k in doc["analysis"] for k in ("audio", "samples", "pcm", "y")))
        self.assertEqual([f for f in os.listdir(self.lib.learned_dir) if not f.endswith(".json")], [])


# ============================================================================= by ear
class TestAlignment(unittest.TestCase):
    def test_finds_a_known_track_by_ear(self):
        s, _ = song(124.0, 3)
        mono = s["audio"].mean(axis=1)
        ref = live_bands(mono[: int(45 * SR)])
        fps = SR / 512
        true_start = 1.37                                                      # the track begins here in the capture
        live = np.concatenate([np.zeros(int(true_start * SR), dtype=np.float32), mono[: int(24 * SR)]])
        start, peak, edge = align_start(live_bands(live), ref, 0.0, fps)
        self.assertAlmostEqual(start, true_start, delta=0.03)
        self.assertGreater(peak, 0.9)
        self.assertGreater(edge, 0.01)


# ============================================================================= engine
class TestEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a, cls.ta = song(128.0, 1)
        cls.b, cls.tb = song(120.0, 2)

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_first_listen_sees_the_drop_coming_then_learns_the_song(self):
        sim = Sim(self.dir, target=20.0)
        e = sim.engine
        sim.play(self.a, self.ta)
        first_drop = self.a["drops"][0]
        seen_at = {}

        def watch():
            t = e.current
            if t and t.analysis and "lookahead" not in seen_at:
                if any(abs(d - first_drop) < 1.0 for d in t.analysis["drops"]):
                    rc = e.room_capture_time()
                    seen_at["lookahead"] = first_drop - ((rc - t.start) if rc is not None else -e.buffered())
            return False

        sim.run(self.a["duration"] - 1, until=watch)
        self.assertTrue(e.streaming)
        self.assertAlmostEqual(e.delay(), 20.0, delta=1.0)                       # the room sits the target behind
        st = e.state()
        self.assertEqual(st["room"]["kind"], "live")
        self.assertAlmostEqual(st["room"]["position"], st["ahead"] and (sim.relay.capture_time - e.delay() - e.current.start), delta=0.2)
        self.assertIn("lookahead", seen_at, f"the first drop was never found; analysis log: {sim.did}")
        self.assertGreater(seen_at["lookahead"], 4.0)                            # known seconds before the room got there
        self.assertAlmostEqual(e.current.analysis["bpm"], 128.0, delta=1.0)

        # what the studio is sent for a first listen, part way through
        doc = e.analysis_json(e.current.key)["analysis"]
        self.assertTrue(doc["provisional"])
        self.assertEqual(len(doc["energy"]), int(self.a["duration"] * 30))
        self.assertAlmostEqual(doc["sections"][-1]["end"], self.a["duration"], places=3)
        self.assertGreater(doc["beats"][-1], self.a["duration"] - 1.0)           # the grid runs on past what was analysed
        for s0, s1 in zip(doc["sections"], doc["sections"][1:]):
            self.assertAlmostEqual(s0["end"], s1["start"], places=2)

        sim.play(self.b, self.tb)                                                # the next song follows naturally
        sim.run(30)
        self.assertEqual(sim.out.starts, 1)                                      # no flush for a natural change
        learned = sim.library.learned(self.ta.id)
        self.assertIsNotNone(learned, f"nothing was learned; analysis log: {sim.did}")
        self.assertIn("align_bands", learned["analysis"])
        self.assertLess(os.path.getsize(sim.library._learned_path(self.ta.id)), 1_500_000)
        self.assertEqual([f for f in os.listdir(sim.library.learned_dir) if not f.endswith(".json")], [])

    def test_a_copy_that_is_not_what_spotify_plays_is_dropped_and_built_live(self):
        from lightshow.studio.analysis import analyze_audio

        # learned ahead from a "copy" that is really another recording (here: the other song)
        other = analyze_audio(self.b["audio"].mean(axis=1), SR, events=False)
        JamLibrary(self.dir).save_learned(self.ta, other, source="youtube")
        sim = Sim(self.dir, target=20.0)
        told = []
        sim.engine.on_mismatch = told.append
        sim.play(self.a, self.ta)
        sim.run(48)                                                               # searched further first (from 30 s in)
        t = sim.engine.current
        self.assertEqual(t.kind, "live")                                          # built as it plays, from Spotify's audio
        self.assertEqual([x.id for x in told], [self.ta.id])                      # the copy is not fetched again
        self.assertGreater(t.passes, 0)
        self.assertNotEqual(sim.library.learned(self.ta.id) and sim.library.learned(self.ta.id).get("source"), "youtube")

    def test_a_song_learned_ahead_is_lined_up_before_a_room_ten_seconds_behind_hears_it(self):
        from lightshow.studio.analysis import analyze_audio

        JamLibrary(self.dir).save_learned(self.ta, analyze_audio(self.a["audio"].mean(axis=1), SR, events=False), source="youtube")
        sim = Sim(self.dir, target=10.0, lag=0.35)                                # Spotify's answers run 350 ms stale
        sim.run(3.0, analyse=False)
        true_start = sim.relay.capture_time
        sim.play(self.a, self.ta)
        sim.run(9.0)                                                              # 9 s in: the room has not heard it yet
        t = sim.engine.current
        self.assertTrue(t.aligned, f"not lined up 9 s in: match {t.align_peak}, edge {t.align_edge}")
        self.assertAlmostEqual(t.start, true_start, delta=0.04)
        sim.run(60.0)                                                             # checked again as it plays: still the same
        self.assertEqual(t.kind, "learned")
        self.assertAlmostEqual(t.start, true_start, delta=0.06)

    def test_a_song_that_stops_matching_its_copy_part_way_is_built_live_from_there(self):
        from lightshow.studio.analysis import analyze_audio

        from lightshow.studio.analysis import HOP
        from lightshow.studio.matching import match_features
        mono = self.a["audio"].mean(axis=1)
        copy = analyze_audio(mono, SR, events=False)
        copy["align_bands"] = match_features(mono, SR, HOP).tolist()               # learned from a copy: the whole song
        JamLibrary(self.dir).save_learned(self.ta, copy, source="youtube")
        sim = Sim(self.dir, target=10.0)
        told = []
        sim.engine.on_mismatch = told.append
        sim.play(self.a, self.ta)
        sim.run(30.0)
        t = sim.engine.current
        self.assertEqual(t.kind, "learned")
        sim.capture.load(self.b["audio"])                                         # Spotify's edit goes another way from here
        sim.run(45.0)
        self.assertEqual(t.kind, "live")
        self.assertEqual([x.id for x in told], [self.ta.id])

    def test_a_copy_with_a_longer_intro_is_found_further_out_not_dropped(self):
        from lightshow.studio.analysis import HOP, analyze_audio
        from lightshow.studio.matching import match_features
        lead = np.zeros((int(5.0 * SR), 2), dtype=self.a["audio"].dtype)        # the copy: 5 s of intro first
        mono = np.concatenate([lead, self.a["audio"]]).mean(axis=1)
        mono[: int(5.0 * SR)] = 0.02 * np.sin(np.arange(int(5.0 * SR)) * 0.05)
        copy = analyze_audio(mono, SR, events=False)
        copy["align_bands"] = match_features(mono, SR, HOP).tolist()
        JamLibrary(self.dir).save_learned(self.ta, copy, source="youtube")
        sim = Sim(self.dir, target=3.0)
        told = []
        sim.engine.on_mismatch = told.append
        sim.play(self.a, self.ta)
        true_start = sim.relay.capture_time
        sim.run(40.0)
        t = sim.engine.current
        self.assertEqual(told, [])
        self.assertEqual(t.kind, "learned")
        self.assertTrue(t.aligned)
        self.assertAlmostEqual(t.start, true_start - 5.0, delta=0.08)             # the copy's 0:00 is 5 s before Spotify's

    def test_a_seek_in_a_known_song_keeps_its_show_and_lines_up_again(self):
        from lightshow.studio.analysis import HOP, analyze_audio
        from lightshow.studio.matching import match_features
        mono = self.a["audio"].mean(axis=1)
        copy = analyze_audio(mono, SR, events=False)
        copy["align_bands"] = match_features(mono, SR, HOP).tolist()
        JamLibrary(self.dir).save_learned(self.ta, copy, source="youtube")
        sim = Sim(self.dir, target=3.0)
        told = []
        sim.engine.on_mismatch = told.append
        sim.play(self.a, self.ta)
        sim.run(20.0)
        first = sim.engine.current
        self.assertTrue(first.aligned)
        sim.play(self.a, self.ta, at=60.0)                                        # seek to 1:00 on the phone
        seek_at = sim.relay.capture_time
        sim.run(16.0)
        t = sim.engine.current
        self.assertIsNot(t, first)                                                # a new stretch of the same song...
        self.assertEqual(t.kind, "learned")                                       # ...with its show, not a first listen
        self.assertEqual(told, [])
        self.assertTrue(t.aligned, f"not lined up after the seek: match {t.align_peak}")
        self.assertAlmostEqual(t.start, seek_at - 60.0, delta=0.06)               # 0:00 where it would be: 1:00 at the seek
        self.assertIs(sim.engine.track_at(seek_at - 1.0), first)                  # the room still hears the old place first

    def test_learning_ahead_a_first_listen_only_glows_and_is_not_kept(self):
        calls = []
        sim = Sim(self.dir, target=5.0, light_live=True)
        sim.engine.analyse_fn = lambda *a, **k: calls.append(1) or self.fail("no full analysis for a stopgap listen")
        sim.engine.genre_fn = lambda y, sr: self.fail("no genre model for a stopgap listen")
        sim.engine.segment_fn = lambda *a, **k: self.fail("no section model for a stopgap listen")
        sim.play(self.a, self.ta)
        sim.run(20)
        t = sim.engine.current
        doc = sim.engine.analysis_json(t.key)
        self.assertTrue(doc["light"])
        a = doc["analysis"]
        # nothing is worked out for it, not even the beat (the lights only glow: jam.js buildLight): a plain
        # grid, so all the PC's effort goes to learning the song from its copy
        self.assertIsNone(t.beat)
        self.assertEqual(a["bpm"], 120.0)
        sim.run(self.a["duration"] - 18)
        sim.play(self.b, self.tb)                                                  # the song ends naturally
        sim.run(12)
        self.assertEqual(calls, [])                                                 # the beat only: no analysis passes at all
        self.assertIsNone(sim.library.learned(self.ta.id))                         # learned from its copy instead

    def test_a_learned_song_is_recognised_and_lined_up_by_ear(self):
        from lightshow.studio.analysis import analyze_audio

        mono = self.a["audio"].mean(axis=1)
        JamLibrary(self.dir).save_learned(self.ta, analyze_audio(mono, SR, events=False))
        sim = Sim(self.dir, target=20.0, lag=0.35)                                # Spotify's answers run 350 ms stale
        lead_in = 3.0
        sim.run(lead_in, analyse=False)                                           # silence before the music starts
        true_start = sim.relay.capture_time
        sim.play(self.a, self.ta)
        sim.run(30)
        t = sim.engine.current
        self.assertEqual(t.kind, "learned")
        self.assertTrue(t.aligned)
        self.assertGreater(t.align_peak, 0.9)
        self.assertAlmostEqual(t.start, true_start, delta=0.04)                  # the stale clock alone would be 350 ms out
        self.assertEqual(sim.engine.analysis_json(t.key)["analysis"]["bpm"], sim.library.learned(self.ta.id)["analysis"]["bpm"])

    def test_a_song_already_playing_is_measured_from_its_real_start(self):
        # The jam begins 15 s into a song. Everything learned must still be in the song's own time.
        sim = Sim(self.dir, target=20.0)
        e = sim.engine
        sim.play(self.a, self.ta, at=15.0)
        sim.run(self.a["duration"] - 15.0 + 2.0)
        jt = e.tracks[0]
        self.assertAlmostEqual(jt.start, -15.0, delta=0.6)
        drops = e.current.analysis["drops"] if e.current is jt else jt.analysis["drops"]
        true = [d for d in self.a["drops"] if d > 20.0]
        self.assertTrue(true)
        for d in true:                                                           # each real drop found where it is
            self.assertLess(min(abs(d - x) for x in drops), 1.0, f"drop at {d:.1f}s, found {drops}")
        self.assertIsNone(sim.library.learned(self.ta.id))                        # half a song is not kept
        doc = e.analysis_json(jt.key) if jt.analysis is not None else None
        if doc and doc.get("analysis") and doc["analysis"].get("energy"):
            self.assertEqual(max(doc["analysis"]["energy"][: int(10 * 30)]), 0.0)  # the unheard opening is silence

    def test_a_learned_song_already_playing_is_lined_up_by_ear(self):
        from lightshow.studio.analysis import analyze_audio

        JamLibrary(self.dir).save_learned(self.ta, analyze_audio(self.a["audio"].mean(axis=1), SR, events=False))
        sim = Sim(self.dir, target=20.0, lag=0.35)
        true_start = sim.relay.capture_time - 20.0                               # it has been playing for 20 s
        sim.play(self.a, self.ta, at=20.0)
        sim.run(30)
        t = sim.engine.tracks[-1]
        self.assertEqual(t.kind, "learned")
        self.assertTrue(t.aligned)
        self.assertGreater(t.align_peak, 0.9)
        self.assertAlmostEqual(t.start, true_start, delta=0.04)

    def test_a_skip_keeps_the_delay_by_default(self):
        sim = Sim(self.dir, target=20.0)
        e = sim.engine
        sim.play(self.a, self.ta)
        sim.run(40, analyse=False)
        sim.play(self.b, self.tb)                                                # a skip, 40 s into a 75 s song
        sim.run(6, analyse=False)
        self.assertEqual((sim.out.starts, e.flushes), (1, 0))                    # nothing thrown away
        self.assertTrue(e.tracks[-2].skipped)                                    # still a skip: the song is not learned
        self.assertAlmostEqual(e.delay(), 20.0, delta=1.0)
        self.assertEqual(e.state()["room"]["id"], self.ta.id)                    # the room is still on the old song
        sim.run(16, analyse=False)
        self.assertEqual(e.state()["room"]["id"], self.tb.id)                    # and reaches the skip a delay later
        self.assertAlmostEqual(e.delay(), 20.0, delta=1.0)

    def test_a_skip_can_jump_and_the_delay_grows_back_between_songs(self):
        sim = Sim(self.dir, target=20.0, max_gap=6.0, skip_jumps=True)
        e = sim.engine
        sim.play(self.a, self.ta)
        sim.run(40, analyse=False)
        self.assertAlmostEqual(e.delay(), 20.0, delta=1.0)
        sim.play(self.b, self.tb)                                                # a skip, 40 s into a 75 s song
        sim.run(6, analyse=False)
        self.assertEqual(sim.out.starts, 2)                                      # the stream was restarted
        self.assertEqual(e.flushes, 1)
        self.assertLess(e.delay(), 5.0)
        self.assertEqual(e.state()["room"]["id"], self.tb.id)                    # the room is on the new song already
        self.assertTrue(e.tracks[-2].skipped)
        before = e.delay()
        sim.run(self.b["duration"] - 6 - 1, analyse=False)
        sim.play(self.a, self.ta)                                                # then a natural change
        sim.run(before + 6 + 8, analyse=False)
        self.assertEqual(sim.out.starts, 2)
        self.assertAlmostEqual(e.delay() - before, 6.0, delta=0.7)               # one gap's worth further behind
        self.assertIsNone(sim.library.learned(self.ta.id))                        # a skipped song is not learned

    def test_a_change_near_the_end_is_the_song_ending_and_keeps_the_delay(self):
        # a change in a song's last stretch is taken as its end
        for early, outro, expect_flush in ((8.0, 30.0, False), (20.0, 30.0, False), (8.0, 0.0, True)):
            sim = Sim(self.dir, target=20.0, outro=outro, skip_jumps=True)
            sim.play(self.a, self.ta)
            sim.run(self.a["duration"] - early, analyse=False)
            sim.play(self.b, self.tb)
            sim.run(5, analyse=False)
            self.assertEqual(sim.engine.flushes, 1 if expect_flush else 0, f"{early} s early, outro {outro}")
            self.assertEqual(sim.engine.tracks[-2].skipped, expect_flush)
            if not expect_flush:
                self.assertGreater(sim.engine.delay(), 19.0)                     # the room is still the full delay behind
                self.assertEqual(sim.engine.state()["room"]["id"], self.ta.id)   # and still on the old song's ending

    def test_pausing_on_the_phone_holds_the_buffer(self):
        sim = Sim(self.dir, target=20.0)
        e = sim.engine
        sim.play(self.a, self.ta)
        sim.run(35, analyse=False)
        before, head = e.delay(), sim.relay.capture_time
        sim.spotify.pause()
        sim.run(12, analyse=False)
        self.assertTrue(e.gate.paused)
        self.assertIsNotNone(sim.out.paused_at)                                  # the speakers stopped too
        self.assertLess(sim.relay.capture_time - head, 1.6)                      # the silence was not recorded
        sim.spotify.resume()
        sim.capture.pos = int((35.0) * SR)                                        # the music carries on from where it was
        sim.run(10, analyse=False)
        self.assertFalse(e.gate.paused)
        self.assertIsNone(sim.out.paused_at)
        self.assertAlmostEqual(e.delay(), before, delta=1.0)                      # the room is as far behind as before
        self.assertEqual(sim.out.starts, 1)

    def test_a_saved_show_is_chosen_for_its_song(self):
        name = "The Synthetics - Test Tune 1"
        os.makedirs(os.path.join(self.dir, name))
        _touch(os.path.join(self.dir, name, "project.json"), b"{}")
        _touch(os.path.join(self.dir, name, "song.mp3"))
        sim = Sim(self.dir, target=20.0, lag=0.25)
        ref = live_bands(self.a["audio"].mean(axis=1)[: int(45 * SR)])
        sim.engine._reference = lambda jt: (ref, SR / 512)                        # stands in for the saved show's own analysis
        sim.run(2.0, analyse=False)
        true_start = sim.relay.capture_time
        sim.play(self.a, self.ta)
        sim.spotify.upcoming = [self.tb]
        sim.run(28)
        sim.engine.refresh_queue()
        t = sim.engine.current
        self.assertEqual((t.kind, t.project), ("custom", name))
        self.assertAlmostEqual(t.start, true_start, delta=0.04)
        st = sim.engine.state()
        self.assertEqual(st["room"]["project"], name)
        self.assertEqual([q["kind"] for q in st["queue"]], ["live"])
        self.assertNotIn("analysis", sim.engine.analysis_json(t.key))             # the studio loads the saved show itself

        # The studio asks for the state every second, on the server that also feeds the lights.
        # It must not touch the disk: re-measuring every saved song each time stalled the lights.
        from unittest import mock
        with mock.patch("lightshow.studio.audiotrim.total_duration", side_effect=AssertionError("song re-measured")), \
                mock.patch("builtins.open", side_effect=AssertionError("file opened")):
            for _ in range(3):
                sim.engine.state()
            sim.engine.library.match_project(self.tb)                            # listed a moment ago: served from memory


# =========================================================================== hand-over
class TestHandover(unittest.TestCase):
    def test_opening_song_hands_over_with_the_buffer_full(self):
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=20.0)
            e = sim.engine
            sim.spotify.play(ta)                      # she has a song up, as she would
            sim.capture.load(a["audio"])
            e.arm_handover()
            self.assertFalse(sim.spotify.playing)     # held while the opening song plays
            remaining = 60.0
            for _ in range(200):
                sim.run(1.0, analyse=False)
                remaining -= 1.0
                e.handover_tick(max(0.0, remaining), remaining > 0)
                if e.streaming:
                    break
            self.assertTrue(sim.spotify.playing)
            self.assertAlmostEqual(remaining, 0.0, delta=1.0)          # took over as the opener ended
            self.assertAlmostEqual(e.delay(), 20.0, delta=1.5)         # with the whole target delay already in hand
            self.assertLess(sim.relay.capture_time, 22.0)              # and nothing captured while Spotify was held
            self.assertIsNone(e.handover)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestOpeningSong(unittest.TestCase):
    """A jam opens with a saved show: Spotify plays unheard from the start, the room joins as the opener
    ends, a whole song behind. Virtual only: no speaker, no lights."""

    def run_opener(self, sim, opener_s, spotify_late=0.0, cue_at=None):
        e = sim.engine
        e.open_with_song()
        t = 0.0
        while t < opener_s + 30:
            if spotify_late and t >= spotify_late and not sim.spotify.playing:
                sim.spotify.resume()
            if cue_at is not None and t == cue_at[0]:
                e.hold()
            if cue_at is not None and t == cue_at[1]:
                e.release(restart=True)
            sim.run(1.0, analyse=False)
            t += 1.0
            e.handover_tick(max(0.0, opener_s - t), t < opener_s)
            if e.streaming:
                break
        return t

    def test_the_room_joins_a_whole_song_behind(self):
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=98.0)                 # the jam service aims at the opener's length, less a little
            sim.play(a, ta)                           # Spotify plays from the start, unheard
            t = self.run_opener(sim, 100.0)
            self.assertTrue(sim.engine.streaming)
            self.assertAlmostEqual(t, 100.0, delta=1.0)                     # as the opener ended, not before
            self.assertAlmostEqual(sim.engine.delay(), 100.0, delta=1.5)    # a whole opener behind
            self.assertIsNone(sim.engine.handover)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_spotify_starting_late_never_adds_silence_later(self):
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=98.0)
            sim.play(a, ta)
            sim.spotify.pause()
            self.run_opener(sim, 100.0, spotify_late=15.0)
            self.assertTrue(sim.engine.streaming)
            self.assertAlmostEqual(sim.engine.delay(), 85.0, delta=1.5)
            self.assertAlmostEqual(sim.engine.cfg.target_delay, 85.0, delta=1.5)   # settles on what it has
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_cue_during_the_opener_holds_the_hand_over(self):
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=98.0)
            sim.play(a, ta)
            t = self.run_opener(sim, 100.0, cue_at=(95.0, 110.0))
            self.assertGreaterEqual(t, 110.0)         # not while the cue had the speakers
            self.assertTrue(sim.engine.streaming)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestColorSets(unittest.TestCase):
    """The party's colors, and named sets of them (a Halloween set) to switch between."""

    def service(self):
        from lightshow.studio.jam_service import JamService

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""
        self.cfg = {}
        return JamService(Studio, ".", lambda: dict(self.cfg), lambda c: self.cfg.update(c), lambda: "127.0.0.1")

    def test_sets_are_saved_switched_and_forgotten(self):
        svc = self.service()
        svc._set_jam_colors({"mode": "pick", "primary": "#ff8000", "secondary": "#8000ff"})
        svc._set_jam_colors({"save_as": "Halloween"})
        c = svc._color_sets()
        self.assertEqual((c["set"], c["edited"], c["sets"]), ("Halloween", False, ["Halloween"]))
        svc._set_jam_colors({"mode": "mix", "pairs": [["#ff0000", "#00ff00"], ["#ffffff", "#ff0000"]], "save_as": "Christmas"})
        svc._set_jam_colors({"use": "Halloween"})                         # back to the orange and purple
        c = svc._color_sets()
        self.assertEqual((c["mode"], c["primary"], c["secondary"], c["set"]), ("pick", "#ff8000", "#8000ff", "Halloween"))
        svc._set_jam_colors({"secondary": "#00ff00"})                     # changed: the set is marked edited...
        self.assertTrue(svc._color_sets()["edited"])
        svc._set_jam_colors({"save": True})                               # ...until it is saved
        self.assertFalse(svc._color_sets()["edited"])
        self.assertEqual(self.cfg["color_sets"]["Halloween"]["secondary"], "#00ff00")
        svc._set_jam_colors({"delete": "Halloween"})
        c = svc._color_sets()
        self.assertEqual((c["set"], c["sets"]), ("", ["Christmas"]))
        self.assertEqual(c["primary"], "#ff8000")                         # the colors stay as they are


class TestOpenerService(unittest.TestCase):
    """The service side: which show opens, how far behind the jam aims, and what the page plays."""

    def service(self, lengths, cfg=None):
        from lightshow.studio.jam_service import JamService

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""
            _show_length = staticmethod(lambda name: lengths.get(name, 0.0))
        self.cfg = dict(cfg or {})
        return JamService(Studio, ".", lambda: dict(self.cfg), lambda c: self.cfg.update(c), lambda: "127.0.0.1")

    def test_if_youre_too_shy_opens_unless_another_is_chosen(self):
        shy = "If Youre Too Shy (Let Me Know)"
        svc = self.service({shy: 245.0, "Cheerleader": 180.0, "Jingle": 20.0})
        self.assertEqual(svc._opener_choice(), shy)
        self.assertEqual(svc._opener_plan({}), (shy, 245.0))
        self.cfg["jam_opener"] = "Cheerleader"
        self.assertEqual(svc._opener_plan({}), ("Cheerleader", 180.0))
        self.cfg["jam_opener"] = "Jingle"                   # too short to fill a buffer: no opener
        self.assertEqual(svc._opener_choice(), "")
        self.assertIsNone(svc._opener_plan({}))
        self.cfg["jam_opener"] = ""                         # none: the jam starts 90 s behind as before
        self.assertIsNone(svc._opener_plan({}))
        self.assertIsNone(self.service({})._opener_plan({}))   # Too Shy not saved here: nothing to open with

    def test_the_page_plays_the_opener_until_the_jam_has_the_room(self):
        import time as _time
        svc = self.service({})
        svc.engine = type("E", (), {"streaming": False})()
        svc.opener = {"id": "opener", "name": "Opening song", "show": "Cheerleader", "via": "pc", "device": "",
                      "run": "r1", "t0": _time.monotonic() - 30.0, "dur": 180.0, "rehearsal": False}
        st = svc._room_cue()
        self.assertEqual((st["phase"], st["show"], st["via"]), ("show", "Cheerleader", "pc"))
        self.assertAlmostEqual(st["progress"], 30 / 180, delta=0.02)
        left, playing = svc._file_status()                  # this PC's page plays it: its own clock says how much is left
        self.assertAlmostEqual(left, 150.0, delta=1.0)
        self.assertTrue(playing)
        svc.engine.streaming = True                         # the jam has the speakers: the opener is over
        self.assertEqual(svc._room_cue()["phase"], "done")

    def test_the_phone_shows_the_opening_song_in_the_room(self):
        import time as _time
        svc = self.service({})
        svc.engine = type("E", (), {"streaming": False})()
        svc.opener = {"id": "opener", "name": "Opening song", "show": "If Youre Too Shy (Let Me Know)", "via": "cast", "device": "",
                      "run": "r1", "t0": _time.monotonic() - 60.0, "dur": 319.0, "rehearsal": False}
        room = svc._opener_room()
        self.assertEqual((room["label"], room["kind"], room["opener"]), ("If Youre Too Shy (Let Me Know)", "custom", True))
        self.assertAlmostEqual(room["position"], 60.0, delta=1.0)
        svc.engine.streaming = True                         # the jam has the room: Spotify's songs from here
        self.assertIsNone(svc._opener_room())


class TestCueFades(unittest.TestCase):
    """A cue's fades are made in the jam's own audio, never on a speaker's volume. Fakes only."""

    def test_the_stream_fades_what_it_hands_out(self):
        clock = Clock()
        relay = Relay(sr=1000, channels=2, capacity_s=60, clock=clock)
        relay.write(np.full((5000, 2), 10000, dtype=np.int16))
        s = relay.open_stream(0.0, pace=False)
        s.fade(2.0, 0.0, start=1.0)                         # out over 2 s
        x = np.frombuffer(s.read(4000, timeout=0), dtype=np.int16).reshape(-1, 2)[:, 0]
        self.assertEqual(x[0], 10000)
        self.assertTrue(np.all(np.diff(x[:2000].astype(int)) <= 0))
        self.assertAlmostEqual(x[1000], 5000, delta=100)  # halfway down halfway through
        self.assertTrue(np.all(x[2000:] == 0))            # and silent after
        s2 = relay.open_stream(0.0, pace=False)
        s2.fade(1.0, 1.0, start=0.0)                       # in over 1 s
        y = np.frombuffer(s2.read(3000, timeout=0), dtype=np.int16).reshape(-1, 2)[:, 0]
        self.assertEqual(y[0], 0)
        self.assertTrue(np.all(y[1000:] == 10000))
        s2.read(500, timeout=0)
        self.assertIsNone(s2.gain)                          # done fading: back to plain copying

    def test_this_pc_fades_in_place_and_comes_back_fading_in(self):
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=10.0)
            sim.play(a, ta)
            sim.run(14.0, analyse=False)
            e = sim.engine
            self.assertTrue(e.streaming)
            starts = sim.out.starts
            wait = e.fade_out(1.5)
            self.assertAlmostEqual(wait, 2.5, delta=0.6)    # the second it holds ahead, then the fade
            self.assertEqual(sim.out.starts, starts)        # faded where it is: no new stream
            e.hold()
            e.release(restart=True, fade_in=2.0)
            self.assertEqual(sim.out.starts, starts + 1)    # the room's music back, from where it stopped
            self.assertEqual(sim.out.stream.gain[2:], (0.0, 1.0))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_cast_speaker_gets_a_fresh_stream_fading_out(self):
        from types import SimpleNamespace
        from lightshow.studio.jam import CastOutput
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=10.0)
            sim.play(a, ta)
            sim.run(14.0, analyse=False)
            e = sim.engine
            played = []
            cast = SimpleNamespace(name="Everywhere", status=lambda: {"position": 3.0},
                                   play_stream=lambda url, kind, title: played.append(url), pause=lambda: None, resume=lambda: None)
            out = CastOutput(cast, lambda sid: f"http://pc/stream/{sid}")
            e.output = out
            old = e.relay.open_stream(e.relay.capture_time - 10.0, pace=False)
            out.start_stream(old)
            old.read(44100 * 15, timeout=0)                  # the speaker has pulled 15 s ahead
            room = e.room_capture_time()
            wait = e.fade_out(2.0)
            self.assertEqual(played, ["http://pc/stream/1", "http://pc/stream/2"])   # a fresh stream, as for a skip
            self.assertAlmostEqual(out.stream.cursor / e.relay.sr, room, delta=0.05)  # from where the room is
            self.assertEqual(out.stream.gain[2:], (1.0, 0.0))
            self.assertGreaterEqual(wait, 2.0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestCueInTheStream(unittest.TestCase):
    """A cue's song played inside the jam's own stream, crossfading in and out. Fakes only."""

    def test_the_stream_crossfades_into_the_song_and_back_into_the_music(self):
        clock = Clock()
        relay = Relay(sr=1000, channels=2, capacity_s=60, clock=clock)
        relay.write(np.full((10000, 2), 1000, dtype=np.int16))          # the music
        s = relay.open_stream(2.0, pace=False)
        s.insert(np.full((3000, 2), 8000, dtype=np.int16), lead=500, tail=1000)
        x = np.frombuffer(s.read(10000, timeout=0), dtype=np.int16).reshape(-1, 2)[:, 0]
        while len(x) < 5000:
            x = np.concatenate([x, np.frombuffer(s.read(10000, timeout=0), dtype=np.int16).reshape(-1, 2)[:, 0]])
        self.assertEqual(x[0], 1000)                                     # from the music...
        self.assertTrue(np.all(x[600:1900] == 8000))                     # ...to the song alone...
        self.assertGreater(x[2500], 1000)                                # ...the two together at its end...
        self.assertLess(x[2500], 8000)
        self.assertTrue(np.all(x[3000:5000] == 1000))                    # ...and the music carries on
        self.assertIsNone(s.cut)
        # the music picks up after what the two crossfades used: 0.5 s at the start, 1 s at the end
        self.assertAlmostEqual(s.capture_time_at(3.0), 2.0 + 0.5 + 1.0, delta=0.01)
        self.assertIsNone(s.capture_time_at(1.0))                         # the song alone: none of the music

    def test_the_engine_holds_the_music_while_the_song_plays_in_its_stream(self):
        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=10.0)
            sim.play(a, ta)
            sim.run(14.0, analyse=False)
            e = sim.engine
            starts, room = sim.out.starts, e.room_capture_time()
            pcm = np.zeros((SR * 6, 2), dtype=np.int16)
            self.assertTrue(e.cue_insert(pcm, 1.0, 2.0))
            self.assertEqual(sim.out.starts, starts + 1)                 # one fresh stream, as for a skip
            self.assertFalse(sim.spotify.playing)                        # the buffer is held
            self.assertAlmostEqual(e.held_at, room + 1.0, delta=0.05)
            paused_at = sim.out.paused_at
            sim.run(3.0, analyse=False)
            self.assertEqual(sim.out.paused_at, paused_at)               # the speakers never paused
            self.assertAlmostEqual(e.cue_position(), 3.0, delta=0.6)    # the room is 3 s into the song
            sim.run(1.0, analyse=False)
            e.cue_over()                                                 # the song crossfades back into the music
            self.assertTrue(sim.spotify.playing)
            sim.run(4.0, analyse=False)
            self.assertFalse(e.gate.paused)
            self.assertEqual(sim.out.starts, starts + 1)                 # no other stream: the music was in this one
            self.assertIsNotNone(e.room_capture_time())
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ========================================================================== sync check
class FakeMic:
    """A microphone in a room that hears the relay late, quieter, and with noise on top."""

    def __init__(self, sim, extra_delay):
        self.sim, self.extra = sim, extra_delay

    def __call__(self, device, sr):
        self.sr = sr
        return self

    def __enter__(self):
        self.t0 = self.sim.clock()
        self.rc0 = self.sim.engine.room_capture_time()
        return self

    def __exit__(self, *a):
        pass

    def wait(self, seconds):
        self.seconds = seconds
        self.sim.run(seconds, analyse=False)

    def audio(self):
        heard_from = self.rc0 - self.extra                             # the room is really this much further behind
        y = self.sim.relay.audio(heard_from, heard_from + self.seconds, self.sr, mono=True)
        rng = np.random.default_rng(0)
        return (0.3 * y + 0.02 * rng.standard_normal(len(y))).astype(np.float32), self.t0


class TestRelaySyncCheck(unittest.TestCase):
    def test_measures_how_late_the_room_really_is(self):
        from lightshow.studio.calibrate import relay_sync_check

        d = tempfile.mkdtemp()
        try:
            a, ta = song(128.0, 1)
            sim = Sim(d, target=20.0)
            sim.play(a, ta)
            sim.run(45, analyse=False)
            r = relay_sync_check(sim.engine, seconds=7.0, recorder=FakeMic(sim, 0.31), log=lambda *_: None)
            self.assertAlmostEqual(r["offset_ms"], 310, delta=25)
            self.assertGreaterEqual(r["confidence"], 0.5)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ============================================================================== routes
class TestStreamRoute(unittest.IsolatedAsyncioTestCase):
    async def test_speakers_are_served_an_endless_wav_of_delayed_audio(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from lightshow.studio.jam_service import JamService

        class Studio:
            port = 0
            class cast:            # noqa: N801 - a stand-in with the two attributes the service reads
                cast, name, media_path = None, "", ""

        d = tempfile.mkdtemp()
        try:
            svc = JamService(Studio, d, lambda: {}, lambda cfg: None, lambda: "127.0.0.1")
            sim = Sim(d, target=5.0)
            a, ta = song(128.0, 1)
            sim.play(a, ta)
            sim.run(12, analyse=False)
            svc.engine = sim.engine
            sim.out.sid = 1
            app = web.Application()
            app.add_routes(svc.routes())
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get("/api/jam/stream.wav?sid=99")).status, 404)      # a stale speaker request
                resp = await client.get("/api/jam/stream.wav?sid=1")
                self.assertEqual(resp.headers["Content-Type"], "audio/wav")
                head = await resp.content.readexactly(44)
                self.assertEqual(head[:4], b"RIFF")
                body = await resp.content.readexactly(4 * 2000)
                self.assertGreater(int(np.abs(np.frombuffer(body, dtype=np.int16)).max()), 100)   # real audio, not silence
                resp.close()
                st = await (await client.get("/api/jam/state")).json()
                self.assertTrue(st["running"] and st["streaming"])
                self.assertEqual(st["room"]["id"], ta.id)
                doc = await (await client.get("/api/jam/analysis?key=" + st["room"]["key"])).json()
                self.assertEqual(doc["kind"], "live")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestDemo(unittest.TestCase):
    def test_the_demo_plays_pauses_and_skips_like_a_phone(self):
        from lightshow.studio.jam_demo import DemoSpotify

        relay = Relay(sr=8000, channels=2, capacity_s=30)
        demo = DemoSpotify(relay, Gate())
        first = demo.now_playing().track
        demo.playing = True
        demo.capture.step(2.0)
        demo.position = 2.0
        self.assertAlmostEqual(relay.capture_time, 2.0, places=2)
        demo.skip()
        self.assertNotEqual(demo.now_playing().track.id, first.id)
        self.assertEqual(demo.now_playing().progress, 0.0)
        self.assertEqual(len(demo.queue()), 3)


if __name__ == "__main__":
    unittest.main()


class TestTrackColors(unittest.TestCase):
    def test_a_tracks_colors_are_kept_and_can_be_cleared(self):
        d = tempfile.mkdtemp()
        try:
            JamLibrary(d).set_colors("abc", "#8000ff", "#00ff00")
            self.assertEqual(JamLibrary(d).colors(), {"abc": {"primary": "#8000ff", "secondary": "#00ff00"}})   # kept on disk
            lib = JamLibrary(d)
            lib.set_colors("abc", None, None)                  # back to the jam's colors
            self.assertEqual(JamLibrary(d).colors(), {})
        finally:
            shutil.rmtree(d)

    def test_a_songs_vibe_can_be_picked_by_hand(self):
        d = tempfile.mkdtemp()
        try:
            JamLibrary(d).set_style("abc", "chill")                       # taken for a rager, but it is chill
            self.assertEqual(JamLibrary(d).styles(), {"abc": "chill"})     # kept on disk
            JamLibrary(d).set_style("abc", "banger")                      # not a style: back to the one found
            self.assertEqual(JamLibrary(d).styles(), {})
        finally:
            shutil.rmtree(d)

    def test_a_song_can_keep_to_just_its_two_colors(self):
        d = tempfile.mkdtemp()
        try:
            JamLibrary(d).set_colors("abc", "#ff0000", "#ff8000", only=True)     # a red and orange song
            self.assertEqual(JamLibrary(d).colors(), {"abc": {"primary": "#ff0000", "secondary": "#ff8000", "only": True}})
            JamLibrary(d).set_colors("abc", "#ff0000", "#ff8000")                # accents allowed again
            self.assertNotIn("only", JamLibrary(d).colors()["abc"])
        finally:
            shutil.rmtree(d)


class TestPairPerSong(unittest.TestCase):
    """'A different pair each song': stable per track, and never the same pair twice in a row."""

    def service(self, moods=None):
        from types import SimpleNamespace
        from lightshow.studio.jam_service import JamService
        moods = moods or {}
        lib = SimpleNamespace(learned=lambda tid: {"analysis": {"mood": moods[tid]}} if tid in moods else None)
        s = SimpleNamespace(library=lib, _assigned={}, _moods={},
                            _jam_colors=lambda: {**JamService.DEFAULT_COLORS, "mode": "mix"})
        s._vibe_order = lambda tid, n: JamService._vibe_order(s, tid, n)
        s.assign = lambda tracks: JamService._assign_colors(s, tracks)
        return s

    def test_neighbors_differ_and_a_track_keeps_its_pair(self):
        s = self.service()
        tracks = [{"id": f"t{i}", "kind": "live"} for i in range(12)]
        first = s.assign(tracks)
        pairs = [tuple(first[t["id"]]) for t in tracks]
        self.assertTrue(all(a != b for a, b in zip(pairs, pairs[1:])))
        self.assertEqual(s.assign(tracks[3:]), {t["id"]: first[t["id"]] for t in tracks[3:]})   # the queue moved on: same pairs
        self.assertGreaterEqual(len(set(pairs)), 3)

    def test_vibe_picks_the_pair_and_a_custom_show_gets_none(self):
        s = self.service({"hard": {"drive": 0.8, "mode": "minor"}, "soft": {"drive": 0.3, "mode": "minor"}})
        out = s.assign([{"id": "hard", "kind": "learned"}, {"id": "mine", "kind": "custom"}, {"id": "soft", "kind": "learned"}])
        self.assertEqual(out["hard"], ["#ff0000", "#0040ff"])              # hard and dark: red/blue
        self.assertEqual(out["soft"], ["#8000ff", "#00ff00"])              # soft and dark: purple/green
        self.assertNotIn("mine", out)


class TestPartyHub(unittest.TestCase):
    def test_the_extras_are_picked_from_a_tracks_two_colors(self):
        import colorsys
        from lightshow.studio.jam_service import extras_for

        hue = lambda hx: colorsys.rgb_to_hsv(*(int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)))[0] * 360
        red, orange = extras_for(["#8000ff", "#00ff00"])           # purple and green: red and orange
        self.assertTrue(hue(red) > 340 or hue(red) < 15)
        self.assertTrue(20 < hue(orange) < 45)
        self.assertEqual(extras_for(["#ffffff", "#000000"]), ["#ff6000", "#ff0000"])   # no hue to steer by
        self.assertIsNone(extras_for(None))

    def test_the_party_colors_switch_off_for_every_song_or_one(self):
        from types import SimpleNamespace
        from lightshow.studio.jam_service import JamService

        def hub(jam_colors, track_colors):
            room = {"id": "a", "label": "A", "kind": "live"}
            st = {"running": True, "room": room, "queue": [{"id": "b", "label": "B", "kind": "live"}],
                  "jam_colors": {**JamService.DEFAULT_COLORS, **jam_colors}, "track_colors": track_colors, "assigned_colors": {}}
            fake = SimpleNamespace(_state=lambda: st, party_level=1.0, studio=SimpleNamespace(rooms=None), _found_style=lambda t: None, _found_genres=lambda t: [])
            return JamService._party(fake)
        pick = {"mode": "pick", "primary": "#8000ff", "secondary": "#00ff00"}
        on = hub(pick, {})
        self.assertEqual(on["room"]["colors"], ["#8000ff", "#00ff00"])
        self.assertTrue(on["theme_on"])
        self.assertNotIn("extras", on["room"])                             # two colors a song, nothing more
        off = hub({**pick, "on": False}, {"a": {"primary": "#ff0000", "secondary": "#0000ff"}})
        self.assertIsNone(off["room"]["colors"])                           # every song in its cover's colors
        self.assertFalse(off["theme_on"])
        one = hub(pick, {"a": {"off": True}})
        self.assertIsNone(one["room"]["colors"])                           # this song in its cover's
        self.assertTrue(one["room"]["theme_off"])
        self.assertEqual(one["upcoming"][0]["colors"], ["#8000ff", "#00ff00"])   # the rest keep the party's

    def test_only_the_home_network_may_use_it(self):
        from types import SimpleNamespace
        from lightshow.studio.jam_service import JamService
        ok = lambda ip: JamService._lan(SimpleNamespace(remote=ip))
        self.assertTrue(ok("192.168.1.40"))
        self.assertTrue(ok("10.0.0.7"))
        self.assertTrue(ok("127.0.0.1"))
        self.assertFalse(ok("8.8.8.8"))
        self.assertFalse(ok(""))


class TestCueHold(unittest.TestCase):
    """A Party Hub cue holds the room where it is, and gives it back from exactly there."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_room_holds_through_a_cue_and_carries_on_from_there(self):
        a, ta = song(120, 7)
        sim = Sim(self.dir, target=10.0)
        e = sim.engine
        sim.play(a, ta)
        sim.run(16, analyse=False)
        self.assertTrue(e.streaming)
        rc = e.room_capture_time()
        self.assertEqual(e.hold(), rc)
        self.assertTrue(e.gate.paused)
        self.assertFalse(sim.spotify.playing)
        sim.run(6, analyse=False)                              # the show plays: the room waits where it was
        self.assertEqual(e.room_capture_time(), rc)
        sim.spotify.playing = True                             # Spotify heard playing mid-cue must not let the room go
        sim.run(1, analyse=False)
        self.assertTrue(e.gate.paused)
        sim.spotify.playing = False
        starts = sim.out.starts
        e.release(restart=True)                                # the speakers get the jam's stream back...
        self.assertEqual(sim.out.starts, starts + 1)
        sim.out.pump()                                         # the speaker pulls its first second
        self.assertAlmostEqual(e.room_capture_time(), rc, delta=0.05)   # ...from where the room stopped
        self.assertTrue(sim.spotify.playing)
        sim.run(2, analyse=False)
        self.assertFalse(e.gate.paused)
        self.assertGreater(e.room_capture_time(), rc + 1.0)    # and the room runs on


class TestLiveWaveform(unittest.TestCase):
    def test_the_live_waveform_keeps_up_with_the_music(self):
        """Fed in uneven pieces (as the capture arrives), the waveform still covers exactly the time
        captured: it used to lose up to half a peak a piece and end about 8% short of the song."""
        from lightshow.studio.jam import FeatureTrack
        from lightshow.studio.analysis import WAVE_PPS
        relay = Relay(sr=22050, channels=2, capacity_s=400)
        ft = FeatureTrack(relay)
        rng = np.random.default_rng(4)
        for k in range(400):                                   # about 200 s, in pieces of 0.3 to 0.7 s
            n = int(22050 * (0.3 + 0.4 * rng.random()))
            relay.write((rng.normal(0, 3000, (n, 2))).astype(np.int16))
            ft.update()
        self.assertAlmostEqual(len(ft.peaks) / WAVE_PPS, ft.done - ft.base, delta=1.5 / WAVE_PPS)
        self.assertGreater(ft.done, 150)
