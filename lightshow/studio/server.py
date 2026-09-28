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
from .analysis import analyze_cached
from . import audiotrim
from .cast import CastManager, CONTENT_TYPES as CAST_TYPES
from .jam_service import JamService

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
ROOT = os.path.dirname(os.path.dirname(HERE))
SHOWS_DIR = os.path.join(ROOT, "shows")
CONFIG_PATH = os.path.join(ROOT, "config.json")
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aiff", ".aif", ".opus"}
DEFAULT_LIGHT_MAC = "5C:E7:53:E9:C7:FE"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


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


class LightOutput:
    """Holds the currently selected lights and pushes frames to them."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled                          # False: a test copy that must never touch the lights
        self.devices: list[govee_lan.GoveeDevice] = []
        self.active = False
        self.last_enable = 0.0
        self.last_frame = 0.0
        self.frames = 0
        self.sent = 0                                   # frames actually sent to the lights (after the cap)
        self.last_sent = 0.0
        # Most frames a second sent to the lights. The page draws about 60; Govee lights listen over
        # 2.4 GHz Wi-Fi, where fewer packets can mean less waiting in the air. 0 means no cap.
        self.max_fps = float(load_config().get("light_max_fps", 0) or 0)
        self.errors = 0
        self.last_error = ""
        self.dark_since = None      # when the stream went all-black (None while lit)
        self.last_rearm = 0.0

    def select(self, ips: list[str], known: list[govee_lan.GoveeDevice]) -> None:
        by_ip = {d.ip: d for d in known}
        self.devices = [by_ip.get(ip) or govee_lan.GoveeDevice(ip=ip) for ip in ips]

    def start(self) -> None:
        if not self.enabled:
            return
        for _ in range(3):
            for d in self.devices:
                d.turn(True)
                d.brightness(100)
            time.sleep(0.12)
        for _ in range(3):
            for d in self.devices:
                d.razer(True)
            time.sleep(0.08)
        self.last_enable = time.monotonic()
        self.active = True

    def stop(self, off: bool = False) -> None:
        if not self.enabled:
            return
        for d in self.devices:
            d.razer(False)
            if off:
                d.turn(False)
        self.active = False

    def _rearm(self, now: float, wake: bool = False) -> None:
        self.last_rearm = now
        for d in self.devices:
            if wake:
                d.turn(True)
            d.razer(True)

    def frame(self, frames: list[list[int]]) -> None:
        if not self.active:
            return
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
        if self.max_fps > 0 and now - self.last_sent < 0.9 / self.max_fps:
            return                                      # over the cap: this one is skipped, the next goes
        self.last_sent = now
        self.sent += 1
        for d, flat in zip(self.devices, frames):
            try:
                cols = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)]
                if cols:
                    d.razer_leds(cols)
            except Exception as e:  # noqa: BLE001 - keep the show running, remember the fault
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"


class Studio:
    def __init__(self, port: int = 8765, quit_when_closed: bool = False, lights: bool = True, player: bool = True):
        self.port = port
        self.quit_when_closed = quit_when_closed
        self.ws_clients = 0
        self._watch_task = None
        self.last_client = 0.0
        self.known: list[govee_lan.GoveeDevice] = []
        self.output = LightOutput(enabled=lights)
        self.cast = CastManager(govee_lan.local_ip())
        self.app = web.Application(client_max_size=512 * 1024 * 1024, middlewares=[no_cache])
        self.jam = JamService(self, SHOWS_DIR, load_config, save_config, govee_lan.local_ip)
        self.jam.player_enabled = player
        self.app.add_routes([
            web.get("/", self.index),
            web.get("/api/devices", self.devices),
            web.get("/api/output", self.output_status),
            web.post("/api/output/fps", self.set_output_fps),
            web.post("/api/devices/segments", self.set_segments),
            web.post("/api/devices/name", self.set_name),
            web.post("/api/devices/selected", self.set_selected),
            web.post("/api/song", self.upload_song),
            web.post("/api/open", self.open_path),
            web.get("/api/audio/{project}", self.audio),
            web.get("/api/projects", self.list_projects),
            web.get("/api/projects/{name}", self.load_project),
            web.post("/api/projects/{name}", self.save_project),
            web.delete("/api/projects/{name}", self.delete_project),
            web.get("/ws", self.websocket),
            web.get("/api/settings", self.settings),
            web.post("/api/settings/light_offset", self.set_light_offset),
            web.get("/api/cast/devices", self.cast_devices),
            web.post("/api/cast/connect", self.cast_connect),
            web.post("/api/cast/disconnect", self.cast_disconnect),
            web.post("/api/cast/play", self.cast_play),
            web.post("/api/cast/pause", self.cast_pause),
            web.post("/api/cast/resume", self.cast_resume),
            web.post("/api/cast/seek", self.cast_seek),
            web.post("/api/cast/stop", self.cast_stop),
            web.post("/api/cast/volume", self.cast_volume),
            web.get("/api/cast/status", self.cast_status),
            web.post("/api/cast/synccheck", self.cast_sync_check),
            *self.jam.routes(),
            web.static("/static", STATIC),
        ])
        if self.quit_when_closed:
            self.app.on_startup.append(self._start_quit_watchdog)
        self.app.on_shutdown.append(self._close_player)

    async def _close_player(self, app):
        """Any orderly exit: Edge's sound goes back to the default output, and the hidden player closes."""
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.jam.close_player)
        except Exception:  # noqa: BLE001
            pass

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
                    print("Studio window closed. Stopping.")
                    try:
                        self.jam.close_player()          # the hidden Spotify player goes with the studio
                    except Exception:  # noqa: BLE001
                        pass
                    os._exit(0)
        # keep a reference: a bare create_task can be garbage collected mid-flight
        self._watch_task = asyncio.create_task(watch())

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
        loop = asyncio.get_running_loop()
        if request.query.get("rescan") == "1" or not self.known:
            self.known = await loop.run_in_executor(None, govee_lan.discover, 2.0)
        cfg = load_config()
        remembered = cfg.get("known_devices", {})
        arp = govee_lan.arp_table() if any(not d.mac for d in self.known) else {}
        for d in self.known:
            # The MAC comes from the ARP cache, which is empty now and then. A blank
            # must never replace a MAC we already know: the segment count is keyed by
            # it, and losing it silently drops a 50 LED strip to the 10 zone default.
            d.mac = d.mac or arp.get(d.ip, "") or remembered.get(d.ip, {}).get("mac", "")
            remembered[d.ip] = {"mac": d.mac, "sku": d.sku or remembered.get(d.ip, {}).get("sku", ""),
                                "device_id": d.device_id or remembered.get(d.ip, {}).get("device_id", "")}
        if self.known:
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
        # Which lights the shows play on is a property of the room, not of a song: one
        # selection, kept here, that every show, the preview and Jam mode all use.
        chosen = cfg.get("selected_lights")
        out = []
        for d in self.known:
            if not d.mac:   # ARP may not have the entry yet; use what we remembered
                d.mac = remembered.get(d.ip, {}).get("mac", "")
            n = seg.get(d.mac) or seg.get(d.ip) or seg.get(d.device_id) or seg.get(d.sku) or 10
            name = names.get(d.mac) or names.get(d.device_id) or names.get(d.ip) or ""
            out.append({"ip": d.ip, "mac": d.mac, "device_id": d.device_id, "name": name, "sku": d.sku, "segments": int(n), "online": d.ip in online,
                        "default": d.mac.upper() == DEFAULT_LIGHT_MAC,
                        "on": (d.mac in chosen or d.device_id in chosen) if chosen is not None else d.mac.upper() == DEFAULT_LIGHT_MAC})
        return web.json_response(out)

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
                                  "sent": o.sent, "max_fps": o.max_fps,
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
            return web.json_response(await self._analyse_into(name, dest, copy=False))
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
            return web.json_response(await self._analyse_into(name, path, copy=True))
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
        analysis = await loop.run_in_executor(None, analyze_cached, os.path.join(pdir, song[0]), os.path.join(pdir, "cache"))
        project = None
        pj = os.path.join(pdir, "project.json")
        if os.path.exists(pj):
            with open(pj, encoding="utf-8") as fh:
                project = json.load(fh)
        return web.json_response({"project": name, "audio_url": f"/api/audio/{name}", "analysis": analysis, "saved": project})

    async def save_project(self, request):
        name = safe_name(request.match_info["name"])
        body = await request.json()
        pdir = self._project_dir(name)
        with open(os.path.join(pdir, "project.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        return web.json_response({"ok": True, "name": name})

    async def delete_project(self, request):
        name = safe_name(request.match_info["name"])
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

    async def cast_stop(self, request):
        return await self._cast_call(self.cast.stop)

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
                    self.output.select(data.get("ips", []), self.known)
                    await ws.send_json({"type": "selected", "ips": [d.ip for d in self.output.devices]})
                elif kind == "start":
                    await loop.run_in_executor(None, self.output.start)
                    await ws.send_json({"type": "started"})
                elif kind == "stop":
                    await loop.run_in_executor(None, self.output.stop, bool(data.get("off")))
                    await ws.send_json({"type": "stopped"})
                elif kind == "frame":
                    self.output.frame(data.get("frames", []))
        finally:
            self.ws_clients -= 1
            self.last_client = time.monotonic()
        if self.output.active:
            self.output.stop()
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


APP_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "lanlightshow", "browser")


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


def run(host: str = "0.0.0.0", port: int = 8765, open_browser: bool = True,
        app_window: bool = True, stay: bool = False, lights: bool = True, player: bool = True) -> None:
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
