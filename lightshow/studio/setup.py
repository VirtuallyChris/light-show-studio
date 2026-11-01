"""Setting up a home: one checklist for everything the studio needs to know about it, and every device in it picked
from a list rather than written into the code.

    GET  /api/setup            the checklist: [{id, title, done, detail}] (the desktop's Setup panel; first run opens it)
    GET  /api/audio/devices    the microphones and outputs on this computer, and which are chosen
    POST /api/audio/devices    {mic_device?, jam_room_output?, jam_hidden_output?}: "" is the system default
    GET  /api/audio/miclevel   a second from the chosen microphone: {level_db} (the Setup panel's test meter)
    GET  /api/party/link       the address phones open the Party Hub at, and its QR code as SVG
    GET  /api/models/setup     the AI models' installer (model_setup): where it stands
    POST /api/models/setup     {action: install | cancel, variant?}: from this computer only

The microphone listens for the sync check and the speakers' calibration (calibrate.py); the room output is where the
house hears this computer in a jam; the hidden output is where Spotify plays unheard while the studio records it.
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
    """The outputs (soundcard: the names the jam matches against), and the default one."""
    try:
        import soundcard as sc
        return [s.name for s in sc.all_speakers()], sc.default_speaker().name
    except Exception:  # noqa: BLE001
        return [], ""


def hidden_output_help() -> dict:
    """Where to get an output nobody listens to, for this computer's system."""
    import sys
    if sys.platform == "darwin":
        return {"name": "BlackHole 2ch", "url": "https://existential.audio/blackhole/", "label": "BlackHole (free)"}
    if sys.platform.startswith("linux"):
        return {"name": "Light Show Studio", "url": "", "label": "made by the studio itself"}
    return {"name": "CABLE Input", "url": "https://vb-audio.com/Cable/", "label": "VB-Cable (free)"}


class HomeSetup:
    def __init__(self, studio, load_config, save_config, port: int):
        self.studio, self.load_config, self.save_config, self.port = studio, load_config, save_config, port

    def routes(self) -> list:
        return [
            web.get("/api/setup", self.h_setup),
            web.get("/api/audio/devices", self.h_devices),
            web.post("/api/audio/devices", self.h_devices_set),
            web.get("/api/audio/miclevel", self.h_miclevel),
            web.get("/api/party/link", self.h_link),
            web.get("/api/models/setup", self.h_models),
            web.post("/api/models/setup", self.h_models_set),
        ]

    # -- the AI models --------------------------------------------------------------------------
    async def h_models(self, request):
        from . import model_setup
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, model_setup.status))

    async def h_models_set(self, request):
        if request.remote not in ("127.0.0.1", "::1"):
            raise web.HTTPForbidden()
        from . import model_setup
        body, code = model_setup.post(await request.json())
        return web.json_response(body, status=code)

    # -- the phone ------------------------------------------------------------------------------
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
        segno.make(url, error="m").save(buf, kind="svg", scale=4, border=2, dark="#e8e9f0", light=None, xmldecl=False, svgns=True)
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
        sp = getattr(getattr(st, "jam", None), "spotify", None)
        signed_in = bool(sp and sp.signed_in)
        client = bool((cfg.get("spotify") or {}).get("client_id"))
        outs, _ = _outputs()
        hidden = cfg.get("jam_hidden_output") or hidden_output_help()["name"]
        hidden_found = any(hidden.lower() in o.lower() for o in outs)
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
            item("sound", "Sound devices", hidden_found, f"Spotify plays into {hidden}" if hidden_found else f"Install {hidden_output_help()['label']} for Spotify to play into"),
            item("models", "AI models", models, "Installed" if models else "Optional: better beats, sections and drums"),
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
                "mic_device": cfg.get("mic_device") or "", "jam_room_output": cfg.get("jam_room_output") or "",
                "jam_hidden_output": cfg.get("jam_hidden_output") or "", "hidden_help": hidden_output_help()}

    async def h_devices(self, request):
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self.devices))

    async def h_devices_set(self, request):
        body = await request.json()
        cfg = self.load_config()
        for k in ("mic_device", "jam_room_output", "jam_hidden_output"):
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
