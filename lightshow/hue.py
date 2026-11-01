"""Philips Hue: bridges found on the network, paired once, their lights streamed in real time.

  * Discovery: mDNS (_hue._tcp.local) first, SSDP (UPnP M-SEARCH) when mDNS finds nothing.
  * Pairing:   the bridge's link button is pressed while the studio asks POST /api for a user name; the
               bridge answers with a user name and a client key, kept in config.json under "hue".
  * Lights:    the CLIP v2 API (https, the bridge's own self-signed certificate). Each light in an
               Entertainment area (set up in the Hue app) is one light here; a gradient strip's channels
               are its segments.
  * Streaming: the area is started (PUT action start), then HueStream v2 packets go over DTLS 1.2 with the
               client key to UDP 2100, 50 a second. Stopping the area gives the lights back to the Hue app.
  * Fallback:  a bridge with no Entertainment area gets slow REST writes (10 a second per bridge, the
               newest color of each light), with each light's state put back when the show lets it go.

HueDevice has the same methods as govee_lan.GoveeDevice, so the studio's light output drives both alike.
Its `ip` is a key, "<bridge ip>#<channel>": unique per light, and the part before the # is pingable.
"""

from __future__ import annotations

import json
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEVICETYPE = "light_show_studio#pc"
STREAM_PORT = 2100
STREAM_HZ = 50
REST_PER_S = 10
IDLE_S = 60.0                 # no colors for this long: the area stops (as a Govee light leaves real-time mode)
PAIR_S = 30.0
SCAN_EVERY_S = 300.0          # look for new bridges this often (and on every rescan)
LIGHTS_EVERY_S = 60.0         # read a paired bridge's lights this often (and on every rescan)
FPS = (25, 25, 25)            # frames a second from the page: Hue lights show about 25 (the stream itself runs at 50)
SEP = "#"


def log(msg: str) -> None:
    print(f"[hue] {msg}", flush=True)


# -- keys: "<bridge ip>#<channel>" ----------------------------------------------------------------------
def is_key(ip: str) -> bool:
    return SEP in str(ip)


def address(ip: str) -> str:
    """The pingable address behind a light's key (a Hue light's is its bridge)."""
    return str(ip).split(SEP, 1)[0]


def sort_key(ip: str) -> tuple:
    """Lights in address order, a bridge's lights after it by channel."""
    addr, _, rest = str(ip).partition(SEP)
    try:
        nums = tuple(int(p) for p in addr.split("."))
    except ValueError:
        nums = (999,)
    return nums + ((1, int(rest), "") if rest.isdigit() else (1 if rest else 0, 10 ** 6, rest),)


def via_bridge(ping):
    """Wrap a ping(ip, timeout_ms) so a Hue light's key pings its bridge."""
    return lambda ip, timeout_ms=300: ping(address(ip), timeout_ms)


# -- the bridge's HTTP API -----------------------------------------------------------------------------
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE           # every bridge has its own self-signed certificate


class BridgeApi:
    """GET/POST/PUT to one bridge. `base` is https://<ip> (a test passes a local http:// server)."""

    def __init__(self, ip: str, username: str = "", base: str | None = None, timeout: float = 3.0):
        self.ip, self.username, self.timeout = ip, username, timeout
        self.base = base or f"https://{ip}"

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.username:
            req.add_header("hue-application-key", self.username)
        ctx = _TLS if self.base.startswith("https") else None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        return json.loads(raw.decode() or "null")

    def resource(self, kind: str, rid: str = "") -> list:
        doc = self.call("GET", f"/clip/v2/resource/{kind}" + (f"/{rid}" if rid else ""))
        if not isinstance(doc, dict):
            return []
        if doc.get("errors") and not doc.get("data"):
            raise OSError(f"{kind}: {doc['errors'][0].get('description', 'error')}")
        return doc.get("data") or []

    def put(self, kind: str, rid: str, body: dict):
        return self.call("PUT", f"/clip/v2/resource/{kind}/{rid}", body)


