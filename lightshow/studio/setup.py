"""Setting up a home: one checklist for everything the studio needs to know about it, and every device in it picked
from a list rather than written into the code.

    GET  /api/setup            the checklist: [{id, title, done, detail}] (the desktop's Setup panel; first run opens it)
    GET  /api/audio/devices    the microphones and outputs on this computer, and which are chosen
    POST /api/audio/devices    {mic_device?, party_room_output?, party_hidden_output?}: "" is the system default
    GET  /api/audio/miclevel   a second from the chosen microphone: {level_db} (the Setup panel's test meter)
    GET  /api/party/link       the address phones open the phone remote at, and its QR code as SVG
    GET  /api/models/setup     the AI models' installer (model_setup): where it stands
    POST /api/models/setup     {action: install | cancel, variant?}: from this computer only

The microphone listens for the sync check and the speakers' calibration (calibrate.py); the room output is where the
house hears this computer in a party; the hidden output is where Spotify plays unheard while the studio records it.
"""

from __future__ import annotations

import asyncio
import os

from aiohttp import web

from .. import govee_lan


def _inputs() -> tuple[list[str], str]:
    """The microphones (sounddevice, the default host API only: each listed once), and the default one."""
    try:
        import sounddevice as sd
        default = sd.default.device[0]
        api = sd.query_devices(default)["hostapi"] if default is not None and default >= 0 else 0
        api_name = sd.query_hostapis(api)["name"]
        names = [f"{d['name']}, {api_name}" for d in sd.query_devices() if d["max_input_channels"] > 0 and d["hostapi"] == api
                 and not d["name"].startswith(("Microsoft Sound Mapper", "Primary Sound Capture"))]   # (Windows' stand-ins)
        return names, (f"{sd.query_devices(default)['name']}, {api_name}" if default is not None and default >= 0 else "")
    except Exception:  # noqa: BLE001 - no audio system: nothing to pick
        return [], ""


def _outputs() -> tuple[list[str], str]:
    """The outputs (soundcard: the names the party matches against), and the default one."""
    try:
        import soundcard as sc
        return [s.name for s in sc.all_speakers()], sc.default_speaker().name
    except Exception:  # noqa: BLE001
        return [], ""


def hidden_output_help() -> dict:
    """Where to get an output nobody listens to, for this computer's system."""
    import sys
    if sys.platform != "win32":
        return {"name": "", "url": "", "label": "nothing to install"}
    return {"name": "CABLE Input", "url": "https://vb-audio.com/Cable/", "label": "VB-Cable (free)"}


