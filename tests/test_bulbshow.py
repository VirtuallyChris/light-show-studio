"""Bulb Show: effects, the looping stream, a running show, the cloud bulb's allowance, and the server.
Virtual speakers and bulbs only: nothing on the network is touched, nothing plays."""

import asyncio
import io
import os
import shutil
import tempfile
import time
import unittest
import wave

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from bulbshow import effects
from bulbshow.bulbs import CloudBulb, CloudError, CloudUsage, VirtualBulb, rgb_int
from bulbshow.runner import ShowRunner
from bulbshow.server import App
from bulbshow.sound import LoopReader, Sound
from bulbshow.speakers import FakeSpeakers


def show_of(*layers, duration=10.0):
    return {"duration": duration, "layers": [list(l) for l in layers]}


class EffectsTest(unittest.TestCase):
    def test_dark_where_nothing_plays(self):
        s = show_of([{"id": "a", "effect": "color", "start": 2, "end": 4, "color": "#ff0000", "level": 1}])
        self.assertEqual(effects.state(s, 1.0), (0, 0, 0))
        self.assertEqual(effects.state(s, 3.0), (255, 0, 0))
        self.assertEqual(effects.state(s, 4.0), (0, 0, 0))

    def test_brightness_folds_into_the_color(self):
        s = show_of([{"id": "a", "effect": "color", "start": 0, "end": 4, "color": "#ff8000", "level": 0.5}])
        self.assertEqual(effects.state(s, 1.0), (128, 64, 0))

    def test_fade_in_and_out(self):
        s = show_of([{"id": "a", "effect": "color", "start": 0, "end": 4, "color": "#ffffff", "level": 1, "fade": 1}])
        self.assertEqual(effects.state(s, 0.0), (0, 0, 0))
        self.assertEqual(effects.state(s, 0.5)[0], 128)
        self.assertEqual(effects.state(s, 2.0)[0], 255)
        self.assertLess(effects.state(s, 3.9)[0], 30)

    def test_pulse_breathes_between_low_and_high(self):
        c = {"id": "p", "effect": "pulse", "start": 0, "end": 8, "color": "#ffffff", "level": 1, "low": 0.2, "period": 2}
        s = show_of([c])
        self.assertEqual(effects.state(s, 0.0)[0], 51)          # low
        self.assertEqual(effects.state(s, 1.0)[0], 255)         # high, half a breath in
        self.assertEqual(effects.state(s, 2.0)[0], 51)
        # through the cloud: a square wave, peaks where the smooth one peaks
        self.assertEqual(effects.state(s, 0.4, cloud=True)[0], 51)
        self.assertEqual(effects.state(s, 1.2, cloud=True)[0], 255)

    def test_lightning_flashes_out_of_the_layer_below(self):
        glow = {"id": "g", "effect": "color", "start": 0, "end": 10, "color": "#200040", "level": 1}
        bolt = {"id": "b", "effect": "lightning", "start": 2, "end": 4, "color": "#ffffff", "level": 1, "flashes": 3}
        s = show_of([glow], [bolt])
        self.assertEqual(effects.state(s, 2.01), (255, 255, 255))   # the first flash starts the clip, full
        fl = effects.flashes(bolt)
        dark = next(t for t in np.arange(2.0, 4.0, 0.01)
                    if not any(f[0] <= t - 2 < f[0] + f[1] + effects.TAIL for f in fl))
        self.assertEqual(effects.state(s, dark), (32, 0, 64))       # between flashes: the glow below
        self.assertEqual(effects.flashes(bolt), fl)                  # the same flashes every time

    def test_cloud_flashes_last_long_enough(self):
        bolt = {"id": "b", "effect": "lightning", "start": 0, "end": 2, "color": "#ffffff", "level": 1, "flashes": 1}
        s = show_of([bolt])
        lan_lit = max(f[0] + f[1] for f in effects.flashes(bolt))
        self.assertLess(lan_lit, effects.CLOUD_FLASH)                 # on the LAN it is over sooner...
        for t in np.arange(0.0, effects.CLOUD_FLASH - 0.01, 0.01):     # ...through the cloud it stays lit 0.3 s
            self.assertEqual(effects.state(s, t, cloud=True), (255, 255, 255))
        self.assertEqual(effects.state(s, 1.0, cloud=True), (0, 0, 0))

    def test_render_is_one_loop(self):
        s = show_of([{"id": "a", "effect": "color", "start": 0, "end": 1, "color": "#00ff00", "level": 1}], duration=2.0)
        frames = effects.render(s)
        self.assertEqual(len(frames), 40)
        self.assertEqual(frames[0], "#00ff00")
        self.assertEqual(frames[-1], "#000000")

    def test_cloud_cost_counts_only_changes(self):
        on = show_of([{"id": "a", "effect": "color", "start": 0, "end": 10, "color": "#ff0000", "level": 1}])
        self.assertEqual(effects.cloud_requests(on), 0)               # one steady color: nothing to send while it loops
        blink = show_of([{"id": "a", "effect": "color", "start": 0, "end": 5, "color": "#ff0000", "level": 1}])
        self.assertEqual(effects.cloud_requests(blink), 2)            # on at the top, off halfway
        fast = show_of([{"id": "p", "effect": "pulse", "start": 0, "end": 10, "color": "#ffffff", "period": 0.2}])
        n = effects.cloud_requests(fast)
        self.assertLessEqual(n, 10 * effects.CLOUD_RATE + effects.CLOUD_BURST)   # never more than the allowance
        self.assertEqual(effects.per_hour(blink), 720)

    SCENE = {"name": "Breathe", "instance": "lightScene", "value": {"id": 3853, "paramId": 4280}}

    def test_a_scene_with_red_now_and_then(self):
        scene = {"id": "s", "effect": "scene", "start": 0, "end": 20, "scene": self.SCENE}
        red = {"id": "r", "effect": "color", "start": 8, "end": 9, "color": "#ff0000", "level": 1}
        s = show_of([scene], [red], duration=20.0)
        st = effects.state(s, 1.0, cloud=True)
        self.assertTrue(effects.is_scene(st))
        self.assertEqual(effects.code(st), "s:Breathe")
        self.assertEqual(effects.state(s, 8.5, cloud=True), (255, 0, 0))
        self.assertEqual(effects.state(s, 9.5, cloud=True), st)       # back to the scene
        self.assertEqual(effects.cloud_requests(s), 2)                 # red, then the scene again: once a loop
        self.assertEqual(effects.per_hour(s), 360)

    def test_lightning_over_a_scene_comes_back_to_it(self):
        scene = {"id": "s", "effect": "scene", "start": 0, "end": 10, "scene": self.SCENE}
        bolt = {"id": "b", "effect": "lightning", "start": 2, "end": 3, "color": "#ffffff", "level": 1, "flashes": 1}
        s = show_of([scene], [bolt])
        self.assertEqual(effects.state(s, 2.1, cloud=True), (255, 255, 255))
        self.assertTrue(effects.is_scene(effects.state(s, 2.9, cloud=True)))
        self.assertEqual(effects.state(s, 2.05), (255, 255, 255))      # a fading LAN flash replaces it outright

    def test_a_scene_not_picked_yet_is_dark(self):
        s = show_of([{"id": "s", "effect": "scene", "start": 0, "end": 10}])
        self.assertEqual(effects.state(s, 1.0), (0, 0, 0))

    def test_bucket(self):
        b = effects.Bucket(rate=2, burst=6, now=0)
        self.assertEqual(sum(b.take(0) for _ in range(10)), 6)
        self.assertFalse(b.take(0.2))
        self.assertTrue(b.take(0.5))
        self.assertAlmostEqual(b.wait(0.5), 0.5)


