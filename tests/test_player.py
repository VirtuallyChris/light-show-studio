"""The studio's own Spotify player: run with  .venv\\Scripts\\python -m unittest tests.test_player -v

Spotify is stood in for; nothing is played and nothing leaves this computer.
"""

import json
import types
import unittest

from lightshow.studio.jam_service import JamService
from lightshow.studio.spotify import NowPlaying, PlayerAwareSpotify, PlayerState, SpotifyClient, Track


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


SDK_STATE = {"position_ms": 61500, "paused": False, "track": {
    "id": "t1", "uri": "spotify:track:t1", "name": "Cheerleader", "duration_ms": 201000,
    "artists": [{"name": "Porter Robinson", "uri": "spotify:artist:a1"}]}}


class FakeClient:
    signed_in = True

    def __init__(self):
        self.asked = 0

    def now_playing(self):
        self.asked += 1
        return NowPlaying(Track("web", "From the Web API", [], 100.0), 5.0, True, 0.0, "phone")

    def queue(self):
        return ["queued"]


class TestPlayerState(unittest.TestCase):
    def test_reads_what_the_player_page_reports(self):
        clock = Clock()
        p = PlayerState(clock)
        p.update({"device_id": "dev123", "ready": True})
        p.update({"state": SDK_STATE})
        self.assertEqual((p.device_id, p.ready, p.paused, p.position), ("dev123", True, False, 61.5))
        self.assertEqual((p.track.id, p.track.label, p.track.duration, p.track.artist_ids), ("t1", "Porter Robinson - Cheerleader", 201.0, ["a1"]))
        p.update({"state": {}})                                   # nothing playing
        self.assertIsNone(p.track)

    def test_the_players_clock_is_used_while_it_is_live(self):
        clock = Clock()
        p = PlayerState(clock)
        p.update({"device_id": "dev", "ready": True})
        p.update({"state": SDK_STATE})
        web = FakeClient()
        sp = PlayerAwareSpotify(web, p)
        clock.t += 2.5
        now = sp.now_playing()
        self.assertEqual((now.track.id, now.playing, now.device), ("t1", True, "Light Show Studio"))
        self.assertAlmostEqual(now.progress, 64.0)                  # runs on between reports
        self.assertEqual(web.asked, 0)
        self.assertEqual(sp.queue(), ["queued"])                    # everything else is the Web API client

    def test_paused_holds_and_a_quiet_player_falls_back_to_the_web_api(self):
        clock = Clock()
        p = PlayerState(clock)
        p.update({"device_id": "dev", "ready": True})
        p.update({"state": {**SDK_STATE, "paused": True}})
        sp = PlayerAwareSpotify(FakeClient(), p)
        clock.t += 3.0
        self.assertAlmostEqual(sp.now_playing().progress, 61.5)
        clock.t += 10.0                                             # no word from the player for too long
        self.assertEqual(sp.now_playing().track.id, "web")
        p.update({"device_id": "", "ready": False})                 # the player window was closed
        p.update({"state": SDK_STATE})
        self.assertEqual(sp.now_playing().track.id, "web")


class TestClient(unittest.TestCase):
    def client(self, responses):
        self.calls = []

        def transport(method, url, headers, body):
            self.calls.append((method, url, body))
            return responses.pop(0)

        self.store = {"client_id": "x", "refresh_token": "r"}
        return SpotifyClient(self.store, transport=transport, clock=Clock())

    def test_sign_in_asks_to_stream_and_remembers_what_was_granted(self):
        from urllib.parse import parse_qs, urlparse
        c = self.client([(200, json.dumps({"access_token": "A", "expires_in": 3600, "scope": "streaming user-read-email user-read-private user-read-playback-state"}).encode())])
        self.assertIn("streaming", parse_qs(urlparse(c.auth_url("http://127.0.0.1:8765/cb")).query)["scope"][0].split())
        self.assertFalse(c.can_stream)                              # an older sign-in: no scope recorded
        self.assertEqual(c.token(), "A")
        self.assertTrue(c.can_stream)

    def test_playback_can_be_handed_to_the_studio_player(self):
        c = self.client([(200, b'{"access_token":"A","expires_in":3600}'), (204, b"")])
        self.assertTrue(c.transfer_playback("dev123"))
        method, url, body = self.calls[-1]
        self.assertEqual((method, url.rsplit("/v1", 1)[1]), ("PUT", "/me/player"))
        self.assertEqual(json.loads(body), {"device_ids": ["dev123"], "play": True})