# -- discovery ----------------------------------------------------------------------------------------
def parse_mdns(name: str, addresses: list[str], properties: dict) -> dict | None:
    """A bridge from one mDNS answer: {"id", "ip", "name"} (None without an IPv4 address)."""
    props = {(k.decode() if isinstance(k, bytes) else str(k)).lower(): (v.decode() if isinstance(v, bytes) else v or "")
             for k, v in (properties or {}).items()}
    ip = next((a for a in addresses if a.count(".") == 3), None)
    if not ip:
        return None
    return {"id": str(props.get("bridgeid", "")).upper(), "ip": ip, "name": name.split("._hue")[0].split(".")[0]}


def parse_ssdp(text: str, ip: str) -> dict | None:
    """A bridge from one SSDP answer (a Hue bridge says IpBridge in SERVER, and sends hue-bridgeid)."""
    headers = {}
    for line in text.splitlines()[1:]:
        k, sep, v = line.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()
    if "ipbridge" not in headers.get("server", "").lower() and "hue-bridgeid" not in headers:
        return None
    return {"id": headers.get("hue-bridgeid", "").upper(), "ip": ip, "name": ""}


def discover_mdns(timeout: float = 1.0) -> list[dict]:
    from zeroconf import ServiceBrowser, Zeroconf
    kind = "_hue._tcp.local."
    names: list[str] = []
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, kind, handlers=[lambda zeroconf, service_type, name, state_change: names.append(name)])
        time.sleep(timeout)
        out = []
        for name in dict.fromkeys(names):
            info = zc.get_service_info(kind, name, timeout=1000)
            if info:
                b = parse_mdns(name, info.parsed_addresses(), info.properties)
                if b:
                    out.append(b)
        return out
    finally:
        zc.close()


def discover_ssdp(timeout: float = 1.0) -> list[dict]:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\n"
           "ST: urn:schemas-upnp-org:device:basic:1\r\n\r\n").encode()
    out: dict = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.settimeout(0.2)
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                raw, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            b = parse_ssdp(raw.decode(errors="replace"), addr[0])
            if b:
                out[b["ip"]] = b
    finally:
        s.close()
    return list(out.values())


def discover(timeout: float = 1.0) -> list[dict]:
    """Bridges on the network: mDNS, then SSDP if mDNS found none. Never raises."""
    for way in (discover_mdns, discover_ssdp):
        try:
            found = way(timeout)
        except Exception as e:  # noqa: BLE001 - discovery must never break the light list
            log(f"{getattr(way, '__name__', 'discovery')}: {type(e).__name__}: {e}")
            found = []
        if found:
            return found
    return []


# -- pairing ------------------------------------------------------------------------------------------
def pair_once(api: BridgeApi) -> tuple[str, dict]:
    """Ask once for a user name. ("paired", {"username", "clientkey"}), ("button", {}) or ("error", {"error"})."""
    try:
        doc = api.call("POST", "/api", {"devicetype": DEVICETYPE, "generateclientkey": True})
    except (OSError, ValueError) as e:
        return "error", {"error": f"{type(e).__name__}: {e}"}
    item = doc[0] if isinstance(doc, list) and doc else {}
    if "success" in item:
        ok = item["success"]
        return "paired", {"username": ok.get("username", ""), "clientkey": ok.get("clientkey", "")}
    err = item.get("error") or {}
    if err.get("type") == 101:                     # link button not pressed
        return "button", {}
    return "error", {"error": err.get("description") or "unexpected answer"}


