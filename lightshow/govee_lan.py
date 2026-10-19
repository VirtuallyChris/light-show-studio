"""Minimal Govee LAN API client (UDP, no cloud).

Protocol summary (documented by Govee):
  * Discovery: multicast JSON to 239.255.255.250:4001, devices answer on UDP 4002.
  * Control:   unicast JSON to <device ip>:4003.
  * Status:    "devStatus" command, device answers on UDP 4002.

"LAN Control" must be switched on for each device in the Govee Home app
(device settings -> LAN Control).
"""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

# Console programs run from a windowless process (the hidden studio launcher)
# would otherwise pop a black window for a moment on Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

MCAST_GROUP = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002

# Segments (separately colorable zones) of models whose count is known, for a new light
# nobody has set a count for yet. Anything else starts at 10 until set in the studio.
# Segments of models that cannot be counted otherwise. The H6821 (meteor lights: 20 hanging tubes of 12
# LEDs) takes every LED in one frame, top of tube 1 first (found 2026-10-18, see razer_leds).
SKU_SEGMENTS = {"H6076": 7, "H6821": 240}
# Models that take frames at a fixed rate whatever their Wi-Fi: the H6821's 240-LED frames ran clean at 15
# a second and skipped now and then at 30.
SKU_FPS = {"H6821": 15}
CONTROL_PORT = 4003
# Light frames are marked as real-time traffic (DSCP 46, "expedited forwarding"), so a Wi-Fi access
# point that honors the mark (WMM) sends them ahead of other traffic, such as a Cast stream. Windows
# drops marks set by ordinary programs; a QoS policy for UDP to port 4003 puts it back (README).
PRIORITY_TOS = 46 << 2


@dataclass
class GoveeDevice:
    ip: str
    device_id: str = ""
    sku: str = ""
    mac: str = ""  # wifi MAC from ARP table, if resolvable
    _sock: socket.socket = field(default=None, repr=False, compare=False)
    _last: tuple = field(default=None, repr=False, compare=False)

    # -- transport --------------------------------------------------------
    def _socket(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            try:
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, PRIORITY_TOS)
            except OSError:
                pass
        return self._sock

    def send(self, cmd: str, data: dict | None = None) -> None:
        payload = {"msg": {"cmd": cmd, "data": data if data is not None else {}}}
        try:
            self._socket().sendto(json.dumps(payload).encode(), (self.ip, CONTROL_PORT))
        except OSError:
            pass  # a dropped UDP frame is not worth crashing a light show over

    # -- commands ---------------------------------------------------------
    def turn(self, on: bool) -> None:
        self.send("turn", {"value": 1 if on else 0})

    def brightness(self, value: int) -> None:
        self.send("brightness", {"value": max(0, min(100, int(value)))})

    def color(self, r: int, g: int, b: int) -> None:
        rgb = (_clamp8(r), _clamp8(g), _clamp8(b))
        self._last = rgb
        self.send("colorwc", {"color": {"r": rgb[0], "g": rgb[1], "b": rgb[2]}, "colorTemInKelvin": 0})

    def color_if_changed(self, r: int, g: int, b: int, threshold: int = 3) -> bool:
        """Send colour only if it moved more than `threshold` on any channel."""
        rgb = (_clamp8(r), _clamp8(g), _clamp8(b))
        if self._last is not None and all(abs(a - b) <= threshold for a, b in zip(rgb, self._last)):
            return False
        self.color(*rgb)
        return True

    def status(self, timeout: float = 1.0) -> dict | None:
        with _listener(timeout) as ls:
            self.send("devStatus")
            for _, msg in _drain(ls, timeout):
                if msg.get("cmd") == "devStatus":
                    return msg.get("data")
        return None

    def white(self, kelvin: int) -> None:
        self.send("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": int(kelvin)})

    # -- real-time per-segment protocol ("Razer Chroma" mode) ---------------
    # Packet: BB <len, two bytes> <cmd> <data...> <xor checksum>. cmd 0xB1 = enable
    # (data: 01/00), cmd 0xB0 = LED data (data: gradient flag, count, RGB*count).
    # The device drops back to its normal effect if no LED packet arrives for
    # about a minute, so the show loop keeps sending.
    def razer(self, enable: bool) -> None:
        self.send("razer", {"pt": _razer_packet(0xB1, bytes([1 if enable else 0]))})

    # The length was long taken for one byte after a fixed 00, which capped a frame at 84 colors (2 + 3*84
    # = 254 bytes); the 00 is the length's high byte, so a frame carries as many colors as its one-byte
    # count allows. Found on the H6821's 240 LEDs (each its own color, one packet); a light with fewer
    # segments gets the same packets as ever (high byte 0).
    MAX_RAZER_LEDS = 255

    def razer_leds(self, colors: list[tuple[int, int, int]], gradient: bool = False) -> None:
        if len(colors) > self.MAX_RAZER_LEDS:
            colors = _resample_colors(colors, self.MAX_RAZER_LEDS)
        data = bytearray([1 if gradient else 0, len(colors)])
        for r, g, b in colors:
            data += bytes((_clamp8(r), _clamp8(g), _clamp8(b)))
        self.send("razer", {"pt": _razer_packet(0xB0, bytes(data))})

    @property
    def label(self) -> str:
        return f"{self.sku or '?'} @ {self.ip}" + (f" [{self.mac}]" if self.mac else "")