class TestAutoStart(unittest.TestCase):
    """Music starting on Light Show Studio starts a jam; a reloaded studio takes its music back."""

    def service(self):
        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""
        self.cfg = {"jam_last": {"speakers": "pc", "pc_device": "Headphones", "target_delay": 30.0}, "jam_fetch": False}
        svc = JamService(Studio, ".", lambda: dict(self.cfg), lambda c: self.cfg.update(c), lambda: "127.0.0.1")
        svc.spotify.store.update(client_id="x", refresh_token="r")   # signed in
        self.started, self.transfers = [], []

        def fake_start(body):
            self.started.append(body)
            svc.engine = object()
            svc.source = body.get("source")
        svc._start = fake_start
        svc.spotify.transfer_playback = lambda dev, play=True: self.transfers.append(dev)
        return svc

    def report(self, svc, rep):
        p = svc.player
        before, was = p.device_id, p.track is not None and not p.paused
        p.update(rep)
        job = svc._after_report(rep, before, was)
        if job:
            job()

    def test_music_starting_starts_a_jam_with_the_last_settings_once(self):
        svc = self.service()
        self.report(svc, {"device_id": "dev1", "ready": True})
        self.report(svc, {"state": {**SDK_STATE, "paused": True}})
        self.assertEqual(self.started, [])                        # paused: nothing yet
        self.report(svc, {"state": {**SDK_STATE, "stamp": 1}})
        self.assertEqual(len(self.started), 1)
        self.assertEqual({k: self.started[0][k] for k in ("source", "speakers", "pc_device", "target_delay", "auto")},
                         {"source": "studio", "speakers": "pc", "pc_device": "Headphones", "target_delay": 30.0, "auto": True})
        svc.engine = None                                         # Stop pressed while the music carries on
        self.report(svc, {"state": {**SDK_STATE, "position_ms": 70000, "stamp": 2}})
        self.assertEqual(len(self.started), 1)                    # not started again behind the user's back
        self.report(svc, {"state": {**SDK_STATE, "paused": True, "stamp": 3}})
        self.report(svc, {"state": {**SDK_STATE, "stamp": 4}})
        self.assertEqual(len(self.started), 2)                    # but it is when the music starts again

    def test_learning_ahead_an_auto_started_jam_keeps_the_short_delay(self):
        from unittest import mock
        svc = self.service()
        self.cfg["jam_fetch"] = True
        with mock.patch("lightshow.studio.fetch_ahead.available", lambda: True):
            self.report(svc, {"device_id": "dev1", "ready": True})
            self.report(svc, {"state": {**SDK_STATE, "stamp": 1}})
        self.assertEqual(len(self.started), 1)
        self.assertNotIn("target_delay", self.started[0])          # the jam's own learn-ahead delay (10 s), not the last 30 s

    def test_auto_start_can_be_turned_off(self):
        svc = self.service()
        self.cfg["jam_auto"] = False
        self.report(svc, {"device_id": "dev1", "ready": True})
        self.report(svc, {"state": {**SDK_STATE, "stamp": 1}})
        self.assertEqual(self.started, [])

    def test_a_reloaded_studio_takes_its_music_back(self):
        svc = self.service()
        self.report(svc, {"device_id": "dev1", "ready": True})
        self.report(svc, {"state": {**SDK_STATE, "stamp": 1}})       # the jam starts
        self.report(svc, {"device_id": "dev2", "ready": True})        # the window was reloaded: a new player
        self.assertEqual(self.transfers, ["dev2"])
        self.assertEqual(len(self.started), 1)