def sound_of(seconds=2.0, sr=1000):
    n = int(seconds * sr)
    pcm = np.stack([np.arange(n, dtype=np.int16), -np.arange(n, dtype=np.int16)], axis=1)
    return Sound(pcm, sr)


class LoopTest(unittest.TestCase):
    def test_reads_round_and_round(self):
        snd = sound_of(1.0)
        r = LoopReader(snd, start=0.75)
        data = np.frombuffer(r.read(500), dtype="<i2").reshape(-1, 2)
        self.assertEqual(list(data[:3, 0]), [750, 751, 752])
        self.assertEqual(data[249, 0], 999)
        self.assertEqual(data[250, 0], 0)                            # straight back to the start, no gap
        self.assertEqual(r.sent, 500)

    def test_volume_is_in_the_stream(self):
        snd = Sound(np.full((100, 1), 1000, dtype=np.int16), 100)
        r = LoopReader(snd, gain=0.5)
        self.assertEqual(np.frombuffer(r.read(10), dtype="<i2")[0], 500)

    def test_header_is_an_endless_wav(self):
        h = LoopReader(sound_of()).header()
        self.assertEqual(h[:4], b"RIFF")
        self.assertEqual(len(h), 44)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class RunnerTest(unittest.TestCase):
    def runner(self, **kw):
        show = {"name": "Witch", "speaker": "Office Home 2", "light_ms": kw.pop("light_ms", 0),
                **show_of([{"id": "a", "effect": "color", "start": 0, "end": 1, "color": "#ff0000", "level": 1}], duration=2.0)}
        return ShowRunner("witch", show, sound_of(2.0), FakeSpeakers(), VirtualBulb(), url_for=lambda s: f"http://x/{s}.wav",
                          log=lambda *_: None, **kw)

    def test_moment_allows_for_the_speaker_delay_and_wraps(self):
        r = self.runner(cast_offset_s=0.3)
        r.base = 0.0
        self.assertIsNone(r.moment(0.2))                              # not heard yet
        self.assertAlmostEqual(r.moment(0.8), 0.5)
        self.assertAlmostEqual(r.moment(2.8), 0.5)                    # the second time round
        r.base = 1.5
        self.assertAlmostEqual(r.moment(0.8), 0.0)

    def test_light_timing_moves_the_light_earlier(self):
        r = self.runner(cast_offset_s=0.0, light_ms=250)
        self.assertAlmostEqual(r.moment(1.0), 1.25)

    def test_runs_and_lights_the_bulb(self):
        r = self.runner(start_at=0.0)
        r.start()
        try:
            for _ in range(200):
                if r.state == "playing" and r.bulb.color == (255, 0, 0):
                    break
                time.sleep(0.01)
            self.assertEqual(r.state, "playing")
            self.assertEqual(r.bulb.color, (255, 0, 0))
            self.assertTrue(r.speakers.opened[0].url.endswith(f"{r.sid}.wav"))
        finally:
            r.stop(wait=True)
        self.assertEqual(r.state, "stopped")
        self.assertEqual(r.bulb.color, (0, 0, 0))

    def test_a_speaker_that_comes_back_gets_a_fresh_stream(self):
        r = self.runner()
        r.cast = r.speakers.open("Office Home 2")
        r._new_stream(0.0)
        first = r.sid
        reader = r.reader_for(first)
        self.assertIsNotNone(reader)
        reader.read(int(5 * r.sound.sr))                              # it has played a while
        r.loop_t = 1.25
        self.assertIsNone(r.reader_for(first))
        for _ in range(100):
            if r.sid != first:
                break
            time.sleep(0.01)
        self.assertNotEqual(r.sid, first)
        self.assertAlmostEqual(r.base, 1.25)
        self.assertIsNone(r.reader_for(first))                         # the old stream is gone for good

    def test_a_speaker_taken_over_stops_the_show(self):
        r = self.runner()
        r.TICK = 0.001
        clock = Clock()
        r.clock = clock
        r.start()
        for _ in range(200):
            if r.state == "playing":
                break
            time.sleep(0.01)
        r.speakers.opened[0].state = "IDLE"
        for _ in range(300):
            if not r.active:
                break
            clock.t += 4
            time.sleep(0.01)
        self.assertEqual(r.state, "error")
        self.assertIn("stopped playing", r.error)


