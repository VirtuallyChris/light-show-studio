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
        self.cfg = {"jam_last": {"speakers": "pc", "pc_device": "Headphones", "target_delay": 30.0}}
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