class TestHiddenPlayer(unittest.TestCase):
    def test_finds_only_the_players_own_audio_process(self):
        from lightshow.studio.player_browser import parse_audio_pids
        prof = r"C:\Users\x\AppData\Local\lanlightshow\player-browser"
        listing = "\n".join([
            f'101\t"msedge.exe" --type=utility --utility-sub-type=audio.mojom.AudioService --user-data-dir={prof}',
            f'102\t"msedge.exe" --type=renderer --user-data-dir={prof}',
            '103\t"msedge.exe" --type=utility --utility-sub-type=audio.mojom.AudioService',        # your own Edge
            ''])
        self.assertEqual(parse_audio_pids(listing, prof), [101])

    def test_refuses_when_the_hidden_output_is_the_default(self):
        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""
        svc = JamService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
        svc.spotify.store.update(client_id="x", refresh_token="r")
        svc._hidden_output_name = lambda: "Digital Audio (S/PDIF)"
        svc._same_device = lambda a, b: True                          # Windows' default output is S/PDIF
        with self.assertRaises(ValueError) as e:
            svc._start({"speakers": "pc", "source": "app"})
        self.assertIn("default output", str(e.exception))


class TestCastFollows(unittest.TestCase):
    """Casting mid-jam moves the room's sound to the Cast speaker, at the point it had reached."""

    def test_the_jam_moves_to_the_cast_and_back(self):
        import threading
        import time as _t
        import numpy as np
        from lightshow.studio.jam import CastOutput
        from lightshow.studio.relay import Relay

        played = []

        class FakeCast:
            def play_stream(self, url, ctype, title):
                played.append(url)

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "Office Home 2", ""
                play_stream = FakeCast().play_stream

        class PcOut:                                   # stands in for this PC's output (never makes a sound)
            device_name, name, offset, stream = "default", "This PC", 0.0, None
            def __init__(self): self.stopped = False
            def stop(self): self.stopped = True
            def start_stream(self, stream): self.stream = stream
            def pause(self): pass

        class Engine:
            def __init__(self):
                self.lock = threading.RLock()
                self.output, self.streaming = PcOut(), True
                self.gate = type("G", (), {"paused": False})()
                self.relay = Relay(sr=1000, channels=2, capacity_s=120)
                self.relay.write(np.zeros((60_000, 2), dtype=np.int16))
                self.cfg = type("C", (), {"target_delay": 45.0})()
                self.log = []
            def room_capture_time(self): return 12.5
            def say(self, m): self.log.append(m)

        svc = JamService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
        svc.engine, svc.source = Engine(), "studio"
        pc = svc.engine.output
        svc._output_for_real = svc._output_for
        svc._output_for = lambda where, *a, **k: PcOut() if where == "pc" else svc._output_for_real(where, *a, **k)

        Studio.cast.cast = object()                    # Cast pressed
        svc._follow_cast()
        for _ in range(50):
            if isinstance(svc.engine.output, CastOutput): break
            _t.sleep(0.02)
        out = svc.engine.output
        self.assertIsInstance(out, CastOutput)
        self.assertTrue(pc.stopped)
        self.assertEqual(len(played), 1)
        self.assertAlmostEqual(out.stream.cursor / 1000.0, 12.5, delta=0.01)   # the same point in the music

        Studio.cast.cast = None                        # the Cast went away: back to this PC
        svc._follow_cast()
        for _ in range(50):
            if svc._output_kind(svc.engine.output) == "pc" and not getattr(svc, "_switching", False): break
            _t.sleep(0.02)
        self.assertEqual(svc._output_kind(svc.engine.output), "pc")


