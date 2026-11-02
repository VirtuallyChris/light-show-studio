"""Philips Hue (lightshow/hue.py) against a fake bridge: a local HTTP server and captured UDP, never a
real bridge or real lights."""

import copy
import json
import os
import socket
import struct
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from lightshow import hue
from lightshow.dtls_psk import prf

AREA = "1a8d99cc-967b-44f2-9202-43f976c0fa6e"
BULB_DEV, STRIP_DEV, LAMP_DEV = "dev-bulb-0000", "dev-strip-0000", "dev-lamp-0000"
BULB_ENT, STRIP_ENT = "ent-bulb-0000", "ent-strip-0000"
BULB_LIGHT, STRIP_LIGHT, LAMP_LIGHT = "light-bulb-00", "light-strip-0", "light-lamp-00"


def resources(area: bool = True) -> dict:
    """A bridge with a bulb and a three-channel gradient strip in one Entertainment area, and a lamp in none."""
    def dev(i, name, model, light, ent=None):
        services = [{"rid": light, "rtype": "light"}] + ([{"rid": ent, "rtype": "entertainment"}] if ent else [])
        return {"id": i, "type": "device", "metadata": {"name": name}, "product_data": {"model_id": model}, "services": services}

    def light(i, owner, name, x=0.3, y=0.3, bri=80.0, on=True):
        return {"id": i, "type": "light", "owner": {"rid": owner, "rtype": "device"}, "metadata": {"name": name},
                "on": {"on": on}, "dimming": {"brightness": bri}, "color": {"xy": {"x": x, "y": y}},
                "color_temperature": {"mirek": None, "mirek_valid": False}}

    res = {
        "device": [dev(BULB_DEV, "Hall bulb", "LCA001", BULB_LIGHT, BULB_ENT),
                   dev(STRIP_DEV, "TV strip", "LCX004", STRIP_LIGHT, STRIP_ENT),
                   dev(LAMP_DEV, "Desk lamp", "LTW001", LAMP_LIGHT)],
        "light": [light(BULB_LIGHT, BULB_DEV, "Hall bulb"), light(STRIP_LIGHT, STRIP_DEV, "TV strip"),
                  light(LAMP_LIGHT, LAMP_DEV, "Desk lamp", x=0.45, y=0.41, bri=40.0)],
        "entertainment": [{"id": BULB_ENT, "owner": {"rid": BULB_DEV, "rtype": "device"}, "renderer": True},
                          {"id": STRIP_ENT, "owner": {"rid": STRIP_DEV, "rtype": "device"}, "renderer": True}],
        "zigbee_connectivity": [{"id": "zb1", "owner": {"rid": STRIP_DEV, "rtype": "device"}, "mac_address": "00:17:88:01:0b:aa:bb:cc-0b"}],
        "entertainment_configuration": [],
    }
    if area:
        member = lambda ent, idx: {"service": {"rid": ent, "rtype": "entertainment"}, "index": idx}
        res["entertainment_configuration"] = [{
            "id": AREA, "type": "entertainment_configuration", "metadata": {"name": "Living room"},
            "status": "inactive", "light_services": [{"rid": BULB_LIGHT}, {"rid": STRIP_LIGHT}],
            "channels": [{"channel_id": 0, "members": [member(BULB_ENT, 0)]},
                         # the strip's channels out of order: its segments follow the member index
                         {"channel_id": 1, "members": [member(STRIP_ENT, 2)]},
                         {"channel_id": 2, "members": [member(STRIP_ENT, 0)]},
                         {"channel_id": 3, "members": [member(STRIP_ENT, 1)]}]}]
    return res


