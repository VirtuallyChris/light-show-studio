"""Bulb Show: little looping light shows, each one sound on one speaker and one bulb.

A show lives in bulbshow/shows/<id>/ as show.json and sound.wav (both kept out of git). Any number of
shows can run at once, each looping on its own speaker and bulb, as long as no two share either.
Like the studio, closing the window ends it all: every show stops, every bulb is put back as it
was, and the server quits.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

from aiohttp import web

from lightshow import govee_lan
from lightshow.studio.winpaths import local_appdata

from . import effects
from .bulbs import CloudClient, CloudUsage, CloudError, find_bulbs, make_bulb
from .runner import ShowRunner
from .sound import Sound

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")
SHOWS_DIR = os.path.join(HERE, "shows")
CONFIG_PATH = os.path.join(HERE, "config.json")
STUDIO_CONFIG = os.path.join(ROOT, "config.json")      # read only: the speakers' measured delays

NEW_SHOW = {"name": "", "source": "", "duration": 0.0, "layers": [[], []], "speaker": "", "bulb": None, "volume": 1.0, "light_ms": 0}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or "show"


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, doc) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


class App:
    def __init__(self, port: int, shows_dir: str = SHOWS_DIR, config_path: str = CONFIG_PATH, lights: bool = True,
                 cast: bool = True, quit_when_closed: bool = False, speakers=None):
        self.port, self.shows_dir, self.config_path = port, shows_dir, config_path
        self.lights, self.quit_when_closed = lights, quit_when_closed
        os.makedirs(shows_dir, exist_ok=True)
        if speakers is None:
            if cast:
                from .speakers import Speakers
                speakers = Speakers()
            else:
                from .speakers import FakeSpeakers
                speakers = FakeSpeakers()
        self.speakers = speakers
        self.lan_ip = govee_lan.local_ip() or "127.0.0.1"
        self.cfg_lock = threading.Lock()
        self.usage = CloudUsage(load=lambda: self.config().get("cloud_usage"), save=self._save_usage)
        self.client = None
        self._reset_client()
        self.runners: dict[str, ShowRunner] = {}
        self.sounds: dict[str, tuple[float, Sound]] = {}
        self.ws_clients: set = set()
        self.started_at = time.monotonic()
        self.CLOSE_GRACE_S = 8.0           # a reload drops the window's socket for a moment: not a close
        self.exit = lambda: os._exit(0)
        self.app = web.Application(client_max_size=512 * 1024 * 1024, middlewares=[self.seen])
        self.app.add_routes([
            web.get("/", self.index),
            web.static("/static", STATIC),
            web.get("/api/shows", self.list_shows),
            web.post("/api/shows", self.create_show),
            web.get("/api/shows/{id}", self.get_show),
            web.post("/api/shows/{id}", self.save_show),
            web.delete("/api/shows/{id}", self.delete_show),
            web.post("/api/shows/{id}/sound", self.upload_sound),
            web.get("/api/shows/{id}/sound.wav", self.get_sound),
            web.post("/api/render", self.render),
            web.get("/api/speakers", self.list_speakers),
            web.get("/api/bulbs", self.list_bulbs),
            web.get("/api/scenes", self.list_scenes),
            web.get("/api/settings", self.get_settings),
            web.post("/api/settings", self.set_settings),
            web.post("/api/run/{id}", self.run_show),
            web.post("/api/stop/{id}", self.stop_show),
            web.post("/api/run-all", self.run_all),
            web.post("/api/stop-all", self.stop_all),
            web.get("/api/running", self.running),
            web.get("/api/loop/{sid}.wav", self.loop_stream),
            web.get("/ws", self.websocket),
        ])
        self.app.on_shutdown.append(self._shutdown)

    # -- config ------------------------------------------------------------------------
    def config(self) -> dict:
        return _read_json(self.config_path, {})

    def _update_config(self, **changes) -> None:
        with self.cfg_lock:
            cfg = self.config()
            cfg.update(changes)
            _write_json(self.config_path, cfg)

    def _save_usage(self, usage: dict) -> None:
        self._update_config(cloud_usage=usage)

    def _reset_client(self) -> None:
        key = (self.config().get("govee_api_key") or "").strip()
        self.client = CloudClient(key, self.usage) if key else None

    def cast_offset(self, speaker: str) -> float:
        """The speaker's sound delay in seconds, as the studio measured it (its config.json)."""
        cfg = _read_json(STUDIO_CONFIG, {})
        ms = (cfg.get("cast_offsets") or {}).get(speaker, cfg.get("cast_offset_ms", 0))
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            return 0.0

    # -- shows on disk -------------------------------------------------------------------
    def show_dir(self, sid: str) -> str:
        if not re.fullmatch(r"[a-z0-9-]{1,48}", sid or ""):
            raise web.HTTPNotFound()
        return os.path.join(self.shows_dir, sid)

    def load(self, sid: str) -> dict:
        doc = _read_json(os.path.join(self.show_dir(sid), "show.json"), None)
        if doc is None:
            raise web.HTTPNotFound()
        return {**NEW_SHOW, **doc}

    def sound(self, sid: str) -> Sound | None:
        path = os.path.join(self.show_dir(sid), "sound.wav")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self.sounds.get(sid)
        if cached and cached[0] == mtime:
            return cached[1]
        snd = Sound.load(path)
        self.sounds[sid] = (mtime, snd)
        return snd

    def ids(self) -> list[str]:
        return sorted(d for d in os.listdir(self.shows_dir) if os.path.isfile(os.path.join(self.shows_dir, d, "show.json")))

    # -- requests ------------------------------------------------------------------------
    @web.middleware
    async def seen(self, request, handler):
        resp = await handler(request)
        if request.path.startswith("/static") or request.path == "/":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    async def index(self, request):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    def _summary(self, sid: str) -> dict:
        s = self.load(sid)
        r = self.runners.get(sid)
        bulb = s.get("bulb") or {}
        return {"id": sid, "name": s.get("name") or sid, "duration": s.get("duration", 0), "speaker": s.get("speaker", ""),
                "bulb": bulb, "per_hour": effects.per_hour(s) if bulb.get("via") == "cloud" else None,
                "running": bool(r and r.active),
                "ready": bool(s.get("duration") and s.get("speaker") and bulb)}

    async def list_shows(self, request):
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, lambda: [self._summary(i) for i in self.ids()])
        return web.json_response(out)

    async def create_show(self, request):
        body = await request.json()
        name = (body.get("name") or "").strip() or "New show"
        base = slug(name)
        sid, n = base, 2
        while os.path.exists(os.path.join(self.shows_dir, sid)):
            sid, n = f"{base}-{n}", n + 1
        os.makedirs(os.path.join(self.shows_dir, sid))
        doc = {**NEW_SHOW, "name": name}
        _write_json(os.path.join(self.shows_dir, sid, "show.json"), doc)
        return web.json_response({"id": sid, **doc})

    async def get_show(self, request):
        sid = request.match_info["id"]
        return web.json_response({"id": sid, **self.load(sid)})

    async def save_show(self, request):
        sid = request.match_info["id"]
        old = self.load(sid)
        body = await request.json()
        doc = {k: body.get(k, old.get(k)) for k in NEW_SHOW}
        doc["duration"] = old.get("duration", 0)               # set by the sound itself, never by the page
        _write_json(os.path.join(self.show_dir(sid), "show.json"), doc)
        r = self.runners.get(sid)
        if r is not None and r.active:
            r.update_show(doc)
        return web.json_response({"ok": True})

    async def delete_show(self, request):
        sid = request.match_info["id"]
        d = self.show_dir(sid)
        r = self.runners.pop(sid, None)
        if r is not None:
            r.stop()
        self.sounds.pop(sid, None)
        shutil.rmtree(d, ignore_errors=True)
        return web.json_response({"ok": True})

    async def upload_sound(self, request):
        sid = request.match_info["id"]
        d = self.show_dir(sid)
        show = self.load(sid)
        data = await request.read()
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=d)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            snd = Sound.load(tmp)
            if snd.duration < 0.2:
                raise ValueError("the sound is too short")
        except Exception as e:  # noqa: BLE001
            os.remove(tmp)
            return web.json_response({"error": f"could not read that sound: {e}"}, status=400)
        r = self.runners.pop(sid, None)
        if r is not None:
            r.stop()
        os.replace(tmp, os.path.join(d, "sound.wav"))
        show["duration"] = round(snd.duration, 4)
        show["source"] = request.query.get("name") or show.get("source") or ""
        _write_json(os.path.join(d, "show.json"), {k: show.get(k) for k in NEW_SHOW})
        return web.json_response({"duration": show["duration"]})

    async def get_sound(self, request):
        path = os.path.join(self.show_dir(request.match_info["id"]), "sound.wav")
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def render(self, request):
        show = await request.json()
        cloud = (show.get("bulb") or {}).get("via") == "cloud"
        loop = asyncio.get_running_loop()

        def work():
            return {"fps": effects.FPS, "frames": effects.render(show, cloud), "cloud": cloud,
                    "per_hour": effects.per_hour(show) if cloud else None}
        return web.json_response(await loop.run_in_executor(None, work))

    async def list_speakers(self, request):
        loop = asyncio.get_running_loop()
        try:
            out = await loop.run_in_executor(None, lambda: self.speakers.list(request.query.get("fresh") == "1"))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e), "speakers": []})
        return web.json_response({"speakers": out})

    async def list_bulbs(self, request):
        if not self.lights:
            return web.json_response({"bulbs": [{"id": "virtual-1", "sku": "H6008", "via": "virtual", "name": "Virtual bulb 1"},
                                                {"id": "virtual-2", "sku": "H6008", "via": "virtual", "name": "Virtual bulb 2"}],
                                      "problem": ""})
        loop = asyncio.get_running_loop()
        bulbs, problem = await loop.run_in_executor(None, find_bulbs, self.client)
        return web.json_response({"bulbs": bulbs, "problem": problem})

    VIRTUAL_SCENES = [{"name": n, "instance": "lightScene", "value": {"id": i, "paramId": i}}
                      for i, n in enumerate(["Breathe", "Candlelight", "Flicker", "Rainbow"], 1)]

    async def list_scenes(self, request):
        """The bulb's own scenes (asked of Govee once per bulb while the app runs)."""
        sku, dev, via = request.query.get("sku", ""), request.query.get("id", ""), request.query.get("via", "")
        if not self.lights or via == "virtual":
            return web.json_response({"scenes": self.VIRTUAL_SCENES})
        if via != "cloud":
            return web.json_response({"scenes": [], "problem": "only a bulb driven through Govee's cloud can run its own scenes"})
        if self.client is None:
            return web.json_response({"scenes": [], "problem": "no Govee API key yet (⚙ settings)"})
        cache = self.__dict__.setdefault("_scenes", {})
        if dev not in cache or request.query.get("fresh") == "1":
            try:
                cache[dev] = await asyncio.get_running_loop().run_in_executor(None, self.client.scenes, sku, dev)
            except CloudError as e:
                return web.json_response({"scenes": [], "problem": str(e)})
        return web.json_response({"scenes": cache[dev]})

    async def get_settings(self, request):
        key = self.config().get("govee_api_key") or ""
        return web.json_response({"has_key": bool(key), "usage": self.usage.snapshot(), "lights": self.lights})

    async def set_settings(self, request):
        body = await request.json()
        if "govee_api_key" in body:
            self._update_config(govee_api_key=str(body["govee_api_key"] or "").strip())
            self._reset_client()
        return await self.get_settings(request)

    # -- running -----------------------------------------------------------------------------
    def _start(self, sid: str, at: float = 0.0) -> str:
        """Start a show; returns "" or why it cannot start."""
        show = self.load(sid)
        snd = self.sound(sid)
        if snd is None:
            return "it has no sound yet"
        if not show.get("speaker"):
            return "it has no speaker"
        if not show.get("bulb"):
            return "it has no bulb"
        bulb_id = show["bulb"].get("id")
        for oid, r in self.runners.items():
            if oid == sid or not r.active:
                continue
            if r.speaker == show["speaker"]:
                return f"{show['speaker']} is already playing {r.show.get('name')}"
            if (r.show.get("bulb") or {}).get("id") == bulb_id:
                return f"that bulb is already in {r.show.get('name')}"
        old = self.runners.get(sid)
        if old is not None:
            old.stop(wait=True)
        try:
            bulb = make_bulb(show["bulb"], self.client, virtual=not self.lights or show["bulb"].get("via") == "virtual")
        except CloudError as e:
            return str(e)
        r = ShowRunner(sid, show, snd, self.speakers, bulb, url_for=lambda s: f"http://{self.lan_ip}:{self.port}/api/loop/{s}.wav",
                       cast_offset_s=self.cast_offset(show["speaker"]), start_at=at)
        self.runners[sid] = r
        r.start()
        return ""

    async def run_show(self, request):
        sid = request.match_info["id"]
        body = await request.json() if request.can_read_body else {}
        loop = asyncio.get_running_loop()
        why = await loop.run_in_executor(None, self._start, sid, float(body.get("at") or 0))
        return web.json_response({"ok": not why, "error": why})

    async def stop_show(self, request):
        r = self.runners.get(request.match_info["id"])
        if r is not None:
            r.stop()
        return web.json_response({"ok": True})

    async def run_all(self, request):
        loop = asyncio.get_running_loop()
        problems = {}
        for sid in self.ids():
            r = self.runners.get(sid)
            if r is not None and r.active:
                continue
            why = await loop.run_in_executor(None, self._start, sid, 0.0)
            if why:
                problems[sid] = why
        return web.json_response({"ok": not problems, "problems": problems})

    async def stop_all(self, request):
        for r in self.runners.values():
            r.stop()
        return web.json_response({"ok": True})

    async def running(self, request):
        out = {sid: r.status() for sid, r in self.runners.items() if r.active or r.error}
        return web.json_response({"shows": out, "usage": self.usage.snapshot()})

    async def loop_stream(self, request):
        sid = request.match_info["sid"]
        runner = next((r for r in self.runners.values() if r.sid == sid and r.active), None)
        reader = runner.reader_for(sid) if runner else None
        if reader is None:
            raise web.HTTPGone() if runner else web.HTTPNotFound()
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav", "Cache-Control": "no-store", "Accept-Ranges": "none"})
        await resp.prepare(request)
        chunk = 4096
        try:
            await resp.write(reader.header())
            while runner.active and runner.reader is reader:
                await resp.write(reader.read(chunk))       # the speaker's own pace sets ours (it stops reading when full)
        except (ConnectionResetError, ConnectionError, OSError, asyncio.CancelledError, RuntimeError):
            pass
        return resp

    # -- life ------------------------------------------------------------------------------
    def any_running(self) -> bool:
        return any(r.active for r in self.runners.values())

    async def websocket(self, request):
        """The window keeps this open for as long as it is open. Polling can't tell: Chrome slows the
        timers of a minimized window to once a minute, which would look like a closed one."""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.ws_clients.add(ws)
        try:
            async for _ in ws:
                pass
        finally:
            self.ws_clients.discard(ws)
            if not self.ws_clients:
                asyncio.create_task(self._window_gone())
        return ws

    async def _window_gone(self) -> None:
        await asyncio.sleep(self.CLOSE_GRACE_S)
        if self.quit_when_closed and not self.ws_clients:
            await self.quit("the window was closed")

    async def quit(self, why: str) -> None:
        """Stop every show (each bulb put back as it was, each speaker let go), then end."""
        print(f"Quitting: {why}.")
        await self._shutdown(None)
        self.exit()

    async def watch_window(self) -> None:
        """Saves the cloud count now and then, and quits if no window ever turned up (it failed to open)."""
        while True:
            await asyncio.sleep(5)
            self.usage.save()
            if self.quit_when_closed and not self.ws_clients and time.monotonic() - self.started_at > 90 and not self.any_running():
                await self.quit("no window")

    async def _shutdown(self, app) -> None:
        for r in self.runners.values():
            r.stop()
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(None, r.stop, True) for r in self.runners.values()))
        self.usage.save()