# -- the bridge's lights ------------------------------------------------------------------------------
def parse_lights(bridge_ip: str, res: dict, area_choice: str = "") -> dict:
    """What a bridge's CLIP v2 resources hold, for the light list.

    res: {"entertainment_configuration", "entertainment", "light", "device", "zigbee_connectivity"}: lists.
    Returns {"area": {"id", "name"} or None, "lights": [...], "outside": [...]}: each light a dict with
    key, mode ("stream" or "rest"), channels (in segment order), light_id, device_id, name, model, mac.
    The area is the one named in config (area_choice), else the one with the most lights.
    """
    devices = {d["id"]: d for d in res.get("device", [])}
    owner_of = {e["id"]: e.get("owner", {}).get("rid") for e in res.get("entertainment", [])}
    macs = {z.get("owner", {}).get("rid"): str(z.get("mac_address", "")).upper() for z in res.get("zigbee_connectivity", [])}
    lights = res.get("light", [])
    light_of_device = {l.get("owner", {}).get("rid"): l for l in lights}

    def info(dev_id: str, light: dict | None) -> dict:
        dev = devices.get(dev_id, {})
        name = ((light or {}).get("metadata") or {}).get("name") or (dev.get("metadata") or {}).get("name") or "Hue light"
        model = (dev.get("product_data") or {}).get("model_id") or "Hue"
        return {"light_id": (light or {}).get("id", ""), "device_id": "hue:" + dev_id, "name": name, "model": model,
                "mac": macs.get(dev_id, "")}

    areas = res.get("entertainment_configuration", [])
    area = next((a for a in areas if a.get("id") == area_choice), None) if area_choice else None
    if area is None and areas:
        area = max(areas, key=lambda a: (len(a.get("light_services") or a.get("channels") or []),
                                         ((a.get("metadata") or {}).get("name") or "")))
    out_lights, inside = [], set()
    if area is not None:
        by_dev: dict = {}
        for ch in area.get("channels", []):
            for m in ch.get("members", [])[:1]:
                dev_id = owner_of.get(m.get("service", {}).get("rid"))
                if dev_id:
                    by_dev.setdefault(dev_id, []).append((m.get("index", 0), ch["channel_id"]))
        for dev_id, chans in by_dev.items():
            chans.sort()
            inside.add(dev_id)
            out_lights.append({"key": f"{bridge_ip}{SEP}{min(c for _, c in chans)}", "mode": "stream",
                               "channels": [c for _, c in chans], **info(dev_id, light_of_device.get(dev_id))})
        outside = [{"key": f"{bridge_ip}{SEP}out-{l['id'][:8]}", **info(l.get("owner", {}).get("rid", ""), l)}
                   for l in lights if l.get("owner", {}).get("rid") not in inside]
    else:                                          # no Entertainment area at all: every light, slowly, over REST
        outside = []
        for l in lights:
            out_lights.append({"key": f"{bridge_ip}{SEP}{l['id'][:8]}", "mode": "rest", "channels": [0],
                               **info(l.get("owner", {}).get("rid", ""), l)})
    out_lights.sort(key=lambda d: sort_key(d["key"]))
    return {"area": {"id": area["id"], "name": (area.get("metadata") or {}).get("name") or "Entertainment area"} if area else None,
            "lights": out_lights, "outside": outside}


RESOURCES = ("entertainment_configuration", "entertainment", "light", "device", "zigbee_connectivity")


def read_resources(api: BridgeApi) -> dict:
    out = {}
    for kind in RESOURCES:
        try:
            out[kind] = api.resource(kind)
        except (OSError, ValueError):
            if kind in ("light", "device"):
                raise
            out[kind] = []                         # (an older bridge may lack one of the others)
    return out


# -- colors -------------------------------------------------------------------------------------------
def rgb_to_xy(r: int, g: int, b: int) -> tuple[float, float, float]:
    """sRGB 0..255 to CIE xy and a brightness 0..100 (Philips' published conversion)."""
    def lin(c):
        c = max(0.0, min(1.0, c / 255.0))
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92
    rl, gl, bl = lin(r), lin(g), lin(b)
    x = rl * 0.664511 + gl * 0.154324 + bl * 0.162028
    y = rl * 0.283881 + gl * 0.668433 + bl * 0.047685
    z = rl * 0.000088 + gl * 0.072310 + bl * 0.986039
    s = x + y + z
    if s <= 0:
        return 0.3227, 0.329, 0.0
    return round(x / s, 4), round(y / s, 4), round(max(r, g, b) / 255 * 100, 1)


def xy_to_rgb(x: float, y: float, bri: float) -> tuple[int, int, int]:
    if y <= 0:
        return (0, 0, 0)
    Y = 1.0
    X, Z = Y / y * x, Y / y * (1 - x - y)
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    def gam(c):
        c = max(0.0, c)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    rgb = [gam(c) for c in (r, g, b)]
    m = max(rgb) or 1.0
    return tuple(int(round(255 * c / m)) for c in rgb)


def stream_packet(area_id: str, seq: int, channels: list[tuple[int, int, int, int]]) -> bytes:
    """One HueStream v2 packet: header, version 2.0, sequence, color space RGB, the area's id, then
    7 bytes per channel (its id, then red, green and blue as 16-bit numbers)."""
    head = b"HueStream" + bytes([2, 0, seq & 0xFF, 0, 0, 0, 0]) + area_id.encode("ascii")
    return head + b"".join(struct.pack(">BHHH", ch, r * 257, g * 257, b * 257) for ch, r, g, b in channels)


