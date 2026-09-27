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
                if not self.engine.streaming and self.engine.buffered() >= self.engine.cfg.target_delay:
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