# -- the window ------------------------------------------------------------------------------------
APP_PROFILE = os.path.join(local_appdata(), "bulbshow", "browser")


APP_ID = "LightShowStudio.BulbShow"


def window_identity() -> dict:
    wscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    return dict(title="", app_id=APP_ID, name="Bulb Show", relaunch=f'"{wscript}" "{os.path.join(HERE, "Bulb Show.vbs")}"',
                icon=os.path.join(HERE, "Bulb Show.ico") + ",0")


def give_window_identity(wait: bool = False) -> None:
    """Make the app window Bulb Show on the taskbar rather than Chrome: its own button, name and icon,
    and a pin that starts Bulb Show (the studio's window_identity, with Bulb Show's details). Every
    window of the app's own browser profile is Bulb Show's, whatever its title."""
    from lightshow.studio.window_identity import adopt_studio_windows
    identity = window_identity()
    if wait:
        adopt_studio_windows(APP_PROFILE, **identity)
    else:
        threading.Thread(target=adopt_studio_windows, args=(APP_PROFILE,), kwargs=identity, daemon=True).start()


def find_chromium() -> str | None:
    cands = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [os.path.join(base, *p) for p in (("Google", "Chrome", "Application", "chrome.exe"),
                                                           ("Microsoft", "Edge", "Application", "msedge.exe"),
                                                           ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"))]
    for name in ("chrome", "google-chrome", "chromium", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    return next((c for c in cands if os.path.exists(c)), None)


def open_app_window(url: str) -> bool:
    exe = find_chromium()
    if exe is None:
        return False
    os.makedirs(APP_PROFILE, exist_ok=True)
    try:
        subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={APP_PROFILE}", "--no-first-run", "--no-default-browser-check",
                          "--autoplay-policy=no-user-gesture-required", "--window-size=1280,820"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def _already_running(url: str) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url + "api/settings", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def run(host: str = "0.0.0.0", port: int = 8770, open_browser: bool = True, stay: bool = False,
        lights: bool = True, cast: bool = True) -> None:
    if sys.stdout is None:                      # started without a console (the .vbs launcher): keep a log
        log = open(os.path.join(HERE, "bulbshow.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} Bulb Show starting")
    url = f"http://127.0.0.1:{port}/"
    if open_browser and _already_running(url):
        if open_app_window(url):
            give_window_identity(wait=True)             # this process ends right after, so do it now
        else:
            webbrowser.open(url)
        return
    app = App(port=port, lights=lights, cast=cast, quit_when_closed=open_browser and not stay)
    print(f"Bulb Show running at {url}" + ("" if lights else "   (lights are virtual in this copy)")
          + ("" if cast else "   (speakers are virtual in this copy)"))
    if open_browser:
        if open_app_window(url):
            give_window_identity()
        else:
            webbrowser.open(url)

    async def start_watch(_app):
        _app["watch"] = asyncio.create_task(app.watch_window())
    app.app.on_startup.append(start_watch)
    web.run_app(app.app, host=host, port=port, print=None)