def _resample(colors: list, n: int) -> list:
    m = len(colors)
    return [colors[min(m - 1, int((i + 0.5) * m / n))] for i in range(n)]


def _c8(v) -> int:
    return max(0, min(255, int(round(v))))


# -- streaming: one per bridge ------------------------------------------------------------------------
def dtls_transport(ip: str, username: str, clientkey: str):
    from .dtls_psk import DtlsPskClient
    c = DtlsPskClient(ip, STREAM_PORT, username.encode(), bytes.fromhex(clientkey))
    c.connect()
    return c


class HueStream:
    """An Entertainment area streamed while any of its lights is in the show. It gathers each channel's
    color from razer_leds and sends them all, 50 times a second, on its own thread."""

    def __init__(self, api: BridgeApi, area_id: str, clientkey: str, transport=dtls_transport, rate: float = STREAM_HZ):
        self.api, self.area_id, self.clientkey = api, area_id, clientkey
        self.transport, self.rate = transport, rate
        self.colors: dict[int, tuple] = {}
        self.wanted: set[str] = set()
        self.off_after: set[str] = set()          # light ids switched off once the area has stopped
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.seq = 0
        self.sent = 0
        self.last_set = 0.0
        self.last_error = ""

    def _start(self) -> None:
        if self.thread is None:                     # (called holding the lock)
            self.thread = threading.Thread(target=self._run, daemon=True, name=f"hue-stream-{self.api.ip}")
            self.thread.start()

    def _idle(self) -> bool:
        return time.monotonic() - self.last_set > IDLE_S

    def enable(self, key: str, on: bool) -> None:
        with self.lock:
            if on:
                self.wanted.add(key)
                self.last_set = time.monotonic()
                self._start()
                return
            if key not in self.wanted:
                return
            self.wanted.discard(key)
            t = self.thread if not self.wanted else None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)                     # the area stopped: the Hue app's colors are back

    def set(self, colors: dict) -> None:
        with self.lock:
            self.colors.update(colors)
            self.last_set = time.monotonic()
            if self.wanted:
                self._start()                       # (stopped while idle: colors again start it again)

    def packet(self) -> bytes:
        with self.lock:
            chans = sorted(self.colors.items())
        self.seq = (self.seq + 1) & 0xFF
        return stream_packet(self.area_id, self.seq, [(ch, *rgb) for ch, rgb in chans])

    def _run(self) -> None:
        while True:
            with self.lock:
                if not self.wanted or self._idle():
                    self.thread = None
                    return
            conn = None
            try:
                self.api.put("entertainment_configuration", self.area_id, {"action": "start"})
                conn = self.transport(self.api.ip, self.api.username, self.clientkey)
                log(f"{self.api.ip}: streaming")
                gap = 1.0 / self.rate
                nxt = time.monotonic()
                while self.wanted and not self._idle():
                    conn.send(self.packet())
                    self.sent += 1
                    nxt += gap
                    time.sleep(max(0.0, nxt - time.monotonic()))
            except Exception as e:  # noqa: BLE001 - a lost stream is tried again while lights want it
                err = f"{type(e).__name__}: {e}"
                if err != self.last_error:          # (said once, not every two seconds)
                    log(f"{self.api.ip}: stream failed: {err}")
                self.last_error = err
                time.sleep(2.0)
            finally:
                if conn is not None:
                    conn.close()
            if not self.wanted or self._idle():
                try:
                    self.api.put("entertainment_configuration", self.area_id, {"action": "stop"})
                except Exception as e:  # noqa: BLE001 - the bridge ends it itself after 10 s without packets
                    log(f"{self.api.ip}: stop failed: {type(e).__name__}: {e}")
                with self.lock:
                    off, self.off_after = self.off_after, set()
                for lid in off:
                    try:
                        self.api.put("light", lid, {"on": {"on": False}})
                    except Exception:  # noqa: BLE001
                        pass


