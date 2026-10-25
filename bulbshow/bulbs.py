"""The bulb end of a show: finding bulbs, and three ways of driving one.

  LanBulb      Govee's LAN protocol (the same one the studio uses), up to 20 colors a second.
  CloudBulb    Govee's cloud API (a key from the Govee Home app), for bulbs with no LAN Control.
               Two commands a second per bulb and 10,000 a day per account, so only changes are sent.
  VirtualBulb  drives nothing; it only remembers its color (tests, and a copy run with --no-lights).

Every bulb takes the whole picture as one color: brightness is folded into it, and black is dark.
A cloud bulb can also be told to run one of its own scenes (effects.scene_state); a LAN bulb has no
way to be asked for one and stays dark instead.
Each one notes what the bulb was showing before the show and puts that back when the show stops.
"""

from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
import uuid

from lightshow import govee_lan

from .effects import Bucket, CLOUD_DAILY, is_scene

CLOUD_HOST = "openapi.api.govee.com"


class VirtualBulb:
    via = "virtual"

    def __init__(self, spec: dict | None = None):
        self.spec = spec or {}
        self.color = (0, 0, 0)
        self.sent = 0
        self.error = ""

    def begin(self) -> None:
        pass

    def apply(self, rgb) -> None:
        if tuple(rgb) != self.color:
            self.color = tuple(rgb)
            self.sent += 1

    def end(self) -> None:
        self.color = (0, 0, 0)


class LanBulb(VirtualBulb):
    via = "lan"
    MIN_GAP = 1 / 20

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.dev = govee_lan.GoveeDevice(ip=spec.get("ip", ""), device_id=spec.get("id", ""), sku=spec.get("sku", ""))
        self.before = None
        self.last_at = 0.0

    def begin(self) -> None:
        st = self.dev.status(1.0)
        if st is None and self.spec.get("id"):                 # the router may have given it a new address
            for d in govee_lan.discover(2.5):
                if d.device_id == self.spec["id"]:
                    self.dev.ip = d.ip
                    st = self.dev.status(1.0)
                    break
        if st is None:
            raise ConnectionError(f"the bulb at {self.dev.ip or '?'} does not answer on the LAN")
        self.before = st
        self.color = None                                      # whatever comes first is sent, black too
        for _ in range(2):                                     # UDP: say it twice
            self.dev.turn(True)
            self.dev.brightness(100)

    def apply(self, rgb) -> None:
        """Called every tick with the color of the moment, so a change held back by the gap goes out
        on a later tick."""
        rgb = (0, 0, 0) if is_scene(rgb) else tuple(rgb)
        now = time.monotonic()
        if rgb == self.color or now - self.last_at < self.MIN_GAP:
            return
        self.dev.color(*rgb)
        self.color, self.last_at = rgb, now
        self.sent += 1

    def end(self) -> None:
        st = self.before or {}
        c = st.get("color") or {}
        if st.get("colorTemInKelvin"):
            self.dev.white(st["colorTemInKelvin"])
        elif c:
            self.dev.color(c.get("r", 255), c.get("g", 255), c.get("b", 255))
        if "brightness" in st:
            self.dev.brightness(st["brightness"])
        if st.get("onOff") == 0:
            self.dev.turn(False)


class CloudUsage:
    """Commands sent to Govee's cloud today, all shows together (the daily allowance is per account)."""

    def __init__(self, load=None, save=None):
        self.lock = threading.Lock()
        self._save = save
        d = (load() if load else None) or {}
        self.day, self.count = d.get("day", ""), int(d.get("count", 0))
        self.exhausted = False

    def add(self, n: int = 1) -> None:
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            if self.day != today:
                self.day, self.count, self.exhausted = today, 0, False
            self.count += n

    def snapshot(self) -> dict:
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            count = self.count if self.day == today else 0
            return {"day": today, "count": count, "limit": CLOUD_DAILY, "exhausted": self.exhausted and self.day == today}

    def save(self) -> None:
        if self._save:
            s = self.snapshot()
            self._save({"day": s["day"], "count": s["count"]})


