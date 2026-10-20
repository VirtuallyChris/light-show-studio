"""Local web server for Light Show Studio.

Serves the single-page app, analyses uploaded songs, stores projects, lists
Govee lights, and forwards rendered frames from the browser to the lights
over the LAN real-time protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from aiohttp import web

from .. import govee_lan
from . import webjson
from .analysis import analyze_cached
from .winpaths import local_appdata
from . import audiotrim
from .cast import CastManager, CONTENT_TYPES as CAST_TYPES
from .jam_service import JamService
from .rooms import Rooms, FADE_S
from .review import Review
from .cues import CueRunner, LiveHooks
from .doorbell import Doorbell

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
ROOT = os.path.dirname(os.path.dirname(HERE))
SHOWS_DIR = os.path.join(ROOT, "shows")
CONFIG_PATH = os.path.join(ROOT, "config.json")
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aiff", ".aif", ".opus"}
DEFAULT_LIGHT_MAC = "5C:E7:53:E9:C7:FE"


# config.json is read and saved by several threads (the jam, learn ahead, rooms, the web handlers). Saved whole
# through a temporary file and swapped in, so a reader never sees half of it; and a read that fails on a bad file
# hands back the last good one, not {}: a save after {} wrote an almost empty config (lights, rooms, offsets gone).
_CONFIG_LOCK = threading.RLock()
_CONFIG_GOOD: str | None = None


def load_config() -> dict:
    global _CONFIG_GOOD
    with _CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                text = fh.read()
            cfg = json.loads(text)
            _CONFIG_GOOD = text
            return cfg
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return json.loads(_CONFIG_GOOD) if _CONFIG_GOOD else {}


def save_config(cfg: dict) -> None:
    global _CONFIG_GOOD
    text = json.dumps(cfg, indent=2)
    with _CONFIG_LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        for attempt in range(10):                      # (Windows: a reader holding the file blocks the swap a moment)
            try:
                os.replace(tmp, CONFIG_PATH)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
        _CONFIG_GOOD = text


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\- .()]+", "_", name).strip(" .")
    return name[:80] or "untitled"


@web.middleware
async def no_cache(request, handler):
    """The app is edited live; never let the browser serve a stale script."""
    resp = await handler(request)
    if request.path.startswith("/static") or request.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


_policy_cache: list = [0.0, None]


def light_priority_policy() -> bool | None:
    """Is the Windows QoS policy that lets the lights' priority mark through in place? (None: unknown)"""
    if sys.platform != "win32":
        return None
    if time.monotonic() - _policy_cache[0] < 60:
        return _policy_cache[1]
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-NetQosPolicy | Where-Object { $_.Name -like 'Light Show Studio*' } | Measure-Object).Count"],
                             capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        found = out.isdigit() and int(out) > 0
    except (OSError, subprocess.TimeoutExpired):
        found = None
    _policy_cache[:] = [time.monotonic(), found]
    return found


def _ease(k: float) -> float:
    k = max(0.0, min(1.0, k))
    return k * k * (3 - 2 * k)


def _mix_toward(flat: list, rgb: tuple, k: float) -> list:
    """A frame (r, g, b per segment) moved k of the way toward one color."""
    if k <= 0:
        return flat
    return [int(round(v + (rgb[i % 3] - v) * k)) for i, v in enumerate(flat or [0, 0, 0])]