class TestSpeakersAlwaysPlay(unittest.TestCase):
    def test_a_jam_meant_for_speakers_is_never_left_silent(self):
        import threading
        import time as _t
        from lightshow.studio.jam import VirtualOutput

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""

        class PcOut:
            device_name, name, offset, stream = "default", "This PC", 0.0, None
            def stop(self): pass
            def start_stream(self, stream): self.stream = stream
            def pause(self): pass

        class Engine:
            def __init__(self):
                import numpy as np
                from lightshow.studio.relay import Relay
                self.lock, self.streaming = threading.RLock(), True
                self.output = VirtualOutput(_t.monotonic)
                self.gate = type("G", (), {"paused": False})()
                self.relay = Relay(sr=1000, channels=2, capacity_s=120)
                self.relay.write(np.zeros((60_000, 2), dtype=np.int16))
                self.cfg = type("C", (), {"target_delay": 45.0})()
                self.log = []
            def room_capture_time(self): return 20.0
            def say(self, m): self.log.append(m)

        for wanted, expect in ((False, "none"), (True, "pc")):
            svc = JamService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
            svc.engine, svc.source, svc.room_wanted = Engine(), "studio", wanted
            svc._output_for = lambda where, *a, **k: PcOut() if where == "pc" else VirtualOutput(_t.monotonic)
            svc._follow_cast()
            for _ in range(50):
                if not getattr(svc, "_switching", False):
                    break
                _t.sleep(0.02)
            self.assertEqual(svc._output_kind(svc.engine.output), expect, f"speakers wanted: {wanted}")


class TestCastThatDoesNotPlay(unittest.TestCase):
    def test_a_silent_cast_hands_the_room_to_this_pc_until_cast_is_pressed(self):
        import threading
        import time as _t
        import numpy as np
        from lightshow.studio.jam import CastOutput
        from lightshow.studio.relay import Relay

        class Cast:                                   # a speaker that never starts playing
            state = "UNKNOWN"
            def play_stream(self, *a): pass
            def status(self): return {"position": 0.0}
            def stop(self): pass

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = object(), "Everywhere", ""
                @staticmethod
                def status(): return {"state": Studio.cast.state_now}
                state_now = "UNKNOWN"
                @staticmethod
                def disconnect(): Studio.cast.cast = None       # let go of a speaker that failed
                play_stream = Cast().play_stream
                stop = Cast().stop

        class PcOut:
            device_name, name, offset, stream = "default", "This PC", 0.0, None
            def stop(self): pass
            def start_stream(self, stream): self.stream = stream
            def pause(self): pass

        relay = Relay(sr=1000, channels=2, capacity_s=120)
        relay.write(np.zeros((60_000, 2), dtype=np.int16))

        class Engine:
            def __init__(self):
                self.lock, self.streaming, self.relay, self.log = threading.RLock(), True, relay, []
                self.gate = type("G", (), {"paused": False})()
                self.cfg = type("C", (), {"target_delay": 45.0})()
                self.output = CastOutput(Cast(), lambda sid: f"http://x/{sid}")
                self.output.start_stream(relay.open_stream(0.0))
            def room_capture_time(self): return 10.0
            def say(self, m): self.log.append(m)

        svc = JamService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
        svc.engine, svc.source, svc.room_wanted = Engine(), "studio", True
        real = svc._output_for
        svc._output_for = lambda where, *a, **k: PcOut() if where == "pc" else real(where, *a, **k)
        wait = lambda: [_t.sleep(0.02) for _ in range(50) if getattr(svc, "_switching", False)]

        svc._follow_cast(); wait()
        self.assertEqual(svc._output_kind(svc.engine.output), "cast")     # still within its 15 s to start
        svc.engine.output.started_at -= 20                                # 20 s on, and it never played
        svc._follow_cast(); wait()
        self.assertEqual(svc._output_kind(svc.engine.output), "pc")
        self.assertTrue(any("not playing" in m for m in svc.engine.log))
        svc._follow_cast(); wait()
        self.assertEqual(svc._output_kind(svc.engine.output), "pc")       # and it stays here
        _t.sleep(0.1)
        self.assertIsNone(Studio.cast.cast)                               # it was let go of: the studio shows it
        svc._cast_failed, Studio.cast.cast = None, object()               # Cast pressed again: a new connection
        Studio.cast.state_now = "PLAYING"
        svc._follow_cast(); wait()
        self.assertEqual(svc._output_kind(svc.engine.output), "cast")