class FakeBridge:
    """The bridge's HTTP side: pairing (button pressed after `button_after` asks), CLIP v2, and every PUT."""

    def __init__(self, res: dict, button_after: int = 0):
        self.res, self.button_after = res, button_after
        self.asks, self.puts, self.keys = 0, [], []
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, doc, code=200):
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")

            def do_POST(self):
                body = self._body()
                if self.path == "/api":
                    bridge.asks += 1
                    if bridge.asks <= bridge.button_after:
                        return self._send([{"error": {"type": 101, "address": "", "description": "link button not pressed"}}])
                    assert body == {"devicetype": "light_show_studio#pc", "generateclientkey": True}, body
                    return self._send([{"success": {"username": "user-abc", "clientkey": "00112233445566778899AABBCCDDEEFF"}}])
                self._send({}, 404)

            def do_GET(self):
                bridge.keys.append(self.headers.get("hue-application-key"))
                if self.path == "/api/config":
                    return self._send({"bridgeid": "ECB5FAFFFE0A1B2C", "name": "Hue Bridge"})
                parts = self.path.split("/")
                if self.path.startswith("/clip/v2/resource/"):
                    if self.headers.get("hue-application-key") != "user-abc":
                        return self._send({"errors": [{"description": "unauthorized user"}], "data": []}, 403)
                    kind = parts[4]
                    data = bridge.res.get(kind, [])
                    if len(parts) > 5:
                        data = [d for d in data if d["id"] == parts[5]]
                    return self._send({"errors": [], "data": data})
                self._send({}, 404)

            def do_PUT(self):
                body = self._body()
                bridge.puts.append((self.path, body))
                parts = self.path.split("/")
                if parts[4] == "light":                    # the fake light takes what it is told
                    for L in bridge.res["light"]:
                        if L["id"] == parts[5]:
                            for k, v in body.items():
                                if k != "dynamics":
                                    L[k] = {**L.get(k, {}), **v}
                self._send({"errors": [], "data": [{"rid": parts[5]}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def api(self, ip, username=""):
        return hue.BridgeApi(ip, username, base=self.base)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CapturedUdp:
    """The stream's transport in a test: plain UDP to a local socket (no DTLS), read back by the test."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2.0)
        self.opened = []

    def __call__(self, ip, username, clientkey):
        self.opened.append((ip, username, clientkey))
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        addr = self.sock.getsockname()
        return type("Conn", (), {"send": lambda s, data: out.sendto(data, addr), "close": lambda s: out.close()})()

    def packets(self, n):
        return [self.sock.recv(2048) for _ in range(n)]

    def close(self):
        self.sock.close()


def memory_config(start: dict | None = None):
    box = {"cfg": copy.deepcopy(start or {})}
    return (lambda: copy.deepcopy(box["cfg"])), (lambda cfg: box.__setitem__("cfg", copy.deepcopy(cfg))), box


PAIRED = {"hue": {"bridges": {"ECB5FAFFFE0A1B2C": {"ip": "127.0.0.1", "name": "Hue Bridge", "username": "user-abc",
                                                    "clientkey": "00112233445566778899AABBCCDDEEFF"}}}}


def no_stream(ip, username, clientkey):
    raise OSError("this test streams nothing")


def manager(bridge, cfg=None, transport=None, found=True, config=None):
    load, save, box = config or memory_config(cfg)
    m = hue.HueManager(load, save, discover=lambda: [{"id": "ECB5FAFFFE0A1B2C", "ip": "127.0.0.1", "name": "Hue Bridge"}] if found else [],
                       api=bridge.api, transport=transport or no_stream)
    m.PAIR_POLL_S = 0.05
    return m, box


class TestDiscovery(unittest.TestCase):
    def test_mdns_answer(self):
        b = hue.parse_mdns("Hue Bridge - 0A1B2C._hue._tcp.local.", ["fe80::1", "192.168.1.40"],
                           {b"bridgeid": b"ecb5fafffe0a1b2c", b"modelid": b"BSB002"})
        self.assertEqual(b, {"id": "ECB5FAFFFE0A1B2C", "ip": "192.168.1.40", "name": "Hue Bridge - 0A1B2C"})
        self.assertIsNone(hue.parse_mdns("x._hue._tcp.local.", ["fe80::1"], {}))

    def test_ssdp_answers(self):
        hue_reply = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=100\r\nLOCATION: http://192.168.1.40:80/description.xml\r\n"
                     "SERVER: Hue/1.0 UPnP/1.0 IpBridge/1.60.0\r\nhue-bridgeid: ECB5FAFFFE0A1B2C\r\n"
                     "ST: urn:schemas-upnp-org:device:basic:1\r\n\r\n")
        self.assertEqual(hue.parse_ssdp(hue_reply, "192.168.1.40"), {"id": "ECB5FAFFFE0A1B2C", "ip": "192.168.1.40", "name": ""})
        cast_reply = "HTTP/1.1 200 OK\r\nSERVER: Linux/3.8 UPnP/1.0 GUPnP/0.20\r\nST: upnp:rootdevice\r\n\r\n"
        self.assertIsNone(hue.parse_ssdp(cast_reply, "192.168.1.50"))

    def test_ssdp_when_mdns_finds_none_or_fails(self):
        one = [{"id": "X", "ip": "192.168.1.40", "name": ""}]
        with mock.patch.object(hue, "discover_mdns", return_value=[]), mock.patch.object(hue, "discover_ssdp", return_value=one):
            self.assertEqual(hue.discover(), one)
        with mock.patch.object(hue, "discover_mdns", side_effect=OSError("no multicast")), \
                mock.patch.object(hue, "discover_ssdp", return_value=one), mock.patch.object(hue, "log"):
            self.assertEqual(hue.discover(), one)
        with mock.patch.object(hue, "discover_mdns", return_value=one), mock.patch.object(hue, "discover_ssdp") as ssdp:
            self.assertEqual(hue.discover(), one)
            ssdp.assert_not_called()

    def test_keys(self):
        self.assertTrue(hue.is_key("192.168.1.40#2"))
        self.assertFalse(hue.is_key("192.168.1.178"))
        self.assertEqual(hue.address("192.168.1.40#2"), "192.168.1.40")
        ips = ["192.168.1.40#10", "192.168.1.178", "192.168.1.40#2", "192.168.1.40#pair", "192.168.1.9"]
        self.assertEqual(sorted(ips, key=hue.sort_key),
                         ["192.168.1.9", "192.168.1.40#2", "192.168.1.40#10", "192.168.1.40#pair", "192.168.1.178"])
        pinged = []
        hue.via_bridge(lambda ip, ms: pinged.append(ip))("192.168.1.40#3", 300)
        self.assertEqual(pinged, ["192.168.1.40"])


class TestPairing(unittest.TestCase):
    def test_button_not_pressed_then_paired(self):
        bridge = FakeBridge(resources(), button_after=3)
        try:
            self.assertEqual(hue.pair_once(bridge.api("127.0.0.1")), ("button", {}))
            m, box = manager(bridge)
            m.refresh()                                       # found: pairing starts by itself
            self.assertEqual(m.extras()[0]["name"], "Hue Bridge: press its button")
            deadline = time.monotonic() + 5
            while m.pair_state("ECB5FAFFFE0A1B2C")["state"] != "paired" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(m.pair_state("ECB5FAFFFE0A1B2C"), {"state": "paired"})
            saved = box["cfg"]["hue"]["bridges"]["ECB5FAFFFE0A1B2C"]
            self.assertEqual((saved["username"], saved["clientkey"], saved["ip"]),
                             ("user-abc", "00112233445566778899AABBCCDDEEFF", "127.0.0.1"))
            while not m.devices() and time.monotonic() < deadline:   # paired: its lights are read at once
                time.sleep(0.05)
            self.assertEqual(len(m.devices()), 2)
            self.assertFalse([r for r in m.extras() if r["hue"] == "pair"])
        finally:
            bridge.close()

    def test_nobody_presses_the_button(self):
        bridge = FakeBridge(resources(), button_after=10 ** 6)
        try:
            m, box = manager(bridge)
            with mock.patch.object(hue, "PAIR_S", 0.3):
                m.refresh()
                time.sleep(0.6)
            self.assertEqual(m.pair_state("ECB5FAFFFE0A1B2C")["state"], "timeout")
            self.assertNotIn("hue", box["cfg"])
            self.assertEqual(m.pair("nope"), {"state": "unknown"})
        finally:
            bridge.close()

    def test_scan_off_in_config(self):
        bridge = FakeBridge(resources())
        try:
            m, _ = manager(bridge, {"hue": {"scan": False}})
            m.refresh(force=True)
            self.assertEqual(m.extras(), [])
            self.assertEqual(bridge.asks, 0)
        finally:
            bridge.close()


class TestLights(unittest.TestCase):
    def test_area_channels_become_lights(self):
        got = hue.parse_lights("192.168.1.40", resources())
        self.assertEqual(got["area"], {"id": AREA, "name": "Living room"})
        bulb, strip = got["lights"]
        self.assertEqual((bulb["key"], bulb["name"], bulb["channels"], bulb["mode"]), ("192.168.1.40#0", "Hall bulb", [0], "stream"))
        # one light, three segments, in the strip's own order (member index), not channel order
        self.assertEqual((strip["key"], strip["name"], strip["channels"], strip["model"]), ("192.168.1.40#1", "TV strip", [2, 3, 1], "LCX004"))
        self.assertEqual(strip["mac"], "00:17:88:01:0B:AA:BB:CC-0B")
        self.assertEqual(strip["device_id"], "hue:" + STRIP_DEV)
        self.assertEqual([(o["name"], o["light_id"]) for o in got["outside"]], [("Desk lamp", LAMP_LIGHT)])

    def test_no_area_means_every_light_over_rest(self):
        got = hue.parse_lights("192.168.1.40", resources(area=False))
        self.assertIsNone(got["area"])
        self.assertEqual(got["outside"], [])
        self.assertEqual(sorted(L["name"] for L in got["lights"]), ["Desk lamp", "Hall bulb", "TV strip"])
        self.assertEqual({L["mode"] for L in got["lights"]}, {"rest"})

    def test_listing_from_the_fake_bridge(self):
        bridge = FakeBridge(resources())
        try:
            m, _ = manager(bridge, PAIRED)
            m.refresh()
            self.assertEqual([(d.ip, d.name, d.segments, d.sku) for d in m.devices()],
                             [("127.0.0.1#0", "Hall bulb", 1, "LCA001"), ("127.0.0.1#1", "TV strip", 3, "LCX004")])
            self.assertIn("user-abc", bridge.keys)
            rows = m.extras()
            self.assertEqual([(r["name"], r["hue"], r["on"], r["online"]) for r in rows], [("Desk lamp", "outside", False, False)])
            self.assertIn("Entertainment area", rows[0]["hint"])
            self.assertIn("Living room", rows[0]["hint"])
        finally:
            bridge.close()


class TestStreaming(unittest.TestCase):
    def test_packet_bytes(self):
        pkt = hue.stream_packet(AREA, 7, [(0, 255, 0, 0), (3, 1, 128, 255)])
        self.assertEqual(pkt[:9], b"HueStream")
        self.assertEqual(pkt[9:16], bytes([2, 0, 7, 0, 0, 0, 0]))           # version 2.0, sequence, reserved, RGB, reserved
        self.assertEqual(pkt[16:52], AREA.encode())
        self.assertEqual(pkt[52:], bytes.fromhex("00" "ffff" "0000" "0000") + bytes.fromhex("03" "0101" "8080" "ffff"))
        self.assertEqual(len(pkt), 52 + 2 * 7)

    def test_a_show_through_the_light_output(self):
        from lightshow.studio import server
        bridge, udp = FakeBridge(resources()), CapturedUdp()
        try:
            m, _ = manager(bridge, PAIRED, transport=udp)
            m.refresh()
            bulb, strip = m.devices()
            with mock.patch.object(server, "load_config", lambda: {}):
                o = server.LightOutput(enabled=False)                     # (frames still go out; nothing else does)
            # a Hue row that is not a light is never made into one
            o.select([bulb.ip, strip.ip, "127.0.0.1#pair"], [bulb, strip])
            self.assertEqual(o.devices, [bulb, strip])
            o.active = True
            for d in o.devices:
                d.razer(True)
                d.razer(True)                                            # (asked three times by the output: one stream)
            o.frame([[255, 0, 0], [0, 0, 255, 0, 255, 0, 255, 255, 255]], [bulb.ip, strip.ip])
            deadline = time.monotonic() + 3
            want = {0: (65535, 0, 0), 2: (0, 0, 65535), 3: (0, 65535, 0), 1: (65535, 65535, 65535)}
            got = {}
            while got != want and time.monotonic() < deadline:
                pkt = udp.sock.recv(2048)
                self.assertEqual(pkt[:11], b"HueStream\x02\x00")
                self.assertEqual(pkt[16:52], AREA.encode())
                got = {ch: (r, g, b) for ch, r, g, b in struct.iter_unpack(">BHHH", pkt[52:])}
            self.assertEqual(got, want)
            self.assertEqual(udp.opened, [("127.0.0.1", "user-abc", "00112233445566778899AABBCCDDEEFF")])
            self.assertEqual(bridge.puts[0], (f"/clip/v2/resource/entertainment_configuration/{AREA}", {"action": "start"}))
            bulb.razer(False)                                            # one light let go: the area plays on, it is dark
            self.assertTrue(strip.stream.wanted)
            strip.razer(False)                                           # the last: the area stops, the Hue app has them back
            self.assertEqual(bridge.puts[-1], (f"/clip/v2/resource/entertainment_configuration/{AREA}", {"action": "stop"}))
            self.assertIsNone(strip.stream.thread)
            self.assertEqual(len([p for p in bridge.puts if p[1] == {"action": "start"}]), 1)
        finally:
            bridge.close()
            udp.close()

    def test_an_idle_area_stops_and_colors_start_it_again(self):
        bridge, udp = FakeBridge(resources()), CapturedUdp()
        try:
            m, _ = manager(bridge, PAIRED, transport=udp)
            m.refresh()
            bulb = m.devices()[0]
            starts = lambda: [p[1]["action"] for p in bridge.puts if "action" in p[1]]
            with mock.patch.object(hue, "IDLE_S", 0.3):
                bulb.razer(True)
                deadline = time.monotonic() + 3
                while bulb.stream.thread is not None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(starts(), ["start", "stop"])          # no colors: given back, like a Govee light
                bulb.razer_leds([(9, 9, 9)])
                while starts() != ["start", "stop", "start"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(starts(), ["start", "stop", "start"])
                bulb.razer(False)
            self.assertEqual(starts(), ["start", "stop", "start", "stop"])
        finally:
            bridge.close()
            udp.close()

    def test_a_show_with_govee_lights_too_gives_hue_back_at_the_end(self):
        """The output restores the lights it saved a state for (Govee ones); a Hue light has none, and still goes back."""
        from lightshow.studio import server
        stream = mock.Mock(wanted=set())
        light = hue.HueDevice(ip="192.168.1.40#0", stream=stream)
        with mock.patch.object(server, "load_config", lambda: {}):
            o = server.LightOutput(enabled=True)
        o.devices, o.active, o.saved = [light], True, {"192.168.1.178": {"onOff": 1}}
        with mock.patch.object(o, "restore"):
            o.stop()
        stream.enable.assert_called_with("192.168.1.40#0", False)

    def test_rest_mode_is_slow_and_gives_the_light_back(self):
        bridge = FakeBridge(resources(area=False))
        try:
            m, _ = manager(bridge, PAIRED)
            m.refresh()
            lamp = next(d for d in m.devices() if d.name == "Desk lamp")
            self.assertEqual((lamp.mode, lamp.segments), ("rest", 1))
            before = copy.deepcopy(next(L for L in bridge.res["light"] if L["id"] == LAMP_LIGHT))
            lamp.razer(True)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0:                            # a minute's show squeezed into a second
                k = int((time.monotonic() - t0) * 60)
                lamp.razer_leds([(255, 0, 0) if k % 2 else (0, 0, 255)] * 4)
                time.sleep(1 / 60)
            lamp.razer(False)
            deadline = time.monotonic() + 3
            while not any(p[1].get("dimming", {}).get("brightness") == 40.0 for p in bridge.puts) and time.monotonic() < deadline:
                time.sleep(0.05)
            colors = [p for p in bridge.puts if "dynamics" in p[1]]
            self.assertGreater(len(colors), 3)
            self.assertLessEqual(len(colors), 12)                         # at most ten a second, not sixty
            after = next(L for L in bridge.res["light"] if L["id"] == LAMP_LIGHT)
            self.assertEqual((after["on"], after["dimming"], after["color"]["xy"]), (before["on"], before["dimming"], before["color"]["xy"]))
            self.assertEqual(lamp.status()["brightness"], 40)
        finally:
            bridge.close()

    def test_colors(self):
        x, y, bri = hue.rgb_to_xy(255, 0, 0)
        self.assertAlmostEqual(x, 0.7006, places=3)
        self.assertAlmostEqual(y, 0.2993, places=3)
        self.assertEqual(bri, 100.0)
        r, g, b = hue.xy_to_rgb(*hue.rgb_to_xy(0, 0, 255)[:2], 100)
        self.assertGreater(b, 200)
        self.assertLess(max(r, g), 60)


class TestStudio(unittest.IsolatedAsyncioTestCase):
    async def test_light_list_and_pairing_endpoint(self):
        from aiohttp.test_utils import TestClient, TestServer
        from lightshow.studio import server
        bridge = FakeBridge(resources(), button_after=10 ** 6)
        config = memory_config({"known_devices": {}})
        load, save, box = config
        try:
            with mock.patch.object(server, "load_config", load), mock.patch.object(server, "save_config", save), \
                    mock.patch.object(server.govee_lan, "discover", return_value=[]), \
                    mock.patch.object(server.govee_lan, "arp_table", return_value={}):
                s = server.Studio(port=0, lights=False, player=False)
                s.hue, _ = manager(bridge, config=config)
                async with TestClient(TestServer(s.app)) as client:
                    rows = await (await client.get("/api/devices?rescan=1")).json()
                    self.assertEqual([(r["name"], r["online"], r["on"]) for r in rows], [("Hue Bridge: press its button", False, False)])
                    r = await client.post("/api/hue/pair", json={"bridge": "ECB5FAFFFE0A1B2C"})
                    self.assertEqual((await r.json())["state"], "waiting")
                    self.assertEqual((await client.post("/api/hue/pair", json={"bridge": "nope"})).status, 404)
                    box["cfg"].update(copy.deepcopy(PAIRED))                 # as if its button had been pressed
                    s.hue.last_read.clear()
                    rows = await (await client.get("/api/devices?rescan=1")).json()
                    lights = [(r["ip"], r["name"], r["segments"], r["online"]) for r in rows if "hue" not in r]
                    self.assertEqual(lights, [("127.0.0.1#0", "Hall bulb", 1, True), ("127.0.0.1#1", "TV strip", 3, True)])
                    self.assertEqual([r["name"] for r in rows if r.get("hue") == "outside"], ["Desk lamp"])
                    self.assertEqual(s._light_segments("127.0.0.1#1"), 3)
                    st = await (await client.get("/api/hue")).json()
                    self.assertEqual(st["bridges"][0]["area"]["name"], "Living room")
                    self.assertNotIn("127.0.0.1#0", box["cfg"].get("known_devices", {}))   # not remembered as a Govee light
        finally:
            bridge.close()


class TestDtls(unittest.TestCase):
    def test_prf_vector(self):
        """The TLS 1.2 PRF (SHA-256) test vector published with RFC 5246."""
        out = prf(bytes.fromhex("9bbe436ba940f017b17652849a71db35"), b"test label",
                  bytes.fromhex("a0ba9f936cda311827a6f796ffd5198c"), 100)
        self.assertEqual(out.hex(), "e3f229ba727be17b8d122620557cd453c2aab21d07c3d495329b52d4e61edb5a6b301791e90d35c9c9a46b4e14baf9"
                                    "af0fa022f7077def17abfd3797c0564bab4fbc91666e9def9b97fce34f796789baa48082d122ee42c5a72e5a51"
                                    "10fff70187347b66")

    @unittest.skipUnless(os.environ.get("HUE_TEST_MBEDTLS_PYTHON"),
                         "set HUE_TEST_MBEDTLS_PYTHON to a Python with python-mbedtls to check against mbedTLS (the bridge's library)")
    def test_handshake_against_mbedtls(self):
        from lightshow.dtls_psk import DtlsPskClient
        server_code = r'''
import socket, sys
from contextlib import suppress
from mbedtls import tls
conf = tls.DTLSConfiguration(pre_shared_key_store={"studio": bytes.fromhex(sys.argv[2])}, validate_certificates=False,
                             ciphers=["TLS-PSK-WITH-AES-128-GCM-SHA256"], lowest_supported_version=tls.DTLSVersion.DTLSv1_2)
raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock = tls.ServerContext(conf).wrap_socket(raw); sock.bind(("127.0.0.1", int(sys.argv[1]))); print("ready", flush=True)
conn, addr = sock.accept(); conn.setcookieparam(addr[0].encode())
with suppress(tls.HelloVerifyRequest): conn.do_handshake()
conn, addr = conn.accept(); conn.setcookieparam(addr[0].encode()); conn.do_handshake()
print(conn.recv(4096).hex(), flush=True)
'''
        port = 12000 + os.getpid() % 1000
        psk = "00112233445566778899aabbccddeeff"
        p = subprocess.Popen([os.environ["HUE_TEST_MBEDTLS_PYTHON"], "-c", server_code, str(port), psk],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ready")
            c = DtlsPskClient("127.0.0.1", port, b"studio", bytes.fromhex(psk))
            c.connect()
            pkt = hue.stream_packet(AREA, 1, [(0, 255, 0, 0)])
            c.send(pkt)
            self.assertEqual(p.stdout.readline().strip(), pkt.hex())
            c.close()
        finally:
            p.kill()
            p.wait()
            p.stdout.close()


if __name__ == "__main__":
    unittest.main()