class FakeClient:
    def __init__(self, usage=None, fail=None):
        self.calls, self.usage, self.fail = [], usage or CloudUsage(), fail

    def control(self, sku, device, ctype, instance, value):
        if self.fail:
            raise self.fail
        self.calls.append((instance, value))

    def state(self, sku, device):
        return {"powerSwitch": 1, "brightness": 40, "colorRgb": 0x00ff00}


class CloudBulbTest(unittest.TestCase):
    def test_sends_only_the_newest_color_and_restores_the_bulb(self):
        client = FakeClient()
        b = CloudBulb({"sku": "H6008", "id": "AA"}, client)
        b.begin()
        self.assertEqual(client.calls[:2], [("powerSwitch", 1), ("brightness", 100)])
        for v in range(50):                         # far more than the allowance, at once
            b.apply((v, 0, 0))
        time.sleep(1.2)
        colors = [v for k, v in client.calls if k == "colorRgb"]
        self.assertLessEqual(len(colors), 4)        # the burst was spent on starting up: about 2 a second
        self.assertEqual(colors[-1], rgb_int((49, 0, 0)))
        b.end()
        self.assertEqual(client.calls[-2:], [("colorRgb", 0x00ff00), ("brightness", 40)])

    def test_a_scene_goes_out_as_govee_gave_it(self):
        client = FakeClient()
        b = CloudBulb({"sku": "H6008", "id": "AA"}, client)
        b.begin()
        time.sleep(0.6)                                # a token for the next command
        b.apply(effects.scene_state(EffectsTest.SCENE))
        for _ in range(100):
            if b.sent:
                break
            time.sleep(0.02)
        b.end()
        self.assertIn(("lightScene", {"id": 3853, "paramId": 4280}), client.calls)

    def test_scene_list(self):
        from bulbshow.bulbs import CloudClient
        c = CloudClient("key")
        answers = {
            "/router/api/v1/device/scenes": {"payload": {"capabilities": [{"type": "devices.capabilities.dynamic_scene", "instance": "lightScene",
                "parameters": {"dataType": "ENUM", "options": [{"name": "Breathe", "value": {"id": 1, "paramId": 2}}]}}]}},
            "/router/api/v1/device/diy-scenes": {"payload": {"capabilities": [{"instance": "diyScene",
                "parameters": {"options": [{"name": "Witch", "value": 77}]}}]}},
        }
        c._request = lambda method, path, body=None: answers[path]
        self.assertEqual(c.scenes("H6008", "AA"), [{"name": "Breathe", "instance": "lightScene", "value": {"id": 1, "paramId": 2}},
                                                   {"name": "Witch", "instance": "diyScene", "value": 77}])

    def test_daily_limit_stops_sending(self):
        usage = CloudUsage()
        client = FakeClient(usage)
        b = CloudBulb({"sku": "H6008", "id": "AA"}, client)
        b.begin()
        usage.exhausted = True
        client.fail = CloudError("Govee's daily limit is used up")
        b.apply((1, 2, 3))
        b.thread.join(timeout=3)
        self.assertFalse(b.thread.is_alive())
        self.assertIn("daily limit", b.error)
        b.end()                                    # and spends nothing on putting the bulb back

    def test_usage_resets_each_day(self):
        saved = {}
        u = CloudUsage(load=lambda: {"day": "2000-01-01", "count": 9000}, save=saved.update)
        self.assertEqual(u.snapshot()["count"], 0)
        u.add(3)
        u.save()
        self.assertEqual(saved["count"], 3)