class CloudError(RuntimeError):
    pass


class CloudClient:
    """Govee's cloud API (developer.govee.com), over one kept-alive HTTPS connection."""

    def __init__(self, key: str, usage: CloudUsage | None = None, timeout: float = 6.0):
        self.key, self.usage, self.timeout = key, usage, timeout
        self.conn = None
        self.lock = threading.Lock()

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.key:
            raise CloudError("no Govee API key yet (⚙ settings)")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Govee-API-Key": self.key, "Content-Type": "application/json"}
        with self.lock:
            for attempt in range(2):
                if self.conn is None:
                    self.conn = http.client.HTTPSConnection(CLOUD_HOST, timeout=self.timeout, context=ssl.create_default_context())
                try:
                    self.conn.request(method, path, body=data, headers=headers)
                    resp = self.conn.getresponse()
                    raw = resp.read()
                    break
                except (OSError, http.client.HTTPException):
                    self.conn.close()
                    self.conn = None
                    if attempt:
                        raise CloudError("Govee's cloud did not answer")
        if self.usage is not None:
            self.usage.add()
        try:
            doc = json.loads(raw.decode() or "{}")
        except ValueError:
            doc = {}
        if resp.status == 429:
            msg = str(doc.get("message") or doc.get("msg") or "")
            if "24" in msg or "day" in msg.lower() or "10000" in msg:
                if self.usage is not None:
                    self.usage.exhausted = True
                raise CloudError("Govee's daily limit is used up (10,000 commands a day): the lights wait until tomorrow")
            raise CloudError("too fast for Govee's cloud")
        if resp.status in (401, 403):
            raise CloudError("Govee did not accept the API key")
        if resp.status >= 400 or doc.get("code") not in (None, 200):
            raise CloudError(f"Govee's cloud said {resp.status}: {doc.get('message') or doc.get('msg') or raw[:120]!r}")
        return doc

    def devices(self) -> list[dict]:
        doc = self._request("GET", "/router/api/v1/user/devices")
        out = []
        for d in doc.get("data") or []:
            if "light" not in str(d.get("type", "")):
                continue
            out.append({"id": d.get("device", ""), "sku": d.get("sku", ""), "name": d.get("deviceName") or d.get("sku", "")})
        return out

    def control(self, sku: str, device: str, ctype: str, instance: str, value) -> None:
        self._request("POST", "/router/api/v1/device/control", {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": sku, "device": device, "capability": {"type": ctype, "instance": instance, "value": value}}})

    def scenes(self, sku: str, device: str) -> list[dict]:
        """The bulb's own scenes, as {name, instance, value}: Govee's built-in ones, then any DIY scenes
        made in the Govee app. The value goes back to Govee exactly as it came."""
        out = []
        for path, inst in (("/router/api/v1/device/scenes", "lightScene"), ("/router/api/v1/device/diy-scenes", "diyScene")):
            try:
                doc = self._request("POST", path, {"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}})
            except CloudError:
                if inst == "lightScene":
                    raise
                continue                                  # (no DIY scenes is not a problem)
            for cap in ((doc.get("payload") or {}).get("capabilities") or []):
                for o in ((cap.get("parameters") or {}).get("options") or []):
                    if o.get("value") is not None:
                        out.append({"name": o.get("name") or "?", "instance": cap.get("instance") or inst, "value": o["value"]})
        return out

    def state(self, sku: str, device: str) -> dict:
        doc = self._request("POST", "/router/api/v1/device/state", {
            "requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}})
        out = {}
        for cap in ((doc.get("payload") or {}).get("capabilities") or []):
            out[cap.get("instance")] = (cap.get("state") or {}).get("value")
        return out


def rgb_int(rgb) -> int:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return (r << 16) | (g << 8) | b


class CloudBulb(VirtualBulb):
    """Sends only changes, newest first: when the allowance is short, colors in between are skipped,
    never queued (a queue would fall further and further behind the sound)."""

    via = "cloud"

    def __init__(self, spec: dict, client: CloudClient, clock=time.monotonic):
        super().__init__(spec)
        self.client, self.clock = client, clock
        self.sku, self.device = spec.get("sku", ""), spec.get("id", "")
        self.bucket = Bucket(now=clock())
        self.want = None
        self.cv = threading.Condition()
        self.stopping = False
        self.before = {}
        self.thread = None

    def _send(self, st) -> None:
        if is_scene(st):
            self.client.control(self.sku, self.device, "devices.capabilities.dynamic_scene", st[1], json.loads(st[2]))
        else:
            self.client.control(self.sku, self.device, "devices.capabilities.color_setting", "colorRgb", rgb_int(st))

    def begin(self) -> None:
        try:
            self.before = self.client.state(self.sku, self.device)
        except CloudError:
            self.before = {}
        self.color = None
        self.client.control(self.sku, self.device, "devices.capabilities.on_off", "powerSwitch", 1)
        self.client.control(self.sku, self.device, "devices.capabilities.range", "brightness", 100)
        self.bucket.tokens = 0                               # the three commands above came out of the burst
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def apply(self, rgb) -> None:
        rgb = tuple(rgb)
        with self.cv:
            if rgb != self.want:
                self.want = rgb
                self.cv.notify()

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.stopping and (self.want is None or self.want == self.color):
                    self.cv.wait(0.5)
                if self.stopping:
                    return
                gap = self.bucket.wait(self.clock())
            if gap > 0:
                time.sleep(gap)
                continue
            with self.cv:
                rgb = self.want
                if rgb is None or rgb == self.color or not self.bucket.take(self.clock()):
                    continue
            try:
                self._send(rgb)
                self.color, self.error = rgb, ""
                self.sent += 1
            except CloudError as e:
                self.error = str(e)
                if self.client.usage is not None and self.client.usage.exhausted:
                    with self.cv:
                        self.stopping = True
                    return
                time.sleep(1.0)

    def end(self) -> None:
        with self.cv:
            self.stopping = True
            self.cv.notify()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.client.usage is not None and self.client.usage.exhausted:
            return
        b = self.before
        try:
            if b.get("colorRgb") is not None:
                self.client.control(self.sku, self.device, "devices.capabilities.color_setting", "colorRgb", int(b["colorRgb"]))
            if b.get("brightness") is not None:
                self.client.control(self.sku, self.device, "devices.capabilities.range", "brightness", int(b["brightness"]))
            if b.get("powerSwitch") == 0:
                self.client.control(self.sku, self.device, "devices.capabilities.on_off", "powerSwitch", 0)
        except CloudError:
            pass


def find_bulbs(client: CloudClient | None, lan_timeout: float = 2.5) -> tuple[list[dict], str]:
    """Every Govee light this app could drive: on the LAN first, then the account's cloud lights
    (a light found both ways is taken over the LAN). Returns (bulbs, a problem with the cloud or "")."""
    out, problem = [], ""
    seen = set()
    for d in govee_lan.discover(lan_timeout):
        out.append({"id": d.device_id, "sku": d.sku, "ip": d.ip, "via": "lan", "name": f"{d.sku} at {d.ip}"})
        seen.add(d.device_id)
    if client is not None and client.key:
        try:
            for d in client.devices():
                if d["id"] in seen:
                    for o in out:
                        if o["id"] == d["id"]:
                            o["name"] = d["name"]
                    continue
                out.append({**d, "via": "cloud"})
        except CloudError as e:
            problem = str(e)
    return out, problem


def make_bulb(spec: dict, client: CloudClient | None, virtual: bool = False):
    if virtual or not spec:
        return VirtualBulb(spec)
    if spec.get("via") == "lan":
        return LanBulb(spec)
    if client is None:
        raise CloudError("no Govee API key yet (⚙ settings)")
    return CloudBulb(spec, client)