# -- the slow REST fallback: one per bridge -----------------------------------------------------------
class RestWriter:
    """At most REST_PER_S writes a second to one bridge, each light's newest color only. A light taken
    by the show has its state read first and written back when it is let go."""

    def __init__(self, api: BridgeApi, per_s: float = REST_PER_S):
        self.api, self.gap = api, 1.0 / per_s
        self.lock = threading.Lock()
        self.want: dict[str, tuple] = {}             # light id -> newest color
        self.sent: dict[str, tuple] = {}
        self.saved: dict[str, dict] = {}             # light id -> its state before the show
        self.jobs: list = []                         # ("save", id) / ("put", id, body): before any color
        self.order: list[str] = []
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None

    def _start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name=f"hue-rest-{self.api.ip}")
            self.thread.start()
        self.wake.set()

    def enable(self, light_id: str, on: bool) -> None:
        with self.lock:
            if on and light_id not in self.order:
                self.order.append(light_id)
                self.jobs.append(("save", light_id))
            elif not on and light_id in self.order:
                self.order.remove(light_id)
                self.want.pop(light_id, None)
                self.sent.pop(light_id, None)
                self.jobs.append(("restore", light_id))
            else:
                return
        self._start()

    def command(self, light_id: str, body: dict) -> None:
        with self.lock:
            self.jobs.append(("put", light_id, body))
        self._start()

    def set(self, light_id: str, rgb: tuple) -> None:
        with self.lock:
            if light_id in self.order:
                self.want[light_id] = rgb
        self.wake.set()

    @staticmethod
    def color_body(rgb: tuple) -> dict:
        if max(rgb) < 3:
            return {"on": {"on": False}}
        x, y, bri = rgb_to_xy(*rgb)
        return {"on": {"on": True}, "dimming": {"brightness": max(1.0, bri)}, "color": {"xy": {"x": x, "y": y}},
                "dynamics": {"duration": 100}}

    @staticmethod
    def restore_body(st: dict) -> dict:
        body = {"on": {"on": bool((st.get("on") or {}).get("on", True))}}
        if body["on"]["on"]:
            if st.get("dimming"):
                body["dimming"] = {"brightness": st["dimming"].get("brightness", 100)}
            ct = st.get("color_temperature") or {}
            if ct.get("mirek_valid") and ct.get("mirek"):
                body["color_temperature"] = {"mirek": ct["mirek"]}
            elif (st.get("color") or {}).get("xy"):
                body["color"] = {"xy": st["color"]["xy"]}
        return body

    def _next(self):
        with self.lock:
            if self.jobs:
                return self.jobs.pop(0)
            for lid in list(self.order):
                rgb = self.want.get(lid)
                if lid in self.saved and rgb is not None and _far(rgb, self.sent.get(lid)):
                    self.order.remove(lid)            # round robin: this light goes to the back
                    self.order.append(lid)
                    self.sent[lid] = rgb
                    return ("put", lid, self.color_body(rgb))
        return None

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                self.wake.clear()
                if not self.wake.wait(30.0):
                    with self.lock:
                        if not self.order and not self.jobs:
                            self.thread = None
                            return
                continue
            try:
                if job[0] == "save":
                    got = self.api.resource("light", job[1])
                    with self.lock:
                        self.saved[job[1]] = got[0] if got else {}
                elif job[0] == "restore":
                    with self.lock:
                        st = self.saved.pop(job[1], None)
                    if st:
                        self.api.put("light", job[1], self.restore_body(st))
                else:
                    self.api.put("light", job[1], job[2])
            except Exception as e:  # noqa: BLE001 - a lost write is replaced by the next color
                log(f"{self.api.ip}: {job[0]} {job[1][:8]} failed: {type(e).__name__}: {e}")
                if job[0] == "save":
                    with self.lock:
                        self.saved.setdefault(job[1], {})
            time.sleep(self.gap)


def _far(a: tuple, b: tuple | None) -> bool:
    return b is None or sum(abs(x - y) for x, y in zip(a, b)) >= 12