class HomeSetup:
    def __init__(self, studio, load_config, save_config, port: int):
        self.studio, self.load_config, self.save_config, self.port = studio, load_config, save_config, port

    @staticmethod
    def _models_note(installed: bool) -> str:
        from . import model_setup
        i = model_setup.INSTALLER
        if i.state == "running":
            return f"Installing by itself: {round(100 * i.status()['progress'])}%"
        return "Installed" if installed else "Optional: better beats, sections and drums"

    @staticmethod
    def _auto_output(outs: list[str], default_out: str, cfg: dict) -> str:
        from .player_browser import pick_hidden_output
        return pick_hidden_output(outs, default_out, cfg.get("party_room_output") or "")[0]

    @staticmethod
    def _sound_note(hidden: str, found: bool) -> str:
        from .player_browser import hidden_problem, own_hidden_output
        if own_hidden_output():
            return "Set up by itself" if found else hidden_problem()
        if not hidden:
            return "Set up by itself" if found else f"This computer has one output: install {hidden_output_help()['label']} for Spotify to play into"
        return f"Spotify plays into {hidden}" if found else f"Install {hidden_output_help()['label']} for Spotify to play into"

    def routes(self) -> list:
        return [
            web.get("/api/setup", self.h_setup),
            web.get("/api/audio/devices", self.h_devices),
            web.post("/api/audio/devices", self.h_devices_set),
            web.get("/api/audio/miclevel", self.h_miclevel),
            web.get("/api/party/link", self.h_link),
            web.get("/api/models/setup", self.h_models),
            web.post("/api/models/setup", self.h_models_set),
            web.get("/api/songs/copies", self.h_copies),
            web.post("/api/songs/copies", self.h_copies_set),
        ]

    # -- the AI models --------------------------------------------------------------------------
    async def h_models(self, request):
        from . import model_setup
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, model_setup.status))

    async def h_models_set(self, request):
        if request.remote not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden()
        from . import model_setup
        asked = await request.json()
        if (asked or {}).get("action") == "cancel":       # cancelled by hand: it doesn't start again by itself
            cfg = self.load_config()
            cfg["models_auto_install"] = False
            self.save_config(cfg)
        body, code = model_setup.post(asked)
        return web.json_response(body, status=code)

    # -- the song copies kept, for fixing shows later ---------------------------------------------
    def _copies_dir(self) -> str | None:
        fetch = getattr(getattr(self.studio, "party", None), "fetch", None)
        return fetch.copies_dir() if fetch else None

    def copies(self) -> dict:
        folder = self._copies_dir()
        names = os.listdir(folder) if folder and os.path.isdir(folder) else []
        size = sum(os.path.getsize(os.path.join(folder, n)) for n in names if os.path.isfile(os.path.join(folder, n)))
        return {"count": len(names), "gb": round(size / 1e9, 2), "keep": self.load_config().get("party_keep_youtube_copies", True) is not False}

    async def h_copies(self, request):
        return web.json_response(self.copies())

    async def h_copies_set(self, request):
        """{keep?, clear?}: from this computer only. Clearing leaves the shows: only Review needs a song's copy."""
        if request.remote not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden()
        body = await request.json()
        if "keep" in body:
            cfg = self.load_config()
            cfg["party_keep_youtube_copies"] = bool(body["keep"])
            self.save_config(cfg)
        folder = self._copies_dir()
        if body.get("clear") and folder and os.path.isdir(folder):
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    os.remove(path)
        return web.json_response(self.copies())

    def link(self) -> str:
        ip = govee_lan.local_ip() or "127.0.0.1"
        return f"http://{ip}:{self.port}/party"

    @staticmethod
    def qr_svg(url: str) -> str:
        try:
            import segno
        except ImportError:
            return ""
        import io
        buf = io.BytesIO()
        # no fixed size, so it fills its box; black on white with its margin, in any theme, as some phone cameras read nothing else
        segno.make(url, error="m").save(buf, kind="svg", omitsize=True, border=3, dark="#000", light="#fff", xmldecl=False, svgns=True)
        return buf.getvalue().decode("utf-8")

    # -- the checklist --------------------------------------------------------------------------
    def checklist(self) -> list[dict]:
        cfg = self.load_config()
        st = self.studio
        known = list(getattr(st, "known", []) or [])
        rooms = st.rooms.list() if getattr(st, "rooms", None) else []
        space = st.space.load() if getattr(st, "space", None) else {}
        shapes = ((space.get("plan") or {}).get("shapes") or {})
        placed = len(space.get("lights") or {})
        casts = len(getattr(st.cast, "casts", []) or []) if getattr(st, "cast", None) else 0
        room_speakers = sum(len(r.get("speakers") or []) for r in rooms)
        sp = getattr(getattr(st, "party", None), "spotify", None)
        signed_in = bool(sp and sp.signed_in)
        client = bool((cfg.get("spotify") or {}).get("client_id"))
        outs, default_out = _outputs()
        from .player_browser import hidden_problem, own_hidden_output
        hidden = cfg.get("party_hidden_output") or ""
        if own_hidden_output():
            hidden_found = not hidden_problem()
        elif hidden:
            hidden_found = any(hidden.lower() in o.lower() for o in outs)
        else:
            hidden_found = bool(self._auto_output(outs, default_out, cfg))
        try:
            from .model_worker import venv_py
            models = os.path.exists(venv_py())
        except Exception:  # noqa: BLE001
            models = False
        from . import addons
        on = [a["name"] for a in addons.status(cfg) if a["on"]]
        item = lambda i, title, done, detail: {"id": i, "title": title, "done": bool(done), "detail": detail}
        return [
            item("lights", "Lights", known, f"{len(known)} found" if known else "None found yet: turn on LAN Control for each light in the Govee Home app"),
            item("rooms", "Rooms", rooms, f"{len(rooms)} rooms" if rooms else "Put your lights and speakers in rooms"),
            item("map", "House map", shapes.get("rooms") and placed, (f"{len(shapes.get('rooms') or [])} rooms drawn, {placed} lights placed"
                                                                       if shapes.get("rooms") else "Draw your home and place the lights")),
            item("speakers", "Speakers", casts or room_speakers or cfg.get("cast_offsets"),
                 f"{casts} Cast speakers found" if casts else ("Speakers in your rooms" if room_speakers else "Find your Google Cast speakers")),
            item("spotify", "Spotify", signed_in, "Signed in" if signed_in else ("Sign in to finish" if client else "Connect your Spotify Premium account")),
            item("sound", "Sound devices", hidden_found, self._sound_note(hidden, hidden_found)),
            item("models", "AI models", models, self._models_note(models)),
            item("phone", "Your phone", True, self.link()),
            item("addons", "Add-ons", True, ", ".join(on) if on else "None switched on"),
        ]

    async def h_setup(self, request):
        items = await asyncio.get_running_loop().run_in_executor(None, self.checklist)
        return web.json_response({"items": items, "done": all(i["done"] for i in items)})

    # -- sound devices --------------------------------------------------------------------------
    def devices(self) -> dict:
        cfg = self.load_config()
        ins, default_in = _inputs()
        outs, default_out = _outputs()
        return {"inputs": ins, "outputs": outs, "default_input": default_in, "default_output": default_out,
                "mic_device": cfg.get("mic_device") or "", "party_room_output": cfg.get("party_room_output") or "",
                "party_hidden_output": cfg.get("party_hidden_output") or "", "hidden_help": hidden_output_help(),
                "auto_output": self._auto_output(outs, default_out, cfg)}

    async def h_devices(self, request):
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self.devices))

    async def h_devices_set(self, request):
        body = await request.json()
        cfg = self.load_config()
        for k in ("mic_device", "party_room_output", "party_hidden_output"):
            if k in body:
                v = str(body[k] or "").strip()[:200]
                if v:
                    cfg[k] = v
                else:
                    cfg.pop(k, None)
        self.save_config(cfg)
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self.devices))

    async def h_miclevel(self, request):
        """A second from the chosen microphone, as a level: the Setup panel's meter (records nothing to disk)."""
        mic = self.load_config().get("mic_device") or None

        def listen():
            import numpy as np

            import time

            from .calibrate import MicRecorder
            with MicRecorder(mic) as rec:
                time.sleep(1.0)
                y, _ = rec.audio()
            y = np.asarray(y, float)
            rms = float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0
            return {"level_db": round(20 * np.log10(rms + 1e-9), 1)}
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, listen))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)

    # -- the phone ------------------------------------------------------------------------------
    async def h_link(self, request):
        url = await asyncio.get_running_loop().run_in_executor(None, self.link)
        return web.json_response({"url": url, "qr_svg": self.qr_svg(url)})