def kelvin_rgb(k: float) -> tuple[int, int, int]:
    """A white's color temperature as RGB (the usual approximation of a black body)."""
    import math
    t = max(1000.0, min(40000.0, float(k))) / 100.0
    r = 255.0 if t <= 66 else 329.698727446 * ((t - 60) ** -0.1332047592)
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * ((t - 60) ** -0.0755148492)
    b = 255.0 if t >= 66 else (0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
    return tuple(int(max(0, min(255, round(c)))) for c in (r, g, b))


def _big_change(new, old) -> bool:
    """A frame that changes most of a light a lot from the last one sent to it: a flash on or off, a slam
    to a new color (half its segments moving by 300 or more of 765 across red, green and blue)."""
    if not new or not old or len(new) != len(old):
        return False
    n = len(new) // 3
    moved = sum(1 for i in range(0, n * 3, 3) if abs(new[i] - old[i]) + abs(new[i + 1] - old[i + 1]) + abs(new[i + 2] - old[i + 2]) >= 300)
    return moved * 2 >= n


class LightOutput:
    """Holds the currently selected lights and pushes frames to them."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled                          # False: a test copy that must never touch the lights
        self.saved: dict = {}                            # ip -> how the light was before the show took it over
        # ip -> (how it was, when it was handed back). Ticked again soon after, a light is not asked
        # how it is: it may still be at the show's 100 %, or the restore may still be on its way.
        self.handed_back: dict = {}
        self.devices: list[govee_lan.GoveeDevice] = []
        self.active = False
        self.last_enable = 0.0
        self.last_frame = 0.0
        self.frames = 0
        self.sent = 0                                   # frames sent to at least one light (after the caps)
        self.last_sent = 0.0
        # Each light's own frame rate (set from its own Wi-Fi by link_health), and when it last got one;
        # a light with none yet uses max_fps.
        self.fps_by_ip: dict[str, float] = {}
        self.last_sent_ip: dict[str, float] = {}
        self.sent_by_ip: dict[str, int] = {}
        # Most frames a second sent to the lights. The page draws about 60; Govee lights listen over
        # 2.4 GHz Wi-Fi, where fewer packets can mean less waiting in the air. 0 means no cap.
        self.max_fps = float(load_config().get("light_max_fps", 0) or 0)
        self.errors = 0
        self.last_error = ""
        self.dark_since = None      # when the stream went all-black (None while lit)
        self.last_rearm = 0.0
        # Rooms switched off or on fade over a few seconds: a light leaving blends from the show to how
        # it was before (then is handed back); a light arriving blends from how it was into the show.
        self.leaving: dict = {}     # ip -> {"dev", "t0", "dur", "target"}
        self.arriving: dict = {}    # ip -> {"t0", "dur", "target"}
        self.last_px: dict = {}     # ip -> the last frame it was sent

    # -- how a light was before the show: asked when the show takes it, and kept on disk ------------
    @staticmethod
    def _key(d) -> str:
        return getattr(d, "mac", "") or getattr(d, "device_id", "") or d.ip

    def remembered(self, d) -> dict:
        """The last state this light was found in before a show took it (kept in config.json), for a
        light that did not answer when asked this time."""
        return dict(load_config().get("light_states", {}).get(self._key(d)) or {})

    def _remember(self, states: list) -> None:
        """Keep (light, state) pairs on disk: a light object cannot be a dictionary key."""
        if not states:
            return
        cfg = load_config()
        keep = cfg.setdefault("light_states", {})
        for d, st in states:
            keep[self._key(d)] = {k: st[k] for k in ("onOff", "brightness", "color", "colorTemInKelvin") if k in st}
        try:
            save_config(cfg)
        except OSError:
            pass

    def target(self, d) -> tuple[int, int, int]:
        """The color a light shows when it is handed back, as a real-time frame would show it."""
        was = self.saved.get(d.ip) or self.remembered(d)
        if not was or not was.get("onOff", 1):
            return (0, 0, 0)
        k = int(was.get("colorTemInKelvin") or 0)
        col = was.get("color") or {}
        rgb = kelvin_rgb(k) if k else (int(col.get("r", 255)), int(col.get("g", 255)), int(col.get("b", 255)))
        b = max(0.0, min(1.0, float(was.get("brightness", 100)) / 100.0))
        return tuple(int(round(c * b)) for c in rgb)

    def select(self, ips: list[str], known: list[govee_lan.GoveeDevice]) -> tuple[list, list]:
        """Change which lights the show drives. Returns (dropped, added) so the caller can hand the
        dropped ones back and, mid-show, take the added ones over (both wait on the network)."""
        by_ip = {d.ip: d for d in known}
        old = {d.ip: d for d in self.devices}
        self.devices = [old.get(ip) or by_ip.get(ip) or govee_lan.GoveeDevice(ip=ip) for ip in ips]
        new = {d.ip for d in self.devices}
        for ip in list(self.arriving):
            if ip not in new:
                self.arriving.pop(ip, None)
        return [d for ip, d in old.items() if ip not in new], [d for d in self.devices if d.ip not in old]

    def reselect(self, dropped: list, added: list) -> None:
        """After select(): an unticked light goes back to how it was, exactly as when the studio closes;
        a light ticked while a show is running is taken over like the rest."""
        if not self.enabled:
            return
        if dropped:
            # every light the show was driving goes back, even one whose state could not be read when
            # the show took it (it still leaves real-time mode, and gets its state from last time)
            self.restore([d for d in dropped if d.ip in self.saved or self.active])
        if added and self.active:
            self._take_over(added)

    # -- rooms switched off and on: a few seconds' fade either way --------------------------------------
    @staticmethod
    def _progress(f: dict, now: float) -> float:
        return max(0.0, min(1.0, (now - f["t0"]) / max(1e-3, f["dur"])))

    def leave(self, devs: list, secs: float) -> None:
        """These lights (already out of the selection) fade from the show to how they were."""
        now = time.monotonic()
        for d in devs:
            a = self.arriving.pop(d.ip, None)
            k0 = 1 - self._progress(a, now) if a else 0.0          # turned around mid-fade: from where it is
            self.leaving[d.ip] = {"dev": d, "t0": now - k0 * secs, "dur": secs, "target": self.target(d)}

    def arrive(self, devs: list, secs: float) -> None:
        """These lights (just added to the selection, mid-show) fade from how they are into the show."""
        if not self.enabled or not devs:
            return
        back = {d.ip: self.leaving.pop(d.ip) for d in devs if d.ip in self.leaving}   # still in the show's hands
        fresh = [d for d in devs if d.ip not in back]
        if fresh and self.active:
            self._take_over(fresh)
        now = time.monotonic()
        for d in devs:
            lv = back.get(d.ip)
            k0 = 1 - self._progress(lv, now) if lv else 0.0          # how far into the show it still was
            self.arriving[d.ip] = {"t0": now - k0 * secs, "dur": secs, "target": self.target(d)}

    def finish_leaving(self) -> None:
        """Hand back the lights whose fade has run its course."""
        now = time.monotonic()
        done = [ip for ip, f in self.leaving.items() if self._progress(f, now) >= 1]
        devs = [self.leaving.pop(ip)["dev"] for ip in done]
        if devs and self.enabled:
            self.restore(devs)

    def start(self) -> None:
        """Take the lights over for a show: remember how each one was, then full brightness, real-time mode."""
        if not self.enabled:
            return
        self._take_over(self.devices)
        self.last_enable = time.monotonic()
        self.active = True

    REUSE_S = 60.0          # a light handed back this recently keeps the state it was first saved with

    def _take_over(self, devices: list) -> None:
        fresh = [d for d in devices if d.ip not in self.saved]
        now = time.monotonic()
        ask = []
        for d in fresh:
            back = self.handed_back.get(d.ip)
            if back and now - back[1] < self.REUSE_S:
                self.saved[d.ip] = back[0]
            else:
                ask.append(d)
        if ask:
            got = govee_lan.statuses(ask, timeout=1.0)
            missing = [d for d in ask if d.ip not in got]
            if missing:                                      # UDP: one more try for the quiet ones
                got.update(govee_lan.statuses(missing, timeout=1.0))
            for d in ask:
                if d.ip in got:
                    self.saved[d.ip] = got[d.ip]
                elif d.ip in self.handed_back:               # never answered: the last known state beats none
                    self.saved[d.ip] = self.handed_back[d.ip][0]
                elif self.remembered(d):                      # or how it was the last time a show took it
                    self.saved[d.ip] = self.remembered(d)
            self._remember([(d, got[d.ip]) for d in ask if d.ip in got])
        for _ in range(3):
            for d in devices:
                d.turn(True)
                d.brightness(100)
            time.sleep(0.12)
        for _ in range(3):
            for d in devices:
                d.razer(True)
            time.sleep(0.08)

    def restore(self, only: list | None = None) -> None:
        """Hand each light back as it was before the show took it over.

        Leaving real-time mode returns a Govee light to what it was doing before (a scene
        included), but at the show's 100 % brightness: so its own brightness goes first, while the
        show still has it (a light that was off is switched off), and only then does it leave
        real-time mode, coming back in its own color at its own brightness. Then its power and
        brightness again, and its color only if it did not come back to the one it had, so a scene is
        not painted over for nothing.
        """
        if not self.enabled:
            return
        if only is not None:                                 # just these lights (unticked, or their room switched off)
            devs = only
        else:
            if not self.saved and not self.leaving:
                return
            devs = [d for d in self.devices if d.ip in self.saved] or [govee_lan.GoveeDevice(ip=ip) for ip in self.saved]
            devs += [f["dev"] for f in self.leaving.values() if f["dev"] not in devs]
            self.leaving.clear()
            self.arriving.clear()
        if not devs:
            return
        for d in devs:                                       # a light whose state was never read: as it was last time
            if not self.saved.get(d.ip) and self.remembered(d):
                self.saved[d.ip] = self.remembered(d)
        for _ in range(2):                                   # brightness first: no flash at 100 % on the way out
            for d in devs:
                was = self.saved.get(d.ip) or {}
                if was and not was.get("onOff", 1):
                    d.turn(False)
                elif was.get("brightness") is not None:
                    d.brightness(int(was["brightness"]))
            time.sleep(0.08)
        for _ in range(3):
            for d in devs:
                d.razer(False)
            time.sleep(0.08)
        time.sleep(0.4)
        now = govee_lan.statuses(devs, timeout=0.8)
        for _ in range(2):                                   # UDP: say it twice
            for d in devs:
                was = self.saved.get(d.ip) or {}
                if not was:
                    continue
                col, k = was.get("color") or {}, int(was.get("colorTemInKelvin") or 0)
                cur = (now.get(d.ip) or {}).get("color") or {}
                if was.get("onOff", 1):
                    d.turn(True)
                    if k:
                        d.white(k)
                    elif col and any(abs(int(col.get(c, 0)) - int(cur.get(c, -99))) > 10 for c in "rgb"):
                        d.color(int(col.get("r", 0)), int(col.get("g", 0)), int(col.get("b", 0)))
                    if was.get("brightness") is not None:
                        d.brightness(int(was["brightness"]))
                else:
                    d.turn(False)
            time.sleep(0.15)
        self._verify(devs)
        t = time.monotonic()
        for d in devs:
            if self.saved.get(d.ip):
                self.handed_back[d.ip] = (self.saved[d.ip], t)
        if only is not None:
            for d in devs:
                self.saved.pop(d.ip, None)
        else:
            self.saved.clear()

    def _verify(self, devs: list) -> None:
        """Ask each light how it is now, and send power and brightness again to any that did not take
        it (a lost packet, or a light still busy leaving real-time mode). A light that does not
        answer is left alone: there is no telling."""
        for _ in range(2):
            time.sleep(0.3)
            now = govee_lan.statuses(devs, timeout=0.8)
            wrong = []
            for d in devs:
                was, cur = self.saved.get(d.ip) or {}, now.get(d.ip)
                if not was or not cur:
                    continue
                on = bool(was.get("onOff", 1))
                if bool(cur.get("onOff", 1)) != on or (on and was.get("brightness") is not None
                                                        and abs(int(cur.get("brightness", -99)) - int(was["brightness"])) > 2):
                    wrong.append((d, was, on))
            if not wrong:
                return
            for _ in range(2):
                for d, was, on in wrong:
                    d.turn(on)
                    if on and was.get("brightness") is not None:
                        d.brightness(int(was["brightness"]))
                time.sleep(0.15)

    def stop(self, off: bool = False) -> None:
        if not self.enabled:
            return
        if self.saved and not off:
            self.restore()
            self.active = False
            return
        for d in self.devices:
            d.razer(False)
            if off:
                d.turn(False)
        self.active = False

    def full_brightness(self) -> int:
        """Every light the show drives at 100% brightness, without asking how it is: sent between songs (a
        home automation dimming a strip would dim the show with it, and a brightness change mid-song could
        show). Lights fading back out of the show are left alone. Returns how many were set."""
        if not (self.enabled and self.active and self.devices):
            return 0
        devs = [d for d in self.devices if d.ip not in self.leaving]
        for _ in range(2):                                   # UDP: say it twice
            for d in devs:
                d.brightness(100)
            time.sleep(0.1)
        return len(devs)

    def _rearm(self, now: float, wake: bool = False) -> None:
        self.last_rearm = now
        for d in self.devices + [f["dev"] for f in self.leaving.values()]:
            if wake:
                d.turn(True)
            d.razer(True)

    def frame(self, frames: list[list[int]], ips: list[str] | None = None) -> None:
        if not self.active:
            return
        # each frame belongs to the light the page drew it for (by address), so a light leaving or
        # joining mid-show can never shift the frames onto the wrong lights
        by_ip = dict(zip(ips, frames)) if ips else dict(zip([d.ip for d in self.devices], frames))
        now = time.monotonic()
        # Re-enable real-time mode after a pause in frames (the light leaves it
        # after ~60 s idle). Re-enabling mid-stream blanks the light for one
        # frame, so while the picture is lit we never do it...
        if self.last_frame and now - self.last_frame > 5.0:
            self._rearm(now)
        # ...but a long run of black frames also makes the light drop out of
        # real-time mode (or doze), and then it stays dark when the show comes
        # back. While the frames are black a blink is invisible, so re-arm
        # every couple of seconds during darkness and once more on the first
        # lit frame after it.
        lit = any(v > 6 for flat in frames for v in flat)
        if not lit:
            if self.dark_since is None:
                self.dark_since = now
            elif now - self.dark_since > 1.5 and now - self.last_rearm > 2.0:
                self._rearm(now, wake=True)
        else:
            if self.dark_since is not None and now - self.dark_since > 1.5:
                self._rearm(now, wake=True)
            self.dark_since = None
        self.last_frame = now
        self.frames += 1
        def due(ip: str) -> bool:
            """Is this light ready for another frame at its own rate? (Over it, this one is skipped.) A big
            change (a strobe flashing or going black, the room slamming to a new color) goes out at once,
            the moment the page drew it, however soon after the last frame (a fortieth of a second at the
            least): at 15 frames a second a flash waited up to 67 ms for its turn, smeared and out of step
            with the lights on other rates. The user's idea."""
            cap = self.fps_by_ip.get(ip) or self.max_fps
            since = now - self.last_sent_ip.get(ip, 0.0)
            if not (cap and cap > 0 and since < 0.9 / cap):
                return True
            return since >= 0.025 and _big_change(by_ip.get(ip), self.last_px.get(ip))
        out = []
        for d in self.devices:
            flat = by_ip.get(d.ip)
            if flat is None or not due(d.ip):
                continue
            a = self.arriving.get(d.ip)
            if a:
                k = self._progress(a, now)
                if k >= 1:
                    self.arriving.pop(d.ip, None)
                else:
                    flat = _mix_toward(flat, a["target"], 1 - _ease(k))
            out.append((d, flat))
        for ip, f in list(self.leaving.items()):
            if not due(ip):
                continue
            flat = by_ip.get(ip) or self.last_px.get(ip) or [0, 0, 0]
            out.append((f["dev"], _mix_toward(flat, f["target"], _ease(self._progress(f, now)))))
        if out:
            self.last_sent = now
            self.sent += 1
        for d, flat in out:
            self.last_px[d.ip] = flat
            self.last_sent_ip[d.ip] = now
            self.sent_by_ip[d.ip] = self.sent_by_ip.get(d.ip, 0) + 1
            try:
                cols = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)]
                if cols:
                    d.razer_leds(cols)
            except Exception as e:  # noqa: BLE001 - keep the show running, remember the fault
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"