class TestAutoSync(unittest.TestCase):
    def test_a_speaker_that_starts_playing_gets_one_sync_check(self):
        import threading
        import time as _t
        from lightshow.studio.jam import CastOutput

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = object(), "Everywhere", ""
                @staticmethod
                def status(): return {"state": "PLAYING"}

        class Cast:
            def play_stream(self, *a): pass
            def status(self): return {"position": 12.0}

        class Engine:
            def __init__(self):
                self.lock, self.streaming, self.log = threading.RLock(), True, []
                self.gate = type("G", (), {"paused": False})()
                self.output = CastOutput(Cast(), lambda sid: f"http://x/{sid}")
                self.output.started_at = _t.monotonic()
            def say(self, m): self.log.append(m)

        svc = JamService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
        e = svc.engine = Engine()
        runs = []
        svc._run_sync = lambda: runs.append(1) or {"saved_for": "Everywhere", "speaker_offset_ms": 250, "offset_ms": 13}
        svc._maybe_auto_sync(e)
        self.assertEqual(runs, [])                                    # not yet: it has only just started
        e.output.started_at -= 10
        svc._maybe_auto_sync(e)
        for _ in range(50):
            if runs and not svc._syncing:
                break
            _t.sleep(0.02)
        svc._maybe_auto_sync(e)
        _t.sleep(0.1)
        self.assertEqual(runs, [1])                                   # once for this start, not on every poll
        self.assertTrue(any("250 ms" in m for m in e.log))

        e.output.started_at = _t.monotonic() - 10                     # restarted (a reconnect): checked again
        svc._maybe_auto_sync(e)
        for _ in range(50):
            if len(runs) == 2:
                break
            _t.sleep(0.02)
        self.assertEqual(len(runs), 2)

        # a muted microphone: listened to again every SILENT_RETRY_S for the same stream, said once
        svc._run_sync = lambda: runs.append(1) or setattr(svc, "sync_warning", "muted") or {"mic_silent": True}
        e.output.started_at = _t.monotonic() - 10
        svc._maybe_auto_sync(e)
        for _ in range(50):
            if len(runs) == 3 and not svc._syncing:
                break
            _t.sleep(0.02)
        self.assertGreater(svc._sync_retry_at, _t.monotonic())
        svc._maybe_auto_sync(e)
        _t.sleep(0.05)
        self.assertEqual(len(runs), 3)                                # not before the retry is due
        svc._sync_retry_at = _t.monotonic() - 1
        svc._maybe_auto_sync(e)
        for _ in range(50):
            if len(runs) == 4 and not svc._syncing:
                break
            _t.sleep(0.02)
        self.assertEqual(len(runs), 4)
        self.assertEqual(sum("heard nothing" in m for m in e.log), 1)



    def test_a_doorbell_or_a_catch_up_stream_gets_none(self):
        """A sync check ran 8 s into a doorbell (listening to its song) and again as the music came back, crunching the
        microphone's recording while the lights faded: they stuttered and the studio lagged (the user)."""
        import threading
        import time as _t
        from lightshow.studio.jam import CastOutput

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = object(), "Everywhere", ""
                @staticmethod
                def status(): return {"state": "PLAYING"}

        class Cast:
            def play_stream(self, *a): pass
            def status(self): return {"position": 12.0}

        class Engine:
            cue_hold = False
            def __init__(self):
                self.lock, self.streaming, self.log = threading.RLock(), True, []
                self.gate = type("G", (), {"paused": False})()
                self.output = CastOutput(Cast(), lambda sid: f"http://x/{sid}")
                self.output.started_at = _t.monotonic() - 10
                self.no_sync_start = self.output.started_at            # (JamEngine._quiet_start)
            def say(self, m): self.log.append(m)

        svc = JamService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
        e = svc.engine = Engine()
        runs = []
        svc._run_sync = lambda: runs.append(1) or {}
        svc._maybe_auto_sync(e)
        e.no_sync_start, e.cue_hold = None, True                      # a doorbell holding the room: none either
        svc._maybe_auto_sync(e)
        _t.sleep(0.1)
        self.assertEqual(runs, [])

