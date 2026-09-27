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