class Studio:
    def __init__(self, port: int = 8765, quit_when_closed: bool = False, lights: bool = True, player: bool = True):
        self.read_only = not lights                      # a test copy (--no-lights) never writes a show
        self.port = port
        self.quit_when_closed = quit_when_closed
        self.ws_clients = 0
        self._watch_task = None
        self.last_client = 0.0
        self.known: list[govee_lan.GoveeDevice] = []
        self.scan_lock = asyncio.Lock()                  # one scan at a time: they all listen on the same UDP port
        self.output = LightOutput(enabled=lights)
        # The frame rate follows the Wi-Fi: pings while a show plays, between 15 and 60 frames a second
        # (light_fps_auto false in config.json keeps the fixed light_max_fps instead).
        self.link = None
        if lights and load_config().get("light_fps_auto", True):
            from .link_health import LinkMonitor
            # each light between 15 and light_fps_ceiling frames a second (30 unless set in config.json: every
            # strip that climbed to 40 choked within minutes, replies at 200 ms, and lagged; at 50 and 60 worse)
            ceiling = int(load_config().get("light_fps_ceiling", 30))
            # each kind of light where it can keep up: a light pole (a dozen segments or fewer) at 15 frames a
            # second, a strip at 30 (the ceiling), down to 15 when the Wi-Fi struggles
            self.link = LinkMonitor(self.output, 15, ceiling,
                                    range_for=lambda ip: (15, 15, 15) if self._light_segments(ip) <= 12 or self._light_fixed_fps(ip)
                                    else (15, ceiling, 30))
            self.output.max_fps = self.link.governor.fps
            self.link.start()
        self.cast = CastManager(govee_lan.local_ip())
        self.requested: list[str] = []                   # the lights ticked in the studio (by address)
        self.rooms = Rooms(load_config, save_config, self.cast, touch=lights)   # a test copy never touches a speaker
        self.cast.on_release = self.rooms.unmute_all             # casting stops: the speakers it muted play again
        # cues play for real; a test copy (no lights) only ever rehearses them, so it never makes a sound
        self.cues = CueRunner(load_config, save_config, self._show_length,
                              live=(lambda: LiveHooks(self.cast, self.jam, self._show_song, self._show_length, pcm=self._cue_pcm)) if lights else None)
        # the Reolink doorbell rings a cue; a test copy (no lights) never connects to it
        self.doorbell = Doorbell(load_config, save_config, on_ring=lambda cid: self.cues.run(cid))
        self.doorbell_live = lights
        self.app = web.Application(client_max_size=512 * 1024 * 1024, middlewares=[no_cache])
        self.jam = JamService(self, SHOWS_DIR, load_config, save_config, govee_lan.local_ip)
        self.review = Review(self.jam.library, read_only=self.read_only)   # learned songs' sections, set right by ear
        self.jam.player_enabled = player
        if lights:                                       # the real studio (a test copy loads no models of its own)
            self.jam.warm_ahead()
        self.app.add_routes([
            web.get("/", self.index),
            web.get("/api/devices", self.devices),
            web.get("/api/output", self.output_status),
            web.post("/api/output/fps", self.set_output_fps),
            web.post("/api/devices/segments", self.set_segments),
            web.post("/api/devices/flip", self.set_flip),
            web.post("/api/devices/order", self.set_order),
            web.post("/api/devices/name", self.set_name),
            web.post("/api/devices/selected", self.set_selected),
            web.get("/api/rooms", self.rooms_get),
            web.post("/api/rooms", self.rooms_set),
            web.post("/api/rooms/on", self.rooms_on),
            web.get("/api/cues", self.cues_get),
            web.post("/api/cues", self.cues_set),
            web.post("/api/cues/run", self.cues_run),
            web.post("/api/cues/stop", self.cues_stop),
            web.get("/api/cues/now", self.cues_now),
            web.get("/api/doorbell", self.doorbell_get),
            web.post("/api/doorbell", self.doorbell_set),
            web.post("/api/song", self.upload_song),
            web.post("/api/open", self.open_path),
            web.get("/api/audio/{project}", self.audio),
            web.get("/api/projects", self.list_projects),
            web.get("/api/projects/{name}", self.load_project),
            web.post("/api/projects/{name}", self.save_project),
            web.delete("/api/projects/{name}", self.delete_project),
            web.get("/ws", self.websocket),
            web.post("/api/closing", self.closing),
            web.get("/api/settings", self.settings),
            web.get("/api/models", self.models_status),
            web.post("/api/settings/light_offset", self.set_light_offset),
            web.get("/api/cast/devices", self.cast_devices),
            web.post("/api/cast/connect", self.cast_connect),
            web.post("/api/cast/disconnect", self.cast_disconnect),
            web.post("/api/cast/play", self.cast_play),
            web.post("/api/cast/pause", self.cast_pause),
            web.post("/api/cast/resume", self.cast_resume),
            web.post("/api/cast/seek", self.cast_seek),
            web.post("/api/cast/volume", self.cast_volume),
            web.get("/api/cast/status", self.cast_status),
            web.post("/api/cast/synccheck", self.cast_sync_check),
            *self.jam.routes(),
            *self.review.routes(),
            web.static("/static", STATIC),
        ])
        if self.quit_when_closed:
            self.app.on_startup.append(self._start_quit_watchdog)
        self.app.on_startup.append(self._rooms_at_start)
        self.app.on_startup.append(self._doorbell_at_start)
        self.app.on_shutdown.append(self._close_player)

    async def _close_player(self, app):
        """Any orderly exit: Edge's sound goes back to the default output, the hidden player closes,
        and every speaker a room muted plays again."""
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.jam.close_player)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.rooms.unmute_all)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.doorbell.close()
        except Exception:  # noqa: BLE001
            pass

    # -- rooms ---------------------------------------------------------------------------
    async def _rooms_at_start(self, app):
        """A room left off last time stays off: its speakers go quiet again (in the background). The
        cues' shows are analysed now too, so a tapped cue never waits for it."""
        asyncio.get_running_loop().run_in_executor(None, self._warm_cues)
        if any(not r["on"] and r["speakers"] for r in self.rooms.list()):
            asyncio.get_running_loop().run_in_executor(None, self.rooms.sync_speakers)

    # -- cues ----------------------------------------------------------------------------
    def _show_length(self, name: str) -> float:
        pdir = os.path.join(SHOWS_DIR, safe_name(name))
        song = [f for f in os.listdir(pdir) if f.startswith("song.")] if os.path.isdir(pdir) else []
        if not song:
            return 0.0
        path = os.path.join(pdir, song[0])
        try:
            key = (path, os.path.getmtime(path))
            cache = self.__dict__.setdefault("_lengths", {})   # asked every second by the Jam panel: read each song once
            if key not in cache:
                cache[key] = float(audiotrim.total_duration(path))
            return cache[key]
        except Exception:  # noqa: BLE001
            return 0.0

    def _show_song(self, name: str) -> tuple[str, str]:
        """A saved show's song: the address a speaker fetches it from, and the file."""
        name = safe_name(name)
        pdir = os.path.join(SHOWS_DIR, name)
        song = [f for f in os.listdir(pdir) if f.startswith("song.")] if os.path.isdir(pdir) else []
        if not song:
            raise FileNotFoundError(f"{name} has no song")
        ip = govee_lan.local_ip() or "127.0.0.1"
        return f"http://{ip}:{self.port}/api/audio/{name}", os.path.join(pdir, song[0])

    def _cue_shows(self) -> list[dict]:
        out = []
        if os.path.isdir(SHOWS_DIR):
            for name in sorted(os.listdir(SHOWS_DIR), key=str.lower):
                if name.startswith("_") or not os.path.isfile(os.path.join(SHOWS_DIR, name, "project.json")):
                    continue
                out.append({"name": name, "length": round(self._show_length(name), 1)})
        return out

    def _warm_cues(self) -> None:
        """Analyse each cue's show ahead of time (cached), and decode its song into memory, so a tapped
        cue's song and lights start at once."""
        for c in self.cues.list():
            if not c["show"]:
                continue
            try:
                _, path = self._show_song(c["show"])
                analyze_cached(path, os.path.join(os.path.dirname(path), "cache"))
                self._cue_pcm(c["show"])
            except Exception:  # noqa: BLE001 - a missing show is shown on the Party Hub, not here
                pass

    CUE_PCM_MAX_S = 600                                   # a longer song is not kept in memory: it plays the old way
    CUE_SR = 44100                                        # the jam's own rate (its relay)

    def _cue_pcm(self, name: str):
        """A cue's song decoded to the jam's rate, as int16 frames, kept in memory (by file and date) so a
        cue plays it inside the jam's stream at once. None if it cannot be (missing, too long)."""
        try:
            _, path = self._show_song(name)
            key = (path, os.path.getmtime(path))
        except Exception:  # noqa: BLE001
            return None
        cache = self.__dict__.setdefault("_pcm", {})
        if key not in cache:
            if self._show_length(name) > self.CUE_PCM_MAX_S:
                return None
            import librosa
            import numpy as np
            y, _ = librosa.load(path, sr=self.CUE_SR, mono=False)
            y = np.atleast_2d(y)
            y = np.vstack([y, y]) if y.shape[0] == 1 else y[:2]
            cache[key] = (np.ascontiguousarray((np.clip(y.T, -1.0, 1.0) * 32767).astype(np.int16)), self.CUE_SR)
            for k in [k for k in cache if k[0] == path and k != key]:
                cache.pop(k)                              # an older copy of the same file
        return cache[key]

    async def cues_get(self, request):
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        shows = await asyncio.get_running_loop().run_in_executor(None, self._cue_shows)
        return web.json_response({"cues": self.cues.list(), "shows": shows, "running": self.cues.status()})

    async def cues_set(self, request):
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        cues = self.cues.replace((await request.json()).get("cues", []))
        asyncio.get_running_loop().run_in_executor(None, self._warm_cues)
        return web.json_response({"cues": cues})

    async def cues_run(self, request):
        """Run a cue: the music fades out, a saved show plays, the music comes back."""
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        try:
            return web.json_response(self.cues.run(str((await request.json()).get("id", ""))))
        except (LookupError, ValueError, RuntimeError) as e:
            return web.json_response({"error": str(e)}, status=400)

    async def cues_now(self, request):
        """The cue running now (asked often by the studio page, so a cue's lights start with its song)."""
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        return web.json_response(self.jam._room_cue())

    async def cues_stop(self, request):
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        return web.json_response(self.cues.stop())

    # -- the doorbell --------------------------------------------------------------------
    async def _doorbell_at_start(self, app):
        if self.doorbell_live:
            self.doorbell.start()

    async def doorbell_get(self, request):
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        return web.json_response({**self.doorbell.status(), "live": self.doorbell_live})

    async def doorbell_set(self, request):
        """The doorbell's address, user, password (kept if left empty), which cue it rings, on or off."""
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        self.doorbell.save(await request.json())
        if self.doorbell_live:
            self.doorbell.start()
        return web.json_response({**self.doorbell.status(), "live": self.doorbell_live})

    def _off_ips(self) -> set[str]:
        off = self.rooms.off_light_keys()
        return {d.ip for d in self.known if {d.mac, d.device_id, d.ip} & off}

    async def apply_rooms(self) -> None:
        """Bring the show in line with the rooms: lights of a room switched off fade back to how they were,
        lights of a room switched on fade into the show, speakers mute or unmute."""
        loop = asyncio.get_running_loop()
        off = self._off_ips()
        dropped, added = self.output.select([ip for ip in self.requested if ip not in off], self.known)
        if self.output.enabled and self.output.active:
            if dropped:
                self.output.leave(dropped, FADE_S)
                loop.call_later(FADE_S + 0.1, lambda: asyncio.ensure_future(self._finish_leaving()))
            if added:
                async with self.scan_lock:
                    await loop.run_in_executor(None, self.output.arrive, added, FADE_S)
        elif dropped:
            async with self.scan_lock:
                await loop.run_in_executor(None, self.output.reselect, dropped, [])
        loop.run_in_executor(None, self.rooms.sync_speakers)

    async def _finish_leaving(self) -> None:
        async with self.scan_lock:
            await asyncio.get_running_loop().run_in_executor(None, self.output.finish_leaving)

    def _rooms_doc(self, devices: bool) -> dict:
        doc = {"rooms": self.rooms.list(), "fade_s": FADE_S, "muted": self.rooms.muted()}
        if devices:
            names = load_config().get("light_names", {})
            doc["lights"] = [{"key": d.mac or d.device_id or d.ip, "ip": d.ip, "sku": d.sku,
                              "name": names.get(d.mac) or names.get(d.device_id) or names.get(d.ip) or ""} for d in self.known]
            doc["speakers"] = sorted({c.cast_info.friendly_name for c in self.cast.casts if c.cast_info.cast_type != "group"}
                                     | {s for r in doc["rooms"] for s in r["speakers"]}, key=str.lower)
        return doc

    async def rooms_get(self, request):
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        devices = request.query.get("devices") == "1"
        if devices and not self.cast.casts:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.cast.discover, 4.0)
            except Exception:  # noqa: BLE001 - no speakers found is not an error
                pass
        return web.json_response(self._rooms_doc(devices))

    async def rooms_set(self, request):
        """Replace the rooms (names, lights, speakers, on or off), from the studio or the Party Hub."""
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        self.rooms.replace((await request.json()).get("rooms", []))
        await self.apply_rooms()
        return web.json_response(self._rooms_doc(request.query.get("devices") == "1"))

    async def rooms_on(self, request):
        """Switch one room on or off."""
        if not JamService._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        self.rooms.set_on(str(body.get("id", "")), bool(body.get("on")))
        await self.apply_rooms()
        return web.json_response(self._rooms_doc(False))

    # The studio quits when its window is closed, like a desktop app would. The page's
    # WebSocket is the signal: relying on the browser process is unreliable because a
    # Chromium launcher hands the URL to an already-running instance and exits at once.
    async def _start_quit_watchdog(self, app):
        async def watch():
            seen = False
            while True:
                await asyncio.sleep(2)
                if self.ws_clients > 0:
                    seen, self.last_client = True, time.monotonic()
                elif seen and time.monotonic() - self.last_client > 8:
                    await self._quit("Studio window closed. Stopping.")
        # keep a reference: a bare create_task can be garbage collected mid-flight
        self._watch_task = asyncio.create_task(watch())

    async def _quit(self, why: str) -> None:
        """Close down like an app: the speakers stop at once (a Cast speaker would otherwise play out
        the 20 s it had buffered), the hidden Spotify player goes, then the studio exits."""
        print(why)
        loop = asyncio.get_running_loop()

        def shut():
            try:                                          # the music first: held at once, then the speakers stopped
                self.jam.hush()                           # (a Cast speaker would play out what it had buffered)
                if self.jam.engine is not None:
                    self.jam.engine.stop()
                self.jam._stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.output.stop()                        # the lights go back to how they were
            except Exception:  # noqa: BLE001
                pass
            try:
                if self.cast.cast is not None:
                    self.cast.disconnect()                # stops the speaker's playback, then lets go of it
            except Exception:  # noqa: BLE001
                pass
            try:
                self.jam.close_player()
            except Exception:  # noqa: BLE001
                pass
            try:                                          # the models and the learn-ahead process, at once
                from .model_worker import kill_all
                kill_all()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(loop.run_in_executor(None, shut), timeout=6)
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    async def closing(self, request):
        """The page is going away (its window closed, or it is reloading). If no page is back within
        a couple of seconds it was a close: quit now rather than after the watchdog's 8 s."""
        if request.remote not in ("127.0.0.1", "::1") or not self.quit_when_closed:
            return web.json_response({"ok": True})

        loop = asyncio.get_running_loop()
        # the music stops the moment the window goes, not after the wait below (or the shutting down)
        hushed = loop.run_in_executor(None, self.jam.hush)

        async def later():
            await asyncio.sleep(2.5)
            if self.ws_clients == 0:
                await self._quit("Studio window closed. Stopping.")
            else:                                         # a reload: the page is back, the room carries on
                try:
                    await hushed
                    await loop.run_in_executor(None, self.jam.unhush)
                except Exception:  # noqa: BLE001
                    pass
        self._closing_task = asyncio.create_task(later())
        return web.json_response({"ok": True})

    async def models_status(self, request):
        """The models loading as the studio opens: {"models": {name: waiting|loading|warming|ready|failed}},
        or {"models": null} when there are none to wait for (not installed, learn ahead off, a test copy)."""
        from .model_worker import HOST
        loop = asyncio.get_running_loop()
        expected = (not self.read_only) and self.jam._fetch_on()       # models to wait for at all
        got = await loop.run_in_executor(None, HOST.status) if expected else None
        return web.json_response({"models": got, "expected": expected})

    # -- pages ------------------------------------------------------------------
    async def index(self, request):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    # -- lights -----------------------------------------------------------------
    async def devices(self, request):
        """Lights that answered the scan, merged with every light seen before.

        Multicast over wifi misses a busy light now and then; a light that has
        answered in the past stays in the list (marked offline) so a show never
        loses its target because of one dropped scan.
        """
        async with self.scan_lock:
            return await self._devices(request.query.get("rescan") == "1")

    async def _devices(self, rescan: bool):
        loop = asyncio.get_running_loop()
        if rescan or not self.known:
            self.known = await loop.run_in_executor(None, govee_lan.discover, 2.0)
        cfg = load_config()
        remembered = cfg.get("known_devices", {})
        before = json.dumps(remembered, sort_keys=True)
        # (arp is a subprocess with a 5 s timeout: in a worker thread, not on the event loop)
        arp = await loop.run_in_executor(None, govee_lan.arp_table) if any(not d.mac for d in self.known) else {}
        for d in self.known:
            # The MAC comes from the ARP cache, which is empty now and then. A blank
            # must never replace a MAC we already know: the segment count is keyed by
            # it, and losing it silently drops a 50 LED strip to the 10 zone default.
            d.mac = d.mac or arp.get(d.ip, "") or remembered.get(d.ip, {}).get("mac", "")
            remembered[d.ip] = {"mac": d.mac, "sku": d.sku or remembered.get(d.ip, {}).get("sku", ""),
                                "device_id": d.device_id or remembered.get(d.ip, {}).get("device_id", "")}
        if self.known and json.dumps(remembered, sort_keys=True) != before:   # the page scans every half minute: write only news
            cfg["known_devices"] = remembered
            try:
                save_config(cfg)
            except OSError:
                pass
        online = {d.ip for d in self.known}
        missing = []
        for ip, info in remembered.items():
            if ip not in online:
                d = govee_lan.GoveeDevice(ip=ip, device_id=info.get("device_id", ""), sku=info.get("sku", ""), mac=info.get("mac", ""))
                self.known.append(d)
                missing.append(d)
        # A light that ignores the multicast scan usually still answers when asked
        # directly, so ask before calling it offline. One at a time: every probe
        # listens on the same UDP port, so two at once steal each other's replies.
        def probe_all():
            return [d.ip for d in missing if d.status(timeout=0.8)]
        if missing:
            online |= set(await loop.run_in_executor(None, probe_all))
        self.known.sort(key=lambda d: tuple(int(p) for p in d.ip.split(".")))
        seg = cfg.get("segments", {})
        # Govee keeps the names given in its app in its cloud; the LAN protocol never
        # reports them, and the lights announce no hostname. So names are given here,
        # keyed like segment counts: by MAC, with the light's own device id as a fallback.
        names = cfg.get("light_names", {})
        flipped = cfg.get("flipped_lights", {})             # lights mounted the other way round, by MAC
        # Which lights the shows play on is a property of the room, not of a song: one
        # selection, kept here, that every show, the preview and Jam mode all use.
        chosen = cfg.get("selected_lights")
        out = []
        for d in self.known:
            if not d.mac:   # ARP may not have the entry yet; use what we remembered
                d.mac = remembered.get(d.ip, {}).get("mac", "")
            n = seg.get(d.mac) or seg.get(d.ip) or seg.get(d.device_id) or seg.get(d.sku) or govee_lan.SKU_SEGMENTS.get(d.sku) or 10
            name = names.get(d.mac) or names.get(d.device_id) or names.get(d.ip) or ""
            flip = bool(flipped.get(d.mac) or flipped.get(d.device_id) or flipped.get(d.ip))
            out.append({"ip": d.ip, "mac": d.mac, "device_id": d.device_id, "name": name, "sku": d.sku, "segments": int(n), "online": d.ip in online, "flip": flip,
                        "default": d.mac.upper() == DEFAULT_LIGHT_MAC,
                        "on": (d.mac in chosen or d.device_id in chosen) if chosen is not None else d.mac.upper() == DEFAULT_LIGHT_MAC})
        # in the order they stand around the room (set by dragging them in the studio); new ones last
        order = cfg.get("light_order", [])
        rank = lambda d: next((order.index(k) for k in (d["mac"], d["device_id"], d["ip"]) if k and k in order), len(order))
        out.sort(key=rank)
        return web.json_response(out)

    async def set_order(self, request):
        """Remember the order the lights stand in around the room: a list of keys (MAC, else IP)."""
        keys = [str(k) for k in (await request.json()).get("keys", []) if k]
        cfg = load_config()
        cfg["light_order"] = keys
        save_config(cfg)
        return web.json_response({"ok": True})

    async def set_output_fps(self, request):
        """Cap the frames a second sent to the lights (0: no cap). Takes effect at once, and is remembered."""
        fps = max(0.0, min(120.0, float((await request.json()).get("fps", 0) or 0)))
        self.output.max_fps = fps
        cfg = load_config()
        cfg["light_max_fps"] = fps
        save_config(cfg)
        return web.json_response({"max_fps": fps})

    async def output_status(self, request):
        o = self.output
        return web.json_response({"active": o.active, "devices": [d.ip for d in o.devices], "frames": o.frames,
                                  "sent": o.sent, "sent_by_light": o.sent_by_ip, "max_fps": o.max_fps, "fps_auto": self.link.to_json() if self.link else None,
                                  "wifi_priority": await asyncio.get_running_loop().run_in_executor(None, light_priority_policy),
                                  "errors": o.errors, "last_error": o.last_error,
                                  "seconds_since_last_frame": round(time.monotonic() - o.last_frame, 2) if o.last_frame else None})

    async def set_selected(self, request):
        """Remember which lights are ticked, by MAC and device id so a new IP address changes nothing."""
        body = await request.json()
        keys = []
        for light in body.get("lights", []):
            keys += [k for k in (light.get("mac"), light.get("device_id")) if k]
        cfg = load_config()
        cfg["selected_lights"] = sorted(set(keys))
        save_config(cfg)
        return web.json_response({"ok": True})

    async def set_name(self, request):
        """Name a light. Stored under every key it is known by, so a changed IP or an empty ARP cache cannot lose it."""
        body = await request.json()
        name = str(body.get("name", "")).strip()[:40]
        cfg = load_config()
        names = cfg.setdefault("light_names", {})
        for key in {body.get("mac"), body.get("device_id")} - {None, ""}:
            if name:
                names[key] = name
            else:
                names.pop(key, None)
        save_config(cfg)
        return web.json_response({"ok": True, "name": name})

    async def set_flip(self, request):
        """Remember that a light runs the other way (its first segment is on the right), by every key it is known by."""
        body = await request.json()
        cfg = load_config()
        flipped = cfg.setdefault("flipped_lights", {})
        for key in {body.get("mac"), body.get("device_id"), body.get("ip")} - {None, ""}:
            if body.get("flip"):
                flipped[key] = True
            else:
                flipped.pop(key, None)
        save_config(cfg)
        return web.json_response({"ok": True})

    def _light_fixed_fps(self, ip: str) -> bool:
        """A model that takes frames at a fixed rate (govee_lan.SKU_FPS: the kitchen's meteor tubes)."""
        d = next((d for d in self.known if d.ip == ip), None)
        return d is not None and d.sku in govee_lan.SKU_FPS

    def _light_segments(self, ip: str) -> int:
        """How many segments the light at this address has (as the light list works it out)."""
        d = next((d for d in self.known if d.ip == ip), None)
        if d is None:
            return 50
        seg = load_config().get("segments", {})
        return int(seg.get(d.mac) or seg.get(d.ip) or seg.get(d.device_id) or seg.get(d.sku) or govee_lan.SKU_SEGMENTS.get(d.sku) or 10)

    async def set_segments(self, request):
        body = await request.json()
        cfg = load_config()
        cfg.setdefault("segments", {})[body["key"]] = int(body["segments"])
        save_config(cfg)
        return web.json_response({"ok": True})

    # -- songs / projects -------------------------------------------------------
    def _project_dir(self, name: str) -> str:
        d = os.path.join(SHOWS_DIR, safe_name(name))
        os.makedirs(d, exist_ok=True)
        return d

    async def _analyse_into(self, name: str, src_path: str, copy: bool) -> dict:
        pdir = self._project_dir(name)
        ext = os.path.splitext(src_path)[1].lower()
        dest = os.path.join(pdir, "song" + ext)
        if copy and os.path.abspath(src_path) != os.path.abspath(dest):
            shutil.copyfile(src_path, dest)
        loop = asyncio.get_running_loop()
        analysis = await loop.run_in_executor(None, analyze_cached, dest, os.path.join(pdir, "cache"))
        return {"project": safe_name(name), "audio_url": f"/api/audio/{safe_name(name)}", "analysis": analysis}

    async def upload_song(self, request):
        reader = await request.multipart()
        field = await reader.next()
        filename = os.path.basename(field.filename or "song.wav")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in AUDIO_EXT:
            return web.json_response({"error": f"unsupported file type {ext}"}, status=400)
        name = os.path.splitext(filename)[0]
        pdir = self._project_dir(name)
        dest = os.path.join(pdir, "song" + ext)
        with open(dest, "wb") as fh:
            while True:
                chunk = await field.read_chunk(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        try:
            return await webjson.response(await self._analyse_into(name, dest, copy=False))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"analysis failed: {e}"}, status=500)

    async def open_path(self, request):
        body = await request.json()
        path = body.get("path", "")
        if not os.path.isfile(path):
            return web.json_response({"error": "file not found"}, status=404)
        if os.path.splitext(path)[1].lower() not in AUDIO_EXT:
            return web.json_response({"error": "unsupported file type"}, status=400)
        name = body.get("name") or os.path.splitext(os.path.basename(path))[0]
        try:
            return await webjson.response(await self._analyse_into(name, path, copy=True))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"analysis failed: {e}"}, status=500)

    async def audio(self, request):
        """The project's song. `?start=` streams it from that second onward.

        Casting uses that parameter instead of asking the speaker to seek, because
        a speaker seeking inside a variable bitrate file lands in the wrong place
        and every start position then drifts by a different amount.
        """
        pdir = os.path.join(SHOWS_DIR, safe_name(request.match_info["project"]))
        for fn in os.listdir(pdir) if os.path.isdir(pdir) else []:
            if not fn.startswith("song."):
                continue
            path = os.path.join(pdir, fn)
            try:
                start = float(request.query.get("start", "0") or 0)
            except ValueError:
                start = 0.0
            offset, exact = audiotrim.slice_at(path, start)
            if offset <= 0:
                return web.FileResponse(path)
            head = audiotrim.header_for(path, offset)
            body = os.path.getsize(path) - offset
            resp = web.StreamResponse(headers={
                "Content-Type": CAST_TYPES.get(os.path.splitext(fn)[1].lower(), "audio/mpeg"),
                "Content-Length": str(len(head) + body),
                "Accept-Ranges": "none",
                "X-Audio-Start": f"{exact:.4f}",
            })
            await resp.prepare(request)
            if head:
                await resp.write(head)
            with open(path, "rb") as fh:
                fh.seek(offset)
                while chunk := fh.read(1 << 18):
                    await resp.write(chunk)
            await resp.write_eof()
            return resp
        raise web.HTTPNotFound()

    async def list_projects(self, request):
        out = []
        if os.path.isdir(SHOWS_DIR):
            for name in sorted(os.listdir(SHOWS_DIR)):
                pdir = os.path.join(SHOWS_DIR, name)
                pj = os.path.join(pdir, "project.json")
                song = [f for f in os.listdir(pdir) if f.startswith("song.")] if os.path.isdir(pdir) else []
                if song:
                    out.append({"name": name, "has_project": os.path.exists(pj),
                                "modified": os.path.getmtime(pj) if os.path.exists(pj) else os.path.getmtime(pdir)})
        out.sort(key=lambda p: -p["modified"])
        return web.json_response(out)

    async def load_project(self, request):
        name = safe_name(request.match_info["name"])
        pdir = os.path.join(SHOWS_DIR, name)
        if not os.path.isdir(pdir):
            raise web.HTTPNotFound()
        song = [f for f in os.listdir(pdir) if f.startswith("song.")]
        if not song:
            raise web.HTTPNotFound()
        loop = asyncio.get_running_loop()
        cache_dir = os.path.join(pdir, "cache")
        if request.query.get("fresh") == "1" and os.path.isdir(cache_dir):
            # Re-detect: every analysis saved for this song (all versions, and the section model's own
            # results) is thrown away, and the song is analysed from scratch by the newest code
            for f in os.listdir(cache_dir):
                fp = os.path.join(cache_dir, f)
                if os.path.isfile(fp) and f.endswith(".json"):
                    os.remove(fp)
        analysis = await loop.run_in_executor(None, analyze_cached, os.path.join(pdir, song[0]), cache_dir)
        project = None
        pj = os.path.join(pdir, "project.json")
        if os.path.exists(pj):
            with open(pj, encoding="utf-8") as fh:
                project = json.load(fh)
        # (a show rebuilt before, project.before-rebuild.json beside it, was built by the studio, not by hand)
        return await webjson.response({"project": name, "audio_url": f"/api/audio/{name}", "analysis": analysis, "saved": project,
                                       "rebuilt_before": os.path.exists(os.path.join(pdir, "project.before-rebuild.json"))})

    async def save_project(self, request):
        """Save a show. ?backup=1 (a show rebuilt because it was built by older show logic) keeps the
        version it replaces as project.before-rebuild.json first. A test copy of the studio (--no-lights)
        never writes: it shares the shows folder, and a test once saved over a real show."""
        name = safe_name(request.match_info["name"])
        body = await request.json()
        if self.read_only:
            print(f"[test copy] not saving {name}: a test copy of the studio never writes shows")
            return web.json_response({"ok": True, "name": name, "read_only": True})
        pdir = self._project_dir(name)
        path = os.path.join(pdir, "project.json")
        if request.query.get("backup") == "1" and os.path.exists(path):
            shutil.copy2(path, os.path.join(pdir, "project.before-rebuild.json"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        return web.json_response({"ok": True, "name": name})

    async def delete_project(self, request):
        name = safe_name(request.match_info["name"])
        if self.read_only:
            return web.json_response({"ok": True, "read_only": True})     # a test copy never deletes a show
        pdir = os.path.join(SHOWS_DIR, name)
        if os.path.isdir(pdir):
            shutil.rmtree(pdir)
        return web.json_response({"ok": True})

    async def settings(self, request):
        cfg = load_config()
        return web.json_response({"cast_offset_ms": cfg.get("cast_offset_ms", 0), "cast_offsets": cfg.get("cast_offsets", {}),
                                  "light_offset_ms": cfg.get("light_offset_ms", 30)})

    async def set_light_offset(self, request):
        """The lights' offset belongs to the lights, not to a song: one value for every show and jam."""
        ms = float((await request.json()).get("ms", 30))
        cfg = load_config()
        cfg["light_offset_ms"] = max(-2000.0, min(2000.0, ms))
        save_config(cfg)
        return web.json_response({"light_offset_ms": cfg["light_offset_ms"]})

    # -- Google Cast ---------------------------------------------------------------
    async def _cast_call(self, fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return web.json_response(await loop.run_in_executor(None, fn, *args))
        except Exception as e:  # noqa: BLE001 - surface the reason to the UI
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def cast_devices(self, request):
        timeout = 6.0 if request.query.get("rescan") == "1" else 4.0
        return await self._cast_call(self.cast.discover, timeout)

    async def cast_connect(self, request):
        body = await request.json()
        self.jam._cast_failed = None                  # pressing Cast tries the speaker again, even one that failed
        return await self._cast_call(self.cast.connect, body.get("name", ""))

    async def cast_disconnect(self, request):
        return await self._cast_call(lambda: (self.cast.disconnect(), self.cast.status())[1])

    async def cast_play(self, request):
        body = await request.json()
        name = safe_name(body.get("project", ""))
        pdir = os.path.join(SHOWS_DIR, name)
        song = [f for f in os.listdir(pdir) if f.startswith("song.")] if os.path.isdir(pdir) else []
        if not song:
            return web.json_response({"error": "project has no song"}, status=404)
        ip = govee_lan.local_ip() or "127.0.0.1"
        url = f"http://{ip}:{self.port}/api/audio/{name}"
        return await self._cast_call(self.cast.play, url, os.path.join(pdir, song[0]), name, float(body.get("start", 0.0)))

    async def cast_pause(self, request):
        return await self._cast_call(self.cast.pause)

    async def cast_resume(self, request):
        return await self._cast_call(self.cast.resume)

    async def cast_seek(self, request):
        body = await request.json()
        return await self._cast_call(self.cast.seek, float(body.get("t", 0.0)))

    async def cast_volume(self, request):
        body = await request.json()
        return await self._cast_call(self.cast.set_volume, float(body.get("level", 0.3)))

    async def cast_status(self, request):
        return web.json_response(self.cast.status())

    async def cast_sync_check(self, request):
        """Listen to the room and measure the real Cast delay for the song playing now."""
        body = await request.json()
        pdir = os.path.join(SHOWS_DIR, safe_name(body.get("project", "")))
        song = [f for f in os.listdir(pdir) if f.startswith("song.")] if os.path.isdir(pdir) else []
        if not song:
            return web.json_response({"error": "project has no song"}, status=404)
        from .calibrate import live_sync_check

        def run():
            result = live_sync_check(self.cast, os.path.join(pdir, song[0]), float(body.get("seconds", 8.0)),
                                     repeats=int(body.get("repeats", 2)))
            # The speaker is always handed a stream that already starts in the right
            # place, so what is measured is the speaker's own fixed delay. Worth
            # keeping: it holds for every song and every start point.
            device = self.cast.name
            if device and result.get("confidence", 0) >= 0.5:
                cfg = load_config()
                cfg.setdefault("cast_offsets", {})[device] = result["offset_ms"]
                save_config(cfg)
                result["saved_for"] = device
            return result

        return await self._cast_call(run)

    # -- frames -------------------------------------------------------------------
    async def websocket(self, request):
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.ws_clients += 1
        loop = asyncio.get_running_loop()
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "select":
                    self.requested = [str(ip) for ip in data.get("ips", [])]
                    off = self._off_ips()
                    dropped, added = self.output.select([ip for ip in self.requested if ip not in off], self.known)
                    for ip in [ip for ip in list(self.output.leaving) if ip not in self.requested]:
                        dropped.append(self.output.leaving.pop(ip)["dev"])     # unticked mid-fade: back at once
                    await ws.send_json({"type": "selected", "ips": [d.ip for d in self.output.devices]})
                    if dropped or added:
                        async with self.scan_lock:          # asking lights how they are shares the scan's port
                            await loop.run_in_executor(None, self.output.reselect, dropped, added)
                elif kind == "start":
                    async with self.scan_lock:
                        await loop.run_in_executor(None, self.output.start)
                    await ws.send_json({"type": "started"})
                elif kind == "full_brightness":                 # between songs (the jam page, on a new song)
                    await loop.run_in_executor(None, self.output.full_brightness)
                elif kind == "stop":
                    async with self.scan_lock:
                        await loop.run_in_executor(None, self.output.stop, bool(data.get("off")))
                    await ws.send_json({"type": "stopped"})
                elif kind == "frame":
                    self.output.frame(data.get("frames", []), data.get("ips"))
        finally:
            self.ws_clients -= 1
            self.last_client = time.monotonic()
        if self.output.active:                          # (off the event loop: restoring the lights takes 3-4 s, and a
            async with self.scan_lock:                  # page reloaded mid-jam froze everything, the speakers' stream too)
                await loop.run_in_executor(None, self.output.stop)
        return ws


def find_chromium() -> str | None:
    """Path to an installed Chromium-family browser, or None."""
    cands: list[str] = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [os.path.join(base, *parts) for parts in (
                    ("Google", "Chrome", "Application", "chrome.exe"),
                    ("Microsoft", "Edge", "Application", "msedge.exe"),
                    ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                    ("Chromium", "Application", "chrome.exe"),
                )]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    return next((c for c in cands if os.path.exists(c)), None)


def chromium_candidates() -> list[str]:
    """Every installed Chromium-family browser, in order of preference."""
    first = find_chromium()
    found = [first] if first else []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                for parts in (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe"),
                              ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")):
                    p = os.path.join(base, *parts)
                    if os.path.exists(p) and p not in found:
                        found.append(p)
    return found


APP_PROFILE = os.path.join(local_appdata(), "lanlightshow", "browser")


def give_window_identity(wait: bool = False) -> None:
    """Once the app window is open, make it Light Show Studio on the taskbar rather than Chrome
    (its own button, name and icon, and a pin that starts the studio). See window_identity.py."""
    from .window_identity import adopt_studio_windows
    if wait:
        adopt_studio_windows(APP_PROFILE)
    else:
        threading.Thread(target=adopt_studio_windows, args=(APP_PROFILE,), daemon=True).start()


def open_app_window(url: str) -> subprocess.Popen | None:
    """Open the studio in a borderless Chromium app window (no tabs or address bar).

    It gets its own profile directory so the window is a real separate instance
    (which also lets us notice when it is closed) and remembers its size.
    """
    exe = find_chromium()
    if exe is None:
        return None
    profile = APP_PROFILE
    os.makedirs(profile, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate,TranslateUI",
            "--autoplay-policy=no-user-gesture-required", "--window-size=1680,980"]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _already_running(url: str) -> bool:
    """True if a studio is already answering on this port."""
    import urllib.request
    try:
        with urllib.request.urlopen(url + "api/settings", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - anything at all means "not ours"
        return False


def _rotate_logs(limit: int = 2_000_000) -> None:
    """studio.log and models.log grow with every session: past 2 MB, each is kept as .1 (one older copy)
    and starts afresh. (Not while another studio has it open: then it is left as it is.)"""
    for name in ("studio.log", "models.log"):
        path = os.path.join(ROOT, name)
        try:
            if os.path.getsize(path) > limit:
                os.replace(path, path + ".1")
        except OSError:
            pass


def run(host: str = "0.0.0.0", port: int = 8765, open_browser: bool = True,
        app_window: bool = True, stay: bool = False, lights: bool = True, player: bool = True) -> None:
    _rotate_logs()
    if sys.stdout is None:
        # Started without a console (pythonw, the hidden launcher): keep a log
        # next to the project instead of losing every message.
        log = open(os.path.join(ROOT, "studio.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} studio starting (hidden)")
    os.makedirs(SHOWS_DIR, exist_ok=True)
    url = f"http://127.0.0.1:{port}/"
    if open_browser and _already_running(url):
        # A second double-click should give you the window, not a silent failure.
        print("Studio already running; opening another window on it.")
        if not (app_window and open_app_window(url) is not None):
            webbrowser.open(url)
        elif app_window:
            give_window_identity(wait=True)             # this process ends right after, so do it now
        return
    studio = Studio(port=port, quit_when_closed=app_window and not stay, lights=lights, player=player)
    if not lights:
        print("Lights are disabled in this copy of the studio.")
    print(f"Light Show Studio running at {url}   (projects in {SHOWS_DIR}; also reachable on the LAN for Cast audio)")
    if open_browser:
        proc = open_app_window(url) if app_window else None
        if proc is not None:
            give_window_identity()
            print("Opened in an app window." + ("" if stay else " Close it to quit the studio."))
        else:
            if app_window:
                print("No Chromium-family browser found; opening a normal browser tab instead.")
            webbrowser.open(url)
    from . import priority
    priority.high_thread()          # this thread relays every light frame: it goes before the analysis threads
    web.run_app(studio.app, host=host, port=port, print=None)