class TestTaskbarIdentity(unittest.TestCase):
    def test_a_pinned_studio_starts_the_studio_with_its_own_icon(self):
        import os
        import re
        from lightshow.studio import window_identity as wi
        files = re.findall(r'"([^"]+)"', wi.relaunch_command())
        self.assertTrue(files[0].lower().endswith("wscript.exe"))
        self.assertTrue(os.path.isfile(files[1]) and files[1].endswith("Light Show Studio.vbs"), files)
        self.assertTrue(os.path.isfile(wi.icon_resource().rsplit(",", 1)[0]))
        self.assertNotIn("chrome", wi.APP_ID.lower())


class TestLocalOnly(unittest.TestCase):
    def test_the_token_and_player_routes_answer_this_computer_only(self):
        req = lambda addr: types.SimpleNamespace(remote=addr)
        self.assertTrue(JamService._local(req("127.0.0.1")))
        self.assertTrue(JamService._local(req("::1")))
        self.assertFalse(JamService._local(req("192.168.1.50")))   # a phone or speaker on the network
        self.assertFalse(JamService._local(req(None)))


class TestRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_the_player_page_reports_and_the_studio_hears_it(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""

        svc = JamService(Studio, ".", lambda: {}, lambda cfg: None, lambda: "127.0.0.1")
        svc.spotify.token = lambda: "TOKEN"
        app = web.Application()
        app.add_routes(svc.routes())
        async with TestClient(TestServer(app)) as client:
            self.assertEqual((await (await client.get("/api/spotify/token")).json())["token"], "TOKEN")
            await client.post("/api/jam/sdk_state", json={"device_id": "dev9", "ready": True})
            await client.post("/api/jam/sdk_state", json={"state": SDK_STATE})
            st = await (await client.get("/api/jam/state")).json()
            self.assertEqual((st["player"]["ready"], st["player"]["device_id"], st["player"]["track"]), (True, "dev9", "Porter Robinson - Cheerleader"))
            self.assertIn("can_stream", st["spotify"])


if __name__ == "__main__":
    unittest.main()


class TestClosing(unittest.IsolatedAsyncioTestCase):
    async def test_closing_the_window_quits_at_once_but_a_reload_does_not(self):
        import asyncio
        from aiohttp.test_utils import TestClient, TestServer
        from lightshow.studio.server import Studio
        quits = []
        for page_back, expect in ((False, 1), (True, 0)):
            s = Studio(port=0, quit_when_closed=True, lights=False, player=False)
            async def fake_quit(why, _q=quits): _q.append(why)
            s._quit = fake_quit
            quits.clear()
            async with TestClient(TestServer(s.app)) as client:
                await client.post("/api/closing")
                if page_back:
                    s.ws_clients = 1                       # the reloaded page connected again
                await asyncio.sleep(2.8)
            self.assertEqual(len(quits), expect, f"page back: {page_back}")


class FakeLight:
    """A Govee light over LAN, as far as the studio can tell: commands change its state (unless
    packets are being lost), real-time mode off brings back the color it had before (or not),
    and it answers status requests unless it is silent."""

    def __init__(self, ip, on=1, brightness=50, color=(255, 0, 0), kelvin=0, keeps_color=True):
        self.ip, self.log = ip, []
        self.st = {"onOff": on, "brightness": brightness, "color": dict(zip("rgb", color)), "colorTemInKelvin": kelvin}
        self.keeps_color, self.pre, self.lose, self.silent = keeps_color, None, 0, False

    def _ok(self):
        if self.lose > 0:
            self.lose -= 1
            return False
        return True

    def turn(self, on):
        self.log.append(("turn", on))
        if self._ok():
            self.st["onOff"] = int(on)

    def brightness(self, v):
        self.log.append(("brightness", v))
        if self._ok():
            self.st["brightness"] = v

    def razer(self, on):
        self.log.append(("razer", on))
        if on and self.pre is None:
            self.pre = dict(self.st["color"])
        if not on and self.pre is not None:
            self.back_at = (self.st["onOff"], self.st["brightness"])     # how it looks the moment the show lets go
            self.st["color"] = self.pre if self.keeps_color else {"r": 10, "g": 200, "b": 30}
            self.pre = None

    def color(self, r, g, b):
        self.log.append(("color", (r, g, b)))
        if self._ok():
            self.st["color"] = {"r": r, "g": g, "b": b}

    def white(self, k):
        self.log.append(("white", k))

    def razer_leds(self, cols):
        pass


def fake_statuses(devices, timeout=1.0):
    return {d.ip: {**d.st, "color": dict(d.st["color"])} for d in devices if not d.silent}


class TestLightsHandedBack(unittest.TestCase):
    def setUp(self):
        # each light's state is remembered in config.json: here, in a config of the test's own
        from unittest import mock
        self.cfg = {}
        self.cfg_patches = [mock.patch("lightshow.studio.server.load_config", lambda: dict(self.cfg)),
                            mock.patch("lightshow.studio.server.save_config", lambda c: self.cfg.update(c))]
        for p in self.cfg_patches:
            p.start()

    def tearDown(self):
        for p in self.cfg_patches:
            p.stop()

    def output(self):
        from lightshow.studio.server import LightOutput
        return LightOutput(enabled=True)

    def patches(self):
        from unittest import mock
        return mock.patch("lightshow.govee_lan.statuses", side_effect=fake_statuses), mock.patch("time.sleep")

    def test_the_show_saves_each_light_and_gives_it_back(self):
        a = FakeLight("1.1.1.1", brightness=40, color=(255, 0, 0))                      # its red comes back by itself
        b = FakeLight("1.1.1.2", brightness=70, color=(0, 0, 255), keeps_color=False)   # its blue does not
        c = FakeLight("1.1.1.3", on=0, brightness=90, color=(0, 0, 0), kelvin=3000)
        o = self.output()
        o.devices = [a, b, c]
        p1, p2 = self.patches()
        with p1, p2:
            o.start()
            self.assertEqual(set(o.saved), {"1.1.1.1", "1.1.1.2", "1.1.1.3"})
            self.assertEqual(a.st["brightness"], 100)
            for L in (a, b, c):
                L.log.clear()
            o.stop()
        self.assertEqual(a.log[0], ("brightness", 40))                     # its brightness first, while the show has it...
        self.assertEqual(a.back_at, (1, 40))                               # ...so it comes back at 40 %, never 100 %
        self.assertEqual(b.back_at, (1, 70))
        self.assertEqual(c.back_at[0], 0)                                  # one that was off goes off before it is let go
        self.assertEqual(a.st["brightness"], 40)
        self.assertNotIn("color", [x[0] for x in a.log])                   # its color came back by itself
        self.assertIn(("color", (0, 0, 255)), b.log)
        self.assertEqual(b.st["brightness"], 70)
        self.assertEqual(c.st["onOff"], 0)                                 # it was off: off again
        self.assertEqual(o.saved, {})
        self.assertFalse(o.active)

    def test_unticking_a_light_gives_just_that_one_back(self):
        a, b = FakeLight("1.1.1.1", brightness=40), FakeLight("1.1.1.2", brightness=70, color=(0, 0, 255), keeps_color=False)
        c = FakeLight("1.1.1.3", on=0)
        o = self.output()
        p1, p2 = self.patches()
        with p1, p2:
            o.select(["1.1.1.1", "1.1.1.2"], [a, b, c])
            o.start()
            for L in (a, b, c):
                L.log.clear()
            dropped, added = o.select(["1.1.1.1"], [a, b, c])          # untick b mid-show
            self.assertEqual([d.ip for d in dropped], ["1.1.1.2"])
            o.reselect(dropped, added)
            self.assertEqual(b.log[0], ("brightness", 70))             # handed back like closing the studio: its brightness first
            self.assertEqual(b.back_at, (1, 70))
            self.assertEqual((b.st["brightness"], b.st["color"]), (70, {"r": 0, "g": 0, "b": 255}))
            self.assertEqual(a.log, [])                                # the other light keeps playing
            self.assertEqual(set(o.saved), {"1.1.1.1"})
            dropped, added = o.select(["1.1.1.1", "1.1.1.3"], [a, b, c])   # tick c mid-show: taken over
            o.reselect(dropped, added)
            self.assertIn(("razer", True), c.log)
            self.assertEqual(c.st["brightness"], 100)
            self.assertIn("1.1.1.3", o.saved)

    def test_rapid_ticking_still_ends_at_the_original_brightness(self):
        a, b = FakeLight("1.1.1.1", brightness=40), FakeLight("1.1.1.2", brightness=25)
        o = self.output()
        p1, p2 = self.patches()
        with p1, p2:
            o.select(["1.1.1.1", "1.1.1.2"], [a, b])
            o.start()
            for _ in range(4):                                         # click b off and on, fast
                o.reselect(*o.select(["1.1.1.1"], [a, b]))
                b.st["brightness"] = 100                               # the restore has not landed yet
                o.reselect(*o.select(["1.1.1.1", "1.1.1.2"], [a, b]))
                self.assertEqual(o.saved["1.1.1.2"]["brightness"], 25)  # still the original, not the show's 100
            o.reselect(*o.select(["1.1.1.1"], [a, b]))
        self.assertEqual(b.st["brightness"], 25)

    def test_a_lost_restore_is_sent_again(self):
        b = FakeLight("1.1.1.2", brightness=25)
        o = self.output()
        o.devices = [b]
        p1, p2 = self.patches()
        with p1, p2:
            o.start()
            b.lose = 4                                                 # the restore's power and brightness go missing
            o.stop()
        self.assertEqual((b.st["onOff"], b.st["brightness"]), (1, 25))

    def test_a_light_that_did_not_answer_at_first_is_asked_again(self):
        b = FakeLight("1.1.1.2", brightness=25)
        o = self.output()
        o.devices = [b]
        calls = []
        from unittest import mock
        def statuses(devices, timeout=1.0):
            calls.append(1)
            return {} if len(calls) == 1 else fake_statuses(devices)  # no reply the first time
        with mock.patch("lightshow.govee_lan.statuses", side_effect=statuses), mock.patch("time.sleep"):
            o.start()
            self.assertEqual(o.saved["1.1.1.2"]["brightness"], 25)
            o.stop()
        self.assertEqual(b.st["brightness"], 25)


class TestSavingShows(unittest.IsolatedAsyncioTestCase):
    """A test copy of the studio never writes a show; a show rebuilt by newer logic keeps the old one."""

    async def test_a_test_copy_never_saves_and_a_rebuild_keeps_a_backup(self):
        import json
        import os
        import tempfile
        from unittest import mock

        from aiohttp.test_utils import TestClient, TestServer

        from lightshow.studio import server
        d = tempfile.mkdtemp()
        with mock.patch.object(server, "SHOWS_DIR", d):
            os.makedirs(os.path.join(d, "Song"))
            path = os.path.join(d, "Song", "project.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"v": "old"}, fh)
            test_copy = server.Studio(port=0, lights=False, player=False)
            async with TestClient(TestServer(test_copy.app)) as client:
                r = await (await client.post("/api/projects/Song", json={"v": "new"})).json()
            self.assertTrue(r.get("read_only"))
            self.assertEqual(json.load(open(path, encoding="utf-8")), {"v": "old"})       # untouched
            real = server.Studio(port=0, lights=False, player=False)
            real.read_only = False                                                     # as the real studio
            async with TestClient(TestServer(real.app)) as client:
                await client.post("/api/projects/Song?backup=1", json={"v": "rebuilt"})
            self.assertEqual(json.load(open(path, encoding="utf-8")), {"v": "rebuilt"})
            self.assertEqual(json.load(open(os.path.join(d, "Song", "project.before-rebuild.json"), encoding="utf-8")), {"v": "old"})