def wav_bytes(seconds=1.0, sr=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.zeros(int(seconds * sr), dtype="<i2").tobytes())
    return buf.getvalue()


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = App(port=0, shows_dir=os.path.join(self.dir, "shows"), config_path=os.path.join(self.dir, "config.json"),
                       lights=False, cast=False)
        self.app.cast_offset = lambda speaker: 0.0
        self.client = TestClient(TestServer(self.app.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        for r in self.app.runners.values():
            r.stop(wait=True)
        await self.client.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def make(self, name, speaker):
        s = await (await self.client.post("/api/shows", json={"name": name})).json()
        r = await self.client.post(f"/api/shows/{s['id']}/sound?name=laugh.wav", data=wav_bytes(2.0))
        self.assertEqual((await r.json())["duration"], 2.0)
        show = await (await self.client.get(f"/api/shows/{s['id']}")).json()
        show.update(speaker=speaker, bulb={"id": f"virtual-{name}", "via": "virtual", "sku": "H6008"},
                    layers=[[{"id": "a", "effect": "color", "start": 0, "end": 1, "color": "#ff0000", "level": 1}]])
        await self.client.post(f"/api/shows/{s['id']}", json=show)
        return s["id"]

    async def test_shows_are_made_saved_and_listed(self):
        sid = await self.make("Witch laugh", "Office Home 2")
        self.assertEqual(sid, "witch-laugh")
        shows = await (await self.client.get("/api/shows")).json()
        self.assertEqual(shows[0]["name"], "Witch laugh")
        self.assertTrue(shows[0]["ready"])
        dup = await (await self.client.post("/api/shows", json={"name": "Witch laugh"})).json()
        self.assertEqual(dup["id"], "witch-laugh-2")

    async def test_render(self):
        sid = await self.make("Witch", "Office Home 2")
        show = await (await self.client.get(f"/api/shows/{sid}")).json()
        res = await (await self.client.post("/api/render", json=show)).json()
        self.assertEqual(len(res["frames"]), 40)
        self.assertIsNone(res["per_hour"])
        show["bulb"]["via"] = "cloud"
        res = await (await self.client.post("/api/render", json=show)).json()
        self.assertEqual(res["per_hour"], 3600)

    async def test_two_shows_at_once_but_never_on_one_speaker(self):
        a = await self.make("Witch", "Office Home 2")
        b = await self.make("Ghost", "Kitchen Home")
        c = await self.make("Bats", "Office Home 2")
        for sid in (a, b):
            res = await (await self.client.post(f"/api/run/{sid}", json={})).json()
            self.assertTrue(res["ok"], res)
        res = await (await self.client.post(f"/api/run/{c}", json={})).json()
        self.assertFalse(res["ok"])
        self.assertIn("Office Home 2 is already playing", res["error"])
        for _ in range(200):
            running = (await (await self.client.get("/api/running")).json())["shows"]
            if all(running.get(s, {}).get("state") == "playing" for s in (a, b)):
                break
            await asyncio.sleep(0.02)
        self.assertEqual(running[a]["color"], "#ff0000")
        self.assertEqual(running[b]["state"], "playing")
        # the speaker's stream: an endless WAV that starts at once
        runner = self.app.runners[a]
        resp = await self.client.get(f"/api/loop/{runner.sid}.wav")
        head = await resp.content.read(44)
        self.assertEqual(head[:4], b"RIFF")
        resp.close()
        await self.client.post("/api/stop-all")
        for r in self.app.runners.values():
            await asyncio.get_running_loop().run_in_executor(None, r.stop, True)
        self.assertFalse(self.app.any_running())

    async def test_scenes_of_a_virtual_bulb(self):
        res = await (await self.client.get("/api/scenes?via=virtual&id=virtual-1")).json()
        self.assertEqual(res["scenes"][0]["name"], "Breathe")

    async def test_a_show_without_a_sound_cannot_run(self):
        s = await (await self.client.post("/api/shows", json={"name": "Empty"})).json()
        res = await (await self.client.post(f"/api/run/{s['id']}", json={})).json()
        self.assertEqual(res["error"], "it has no sound yet")

    async def test_the_api_key_is_kept_but_never_shown(self):
        st = await (await self.client.post("/api/settings", json={"govee_api_key": " abc "})).json()
        self.assertTrue(st["has_key"])
        self.assertNotIn("abc", str(st))
        self.assertEqual(self.app.client.key, "abc")


class TaskbarTest(unittest.TestCase):
    def test_a_pinned_bulb_show_starts_bulb_show_with_its_own_icon(self):
        import re
        from bulbshow.server import window_identity
        idn = window_identity()
        files = re.findall(r'"([^"]+)"', idn["relaunch"])
        self.assertTrue(files[0].lower().endswith("wscript.exe"))
        self.assertTrue(os.path.isfile(files[1]) and files[1].endswith("Bulb Show.vbs"), files)
        self.assertTrue(os.path.isfile(idn["icon"].rsplit(",", 1)[0]))
        self.assertNotEqual(idn["app_id"], "LightShowStudio.Studio")   # its own button, apart from the studio's
        self.assertEqual(idn["title"], "")                              # every window of its own profile


if __name__ == "__main__":
    unittest.main()
