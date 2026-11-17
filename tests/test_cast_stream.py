"""The Cast speaker's stream: run with  .venv\\Scripts\\python -m unittest tests.test_cast_stream -v

Nothing is played: the speaker is stood in for, and the stream is read in memory.
"""

import unittest

import numpy as np

from lightshow.studio.relay import Relay


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestReserve(unittest.TestCase):
    def test_a_speaker_may_run_well_ahead_and_is_never_padded_with_silence(self):
        clock = Clock()
        relay = Relay(sr=1000, channels=2, capacity_s=600, clock=clock)
        relay.write(np.ones((30_000, 2), dtype=np.int16))           # 30 s of audio waiting
        s = relay.open_stream(0.0)
        got = 0
        while True:                                                 # a speaker pulling as fast as it can
            b = s.read(4096, timeout=0)
            if not b:
                break
            got += len(b) // 4
        self.assertAlmostEqual(got / 1000, s.BURST_S, delta=0.1)    # 20 s ahead, not 4
        # a stream that has caught the capture head while ahead of real time waits, rather than
        # sending silence (which would push the room's timeline out of step)
        clock.t = 5.0
        relay2 = Relay(sr=1000, channels=2, capacity_s=600, clock=clock)
        relay2.write(np.ones((2_000, 2), dtype=np.int16))
        s2 = relay2.open_stream(0.0)
        self.assertEqual(len(s2.read(4096, timeout=0)) // 4, 2000)
        self.assertIsNone(s2.read(4096, timeout=0))
        self.assertEqual(s2.segments, [[0, 0, 2000]])              # no silence was noted
        clock.t = 8.0                                               # now it is due and there is nothing: fed silence
        self.assertTrue(s2.read(4096, timeout=0))
        self.assertEqual(s2.segments[-1][1], -1)


class TestReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_a_speaker_that_comes_back_gets_a_clean_restart(self):
        import threading
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from lightshow.studio.party import CastOutput
        from lightshow.studio.party_service import PartyService

        loads, playing = [], {"position": 0.0}

        class Cast:
            name = "Office Home 2"
            def play_stream(self, url, ctype, title): loads.append((url, ctype))
            def status(self): return dict(playing)
            def stop(self): pass

        class Studio:
            port = 0
            class cast:            # noqa: N801
                cast, name, media_path = None, "", ""

        relay = Relay(sr=1000, channels=2, capacity_s=600)
        relay.write(np.ones((60_000, 2), dtype=np.int16))

        class Engine:
            def __init__(self):
                self.lock, self.relay, self.log = threading.RLock(), relay, []
                self.gate = type("G", (), {"paused": False})()
                self.cfg = type("C", (), {"target_delay": 45.0})()
                self.output = CastOutput(Cast(), lambda sid: f"http://x/api/relay/stream.wav?sid={sid}", )
                self.output.start_stream(relay.open_stream(10.0))
            def room_capture_time(self): return 12.0
            def say(self, m): self.log.append(m)

        svc = PartyService(Studio, ".", lambda: {}, lambda c: None, lambda: "127.0.0.1")
        svc.engine = eng = Engine()
        app = web.Application()
        app.add_routes(svc.routes())
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/api/relay/stream.wav?sid=1")        # the speaker's first request: a short probe
            await r.content.read(4 * 1000)
            r.close()
            r = await client.get("/api/relay/stream.wav?sid=1")        # it asks again before playing: no restart
            self.assertEqual(r.status, 200)
            await r.content.read(4 * 5000)
            r.close()
            playing["position"] = 42.0                               # it played for a while...
            r = await client.get("/api/relay/stream.wav?sid=1")        # ...then the connection broke and it came back
            self.assertEqual(r.status, 410)
            for _ in range(50):
                if eng.output.sid == 2:
                    break
                import asyncio
                await asyncio.sleep(0.02)
        self.assertEqual(eng.output.sid, 2)                           # restarted, as a new stream
        self.assertEqual(eng.output.stream.cursor, 12_000)            # at the point the room had reached
        self.assertTrue(any("came back" in m for m in eng.log))


if __name__ == "__main__":
    unittest.main()


class TestSpeakerVolumes(unittest.TestCase):
    """The phone remote's settings: every speaker's volume, read and set (stand-in speakers: nothing plays)."""

    def test_volumes_are_read_and_set_per_speaker(self):
        from types import SimpleNamespace
        from lightshow.studio.cast import CastManager

        class Speaker:
            def __init__(self, name, kind, vol):
                self.cast_info = SimpleNamespace(friendly_name=name, cast_type=kind, model_name="Google Home")
                self.status = SimpleNamespace(volume_level=vol)
                self.socket_client = SimpleNamespace(is_connected=True)
                self.set_to = None

            def wait(self, timeout=None):
                pass

            def set_volume(self, v):
                self.set_to = v
        m = CastManager(None)
        kitchen, group = Speaker("Kitchen Home", "audio", 0.4), Speaker("Everywhere", "group", 0.38)
        m.casts = [kitchen, group]
        vols = m.device_volumes()
        self.assertEqual([v["name"] for v in vols], ["Everywhere", "Kitchen Home"])        # groups first
        self.assertEqual(vols[1]["volume"], 0.4)
        m.set_device_volume("Kitchen Home", 1.7)
        self.assertEqual(kitchen.set_to, 1.0)                                              # clamped
        self.assertIsNone(group.set_to)
        with self.assertRaises(ValueError):
            m.set_device_volume("Nowhere", 0.5)


class TestLookingForSpeakersAgain(unittest.TestCase):
    """The Party screen looks for speakers every 20 s. A look must never cut the speakers' connections: it used to
    stop the last discovery, which closed what every speaker reconnects through (the party's speaker could not get
    back, and a new connection to every speaker was opened each time). Nothing here reaches the network."""

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock
        from lightshow.studio import cast as cast_mod
        self.ns, self.mock = SimpleNamespace, mock
        self.m = cast_mod.CastManager(None)
        self.made = []

    def cast(self, uuid):
        info = self.ns(friendly_name=uuid, model_name="", cast_type="audio", host="")
        c = self.ns(uuid=uuid, cast_info=info, socket_client=self.ns(stop=__import__("threading").Event()), let_go=False)
        c.disconnect = lambda timeout=None: setattr(c, "let_go", True)
        return c

    def make(self, info, zc):
        c = self.cast(info)
        self.made.append(info)
        return c

    def test_a_second_look_keeps_the_discovery_and_every_connection(self):
        import pychromecast.discovery as disc
        browser = self.mock.Mock(devices={"a": "a", "b": "b"})
        with self.mock.patch.object(disc, "CastBrowser", return_value=browser) as made, \
                self.mock.patch("zeroconf.Zeroconf"), \
                self.mock.patch("pychromecast.get_chromecast_from_cast_info", side_effect=self.make), \
                self.mock.patch("lightshow.studio.cast.time.sleep"):
            self.m.discover(0.1)
            first = list(self.m.casts)
            self.m.discover(0.1)
        made.assert_called_once()                                   # one discovery, for the studio's whole life
        browser.stop_discovery.assert_not_called()
        self.assertEqual([id(c) for c in self.m.casts], [id(c) for c in first])   # the same connections
        self.assertEqual(self.made, ["a", "b"])

    def test_new_speakers_join_gone_ones_are_let_go_but_not_the_one_playing(self):
        a, b, c = self.cast("a"), self.cast("b"), self.cast("c")
        self.m.casts, self.m.cast = [a, b, c], c
        out = self.m._reconcile({"a": "a", "d": "d"}, self.make)
        self.assertEqual([x.uuid for x in out], ["a", "d"])
        self.assertIs(out[0], a)
        self.assertTrue(b.let_go)
        self.assertFalse(c.let_go)                                   # cast to right now: left alone

    def test_a_speaker_let_go_is_made_again(self):
        a = self.cast("a")
        a.socket_client.stop.set()                                   # disconnect(): its connection cannot start again
        self.m.casts = [a]
        out = self.m._reconcile({"a": "a"}, self.make)
        self.assertIsNot(out[0], a)