# -- one light ----------------------------------------------------------------------------------------
@dataclass(eq=False)
class HueDevice:
    """A Hue light as the light output sees one (the methods of govee_lan.GoveeDevice)."""
    ip: str                                       # "<bridge ip>#<channel>": a key, see the module notes
    device_id: str = ""                           # "hue:<the Hue device's id>"
    sku: str = "Hue"                              # the model, e.g. LCX004
    mac: str = ""                                 # its Zigbee address
    name: str = ""                                # as named in the Hue app
    channels: list = field(default_factory=lambda: [0])
    light_id: str = ""
    mode: str = "stream"                          # "stream" (an Entertainment area) or "rest"
    stream: HueStream | None = field(default=None, repr=False)
    rest: RestWriter | None = field(default=None, repr=False)
    online: bool = True

    @property
    def segments(self) -> int:
        return len(self.channels) if self.mode == "stream" else 1

    def razer(self, enable: bool) -> None:
        if self.mode == "stream" and self.stream is not None:
            if not enable:                        # (a light let go while others play on stays dark until they stop)
                self.stream.set({ch: (0, 0, 0) for ch in self.channels})
            self.stream.enable(self.ip, enable)
        elif self.rest is not None and self.light_id:
            self.rest.enable(self.light_id, enable)

    def razer_leds(self, colors: list, gradient: bool = False) -> None:
        if not colors:
            return
        if self.mode == "stream" and self.stream is not None:
            cols = colors if len(colors) == len(self.channels) else _resample(colors, len(self.channels))
            self.stream.set({ch: (_c8(r), _c8(g), _c8(b)) for ch, (r, g, b) in zip(self.channels, cols)})
        elif self.rest is not None and self.light_id:
            n = len(colors)
            self.rest.set(self.light_id, tuple(_c8(sum(c[i] for c in colors) / n) for i in range(3)))

    def turn(self, on: bool) -> None:
        """Off goes to the bridge (stop(off=True) after a show); on is left to the stream or the colors."""
        if on or not self.light_id:
            return
        if self.stream is not None and self.stream.wanted:      # once the area has stopped (or it comes back on)
            with self.stream.lock:
                self.stream.off_after.add(self.light_id)
        elif self.rest is not None:
            self.rest.command(self.light_id, {"on": {"on": False}})

    # The light's own brightness and color belong to the Hue app: stopping the area gives them back.
    def brightness(self, value: int) -> None:
        pass

    def color(self, r: int, g: int, b: int) -> None:
        pass

    def white(self, kelvin: int) -> None:
        pass

    def send(self, cmd: str, data: dict | None = None) -> None:
        pass                                      # (govee_lan.statuses asks every light; a Hue light never answers)

    def status(self, timeout: float = 1.0) -> dict | None:
        """How the light is now, in the shape a Govee light answers devStatus."""
        if self.rest is None or not self.light_id:
            return None
        try:
            got = self.rest.api.resource("light", self.light_id)
        except (OSError, ValueError):
            return None
        if not got:
            return None
        st = got[0]
        xy = (st.get("color") or {}).get("xy") or {}
        r, g, b = xy_to_rgb(xy.get("x", 0.3227), xy.get("y", 0.329), 100) if xy else (255, 255, 255)
        return {"onOff": 1 if (st.get("on") or {}).get("on") else 0,
                "brightness": int(round((st.get("dimming") or {}).get("brightness", 100))),
                "color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}

    @property
    def label(self) -> str:
        return f"{self.name or self.sku} @ {self.ip}"


# -- every bridge: found, paired, read ----------------------------------------------------------------
class HueManager:
    """The studio's Hue side. config.json "hue": {"scan": false} turns it off; "bridges": {id: {"ip",
    "name", "username", "clientkey", "area" (optional: which Entertainment area to play)}}."""

    def __init__(self, load_config, save_config, discover=discover, api=BridgeApi, transport=dtls_transport):
        self.load_config, self.save_config = load_config, save_config
        self._discover, self._api, self._transport = discover, api, transport
        self.lock = threading.RLock()
        self.found: dict[str, dict] = {}            # bridge id (or ip, until it is known) -> {"id", "ip", "name"}
        self.pairing: dict[str, dict] = {}          # bridge id -> {"state", "until", "error"}
        self.devices_by_key: dict[str, HueDevice] = {}
        self.outside: dict[str, list] = {}          # bridge id -> lights in no area: [{"key", "name", ...}]
        self.areas: dict[str, dict | None] = {}
        self.streams: dict[str, HueStream] = {}
        self.rests: dict[str, RestWriter] = {}
        self.last_scan = 0.0
        self.last_read: dict[str, float] = {}
        self.busy = threading.Lock()                # one refresh at a time
        self.errors: dict[str, str] = {}

    def _cfg(self) -> dict:
        return self.load_config().get("hue") or {}

    def paired(self) -> dict:
        return {k: v for k, v in (self._cfg().get("bridges") or {}).items() if v.get("username") and v.get("clientkey")}

    def refresh(self, force: bool = False) -> None:
        """Look for bridges (now and then, or when forced) and read paired bridges' lights. Blocks for a
        couple of seconds at most; the studio runs it off the event loop."""
        if self._cfg().get("scan") is False:
            return
        if not self.busy.acquire(blocking=False):
            return                                  # one is under way already
        try:
            now = time.monotonic()
            if force or not self.last_scan or now - self.last_scan > SCAN_EVERY_S:
                self.last_scan = now
                self._scan(force)
            for bid, b in self.paired().items():
                if force or now - self.last_read.get(bid, -1e9) > LIGHTS_EVERY_S:
                    self.last_read[bid] = now
                    self._read(bid, b)
        finally:
            self.busy.release()

    def refresh_soon(self, force: bool = False) -> None:
        threading.Thread(target=self.refresh, args=(force,), daemon=True, name="hue-refresh").start()

    def _scan(self, force: bool) -> None:
        for b in self._discover():
            bid = b.get("id") or ""
            if not bid:                              # SSDP from an older bridge: ask it who it is
                try:
                    cfg = self._api(b["ip"]).call("GET", "/api/config")
                    bid = str((cfg or {}).get("bridgeid", "")).upper()
                    b["name"] = b.get("name") or (cfg or {}).get("name", "")
                except (OSError, ValueError):
                    pass
            bid = bid or b["ip"]
            with self.lock:
                fresh = bid not in self.found
                self.found[bid] = {"id": bid, "ip": b["ip"], "name": b.get("name") or "Hue Bridge"}
            paired = self.paired()
            if bid in paired:
                if paired[bid].get("ip") != b["ip"]:     # a new address from the router: remember it
                    cfg = self.load_config()
                    cfg.setdefault("hue", {}).setdefault("bridges", {}).setdefault(bid, {})["ip"] = b["ip"]
                    self.save_config(cfg)
            elif fresh or force:
                self.pair(bid)                       # found (or rescanned): press its button within 30 s

    # -- pairing --
    def pair(self, bridge_id: str) -> dict:
        """Ask the bridge for a user name for PAIR_S seconds, on a thread, while its button is pressed."""
        with self.lock:
            b = self.found.get(bridge_id)
            if b is None:
                return {"state": "unknown"}
            st = self.pairing.get(bridge_id)
            if st and st["state"] == "waiting" and time.monotonic() < st["until"]:
                return self.pair_state(bridge_id)
            self.pairing[bridge_id] = {"state": "waiting", "until": time.monotonic() + PAIR_S, "error": ""}
        threading.Thread(target=self._pair, args=(bridge_id, b["ip"]), daemon=True, name="hue-pair").start()
        return self.pair_state(bridge_id)

    PAIR_POLL_S = 1.0

    def _pair(self, bridge_id: str, ip: str) -> None:
        api = self._api(ip)
        st = self.pairing[bridge_id]
        while time.monotonic() < st["until"]:
            state, got = pair_once(api)
            if state == "paired":
                cfg = self.load_config()
                bridges = cfg.setdefault("hue", {}).setdefault("bridges", {})
                bridges[bridge_id] = {**bridges.get(bridge_id, {}), "ip": ip,
                                      "name": self.found.get(bridge_id, {}).get("name", "Hue Bridge"), **got}
                self.save_config(cfg)
                st["state"] = "paired"
                log(f"{ip}: paired")
                self.last_read.pop(bridge_id, None)
                self.refresh_soon()
                return
            if state == "error":
                st["error"] = got.get("error", "")
            time.sleep(self.PAIR_POLL_S)
        st["state"] = "timeout"

    def pair_state(self, bridge_id: str) -> dict:
        st = self.pairing.get(bridge_id)
        if bridge_id in self.paired():
            return {"state": "paired"}
        if not st:
            return {"state": "idle"}
        return {"state": st["state"], "seconds_left": max(0, round(st["until"] - time.monotonic())), "error": st.get("error", "")}

    # -- lights --
    def _read(self, bid: str, b: dict) -> None:
        ip = (self.found.get(bid) or {}).get("ip") or b.get("ip")
        api = self._api(ip, b["username"])
        try:
            parsed = parse_lights(ip, read_resources(api), b.get("area", ""))
        except Exception as e:  # noqa: BLE001 - an unreachable bridge leaves its lights listed, offline
            self.errors[bid] = f"{type(e).__name__}: {e}"
            with self.lock:
                for d in self.devices_by_key.values():
                    if address(d.ip) == ip:
                        d.online = False
            return
        self.errors.pop(bid, None)
        area = parsed["area"]
        with self.lock:
            self.areas[bid] = area
            self.outside[bid] = parsed["outside"]
            stream = None
            if area:
                stream = self.streams.get(bid)
                if stream is None or stream.area_id != area["id"] or stream.api.ip != ip:
                    if stream is not None:           # the area changed: let the old one go
                        for key in list(stream.wanted):
                            stream.enable(key, False)
                    stream = self.streams[bid] = HueStream(api, area["id"], b["clientkey"], transport=self._transport)
            rest = self.rests.get(bid)
            if rest is None or rest.api.ip != ip:
                rest = self.rests[bid] = RestWriter(api)
            seen = set()
            for L in parsed["lights"]:
                d = self.devices_by_key.get(L["key"])
                if d is None:
                    d = self.devices_by_key[L["key"]] = HueDevice(ip=L["key"])
                d.device_id, d.sku, d.mac, d.name = L["device_id"], L["model"], L["mac"], L["name"]
                d.channels, d.light_id, d.mode = L["channels"], L["light_id"], L["mode"]
                d.stream, d.rest, d.online = stream, rest, True
                seen.add(L["key"])
            for key, d in self.devices_by_key.items():
                if address(key) == ip and key not in seen:
                    d.online = False                 # left the area: kept (a show may hold it), marked offline

    def devices(self) -> list[HueDevice]:
        """Every Hue light this session has seen (offline ones too: a show may still hold one)."""
        with self.lock:
            return list(self.devices_by_key.values())

    def extras(self) -> list[dict]:
        """Rows for the light list that are not lights to play: bridges to pair, lights in no area."""
        paired = self.paired()
        rows = []
        with self.lock:
            for bid, b in self.found.items():
                if bid not in paired:
                    rows.append({"ip": f"{b['ip']}{SEP}pair", "device_id": f"hue-bridge:{bid}", "sku": "Hue Bridge",
                                 "name": "Hue Bridge: press its button", "hue": "pair",
                                 "hint": "Press the round button on the Hue Bridge; the studio pairs with it by itself.",
                                 "pairing": self.pair_state(bid)})
            for bid, lights in self.outside.items():
                area = (self.areas.get(bid) or {}).get("name")
                for L in lights:
                    rows.append({"ip": L["key"], "device_id": L["device_id"], "mac": L["mac"], "sku": L["model"],
                                 "name": L["name"], "hue": "outside",
                                 "hint": f"Add it to the Entertainment area{' ' + repr(area) if area else ''} in the Hue app to play it."})
        for r in rows:
            r.setdefault("mac", "")
            r.update(segments=1, online=False, flip=False, default=False, on=False)
        return rows

    def status(self) -> dict:
        paired = self.paired()
        with self.lock:
            ids = list(dict.fromkeys(list(self.found) + list(paired)))
            return {"bridges": [{"id": bid, "ip": (self.found.get(bid) or paired.get(bid) or {}).get("ip", ""),
                                 "name": (self.found.get(bid) or paired.get(bid) or {}).get("name", "Hue Bridge"),
                                 "found": bid in self.found, "paired": bid in paired,
                                 "pairing": self.pair_state(bid),
                                 "area": self.areas.get(bid),
                                 "streaming": bool(self.streams.get(bid) and self.streams[bid].wanted),
                                 "error": self.errors.get(bid, "")} for bid in ids],
                    "lights": [{"ip": d.ip, "name": d.name, "segments": d.segments, "mode": d.mode, "online": d.online}
                               for d in self.devices_by_key.values()],
                    "outside": [L for ls in self.outside.values() for L in ls]}