def _resample_colors(colors, n):
    """Shrink a colour list to n entries by nearest sampling (keeps the picture, drops resolution)."""
    m = len(colors)
    return [colors[min(m - 1, int((i + 0.5) * m / n))] for i in range(n)]


def _razer_packet(cmd: int, data: bytes) -> str:
    pkt = bytearray([0xBB, (len(data) >> 8) & 0xFF, len(data) & 0xFF, cmd]) + data
    crc = 0
    for b in pkt:
        crc ^= b
    pkt.append(crc)
    return base64.b64encode(bytes(pkt)).decode()


def _clamp8(v) -> int:
    return max(0, min(255, int(round(v))))


class _listener:
    """Context manager yielding a UDP socket bound to the Govee reply port."""

    def __init__(self, timeout: float):
        self.timeout = timeout

    def __enter__(self) -> socket.socket:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", LISTEN_PORT))
        self.sock.settimeout(self.timeout)
        return self.sock

    def __exit__(self, *exc):
        self.sock.close()


def _drain(sock: socket.socket, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sock.settimeout(remaining)
        try:
            raw, addr = sock.recvfrom(8192)
        except socket.timeout:
            return
        except OSError:
            return
        try:
            yield addr[0], json.loads(raw.decode())["msg"]
        except (ValueError, KeyError):
            continue


def statuses(devices: list, timeout: float = 1.0) -> dict:
    """{ip: status} for several lights at once: ask them all, then gather the replies."""
    out: dict = {}
    want = {d.ip for d in devices}
    try:
        with _listener(timeout) as ls:
            for _ in range(2):                              # UDP: ask twice
                for d in devices:
                    d.send("devStatus")
            for ip, msg in _drain(ls, timeout):
                if msg.get("cmd") == "devStatus" and ip in want and ip not in out:
                    out[ip] = msg.get("data")
                    if len(out) == len(want):
                        break
    except OSError:
        pass
    return out


def local_ip() -> str | None:
    """Best guess at the LAN interface address (the default route's source IP).

    On a machine with several interfaces (VPNs, a mapped network share, virtual
    adapters) the OS may send multicast out the wrong one, so discovery pins the
    scan to this interface. Returns None if it cannot be determined.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is sent; this just resolves the route
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def discover(timeout: float = 2.0, interface: str | None = None) -> list[GoveeDevice]:
    """Multicast a scan and collect every device that answers within `timeout`."""
    found: dict[str, GoveeDevice] = {}
    arp = arp_table()
    scan = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    iface = interface or local_ip()
    with _listener(timeout) as ls:
        if iface:
            try:
                ls.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
            except OSError:
                pass
        # Multicast is what Govee documents, but some networks (a VPN adapter on the PC,
        # a router's IGMP snooping) let none of it through. A plain broadcast on the home
        # subnet reaches the lights there, so the scan goes out all three ways.
        targets = [MCAST_GROUP]
        try:
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if iface:
                targets.append(iface.rsplit(".", 1)[0] + ".255")   # the home subnet (a /24, as routers set up)
            targets.append("255.255.255.255")
        except OSError:
            pass
        # Over wifi any of it can be lost; ask twice so a missed packet does not
        # make a light vanish from the list.
        for wait in (min(0.4, timeout / 2), timeout):
            for t in targets:
                try:
                    ls.sendto(scan, (t, SCAN_PORT))
                except OSError:
                    pass
            for ip, msg in _drain(ls, wait):
                _collect(found, ip, msg, arp)
    return sorted(found.values(), key=lambda d: tuple(int(p) for p in d.ip.split(".")))


def _collect(found: dict, ip: str, msg: dict, arp: dict) -> None:
    if msg.get("cmd") != "scan":
        return
    d = msg.get("data", {})
    dev_ip = d.get("ip", ip)
    found[dev_ip] = GoveeDevice(ip=dev_ip, device_id=d.get("device", ""), sku=d.get("sku", ""), mac=arp.get(dev_ip, ""))


_MAC_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:[-:][0-9a-f]{2}){5})", re.I)


def arp_table() -> dict[str, str]:
    """Return {ip: mac} from the OS ARP cache (works on Windows, Linux, macOS)."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for ip, mac in _MAC_RE.findall(out):
        table[ip] = normalize_mac(mac)
    return table


def normalize_mac(mac: str) -> str:
    return mac.replace("-", ":").upper()


def is_mac(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{2}([-:][0-9a-fA-F]{2}){5}", s))


def resolve_targets(selectors: list[str], discovered: list[GoveeDevice]) -> list[GoveeDevice]:
    """Pick devices by IP, wifi MAC, Govee device id, or SKU from the discovered list."""
    if not selectors:
        return list(discovered)
    picked: list[GoveeDevice] = []
    for sel in selectors:
        s = sel.strip()
        match = None
        if is_mac(s):
            want = normalize_mac(s)
            match = next((d for d in discovered if d.mac == want), None)
            if match is None:
                # MAC known to ARP but device did not answer scan: control it blind.
                ip = next((ip for ip, mac in arp_table().items() if mac == want), None)
                if ip:
                    match = GoveeDevice(ip=ip, mac=want)
        else:
            match = next((d for d in discovered if s in (d.ip, d.device_id)), None)
            if match is None and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", s):
                match = GoveeDevice(ip=s)
        if match is None:
            matches = [d for d in discovered if d.sku.lower() == s.lower()]
            picked.extend(m for m in matches if m not in picked)
            if not matches:
                raise LookupError(f"No device matched '{sel}'")
        elif match not in picked:
            picked.append(match)
    return picked
