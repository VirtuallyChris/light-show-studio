"""Jam mode's place in the studio server: starting and stopping it, and its HTTP routes."""

from __future__ import annotations

import asyncio
import html
import os
import time

from aiohttp import web

from . import loopback
from .jam import CastOutput, JamEngine, JamLibrary, JamRunner, JamSettings, VirtualOutput
from .loopback import Gate, LoopbackCapture
from .relay import Relay
from .spotify import SpotifyClient


class JamService:
    def __init__(self, studio, shows_dir: str, load_config, save_config, local_ip):
        self.studio, self.shows_dir = studio, shows_dir
        self.load_config, self.save_config, self.local_ip = load_config, save_config, local_ip
        self.library = JamLibrary(shows_dir)
        self.store = dict(load_config().get("spotify", {}))
        self.spotify = SpotifyClient(self.store, save=self._save_store)
        self.engine: JamEngine | None = None
        self.runner: JamRunner | None = None
        self.capture = None
        self.demo = None
        self.mode = ""

    def _save_store(self) -> None:
        cfg = self.load_config()
        cfg["spotify"] = dict(self.store)
        self.save_config(cfg)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.studio.port}/api/spotify/callback"

    def routes(self) -> list:
        return [
            web.get("/api/jam/state", self.state),
            web.get("/api/jam/outputs", self.outputs),
            web.post("/api/jam/start", self.start),
            web.post("/api/jam/stop", self.stop),
            web.post("/api/jam/handover", self.handover),
            web.post("/api/jam/link", self.link),
            web.post("/api/jam/synccheck", self.sync_check),
            web.post("/api/jam/demo/{action}", self.demo_action),
            web.get("/api/jam/analysis", self.analysis),
            web.get("/api/jam/stream.wav", self.stream),
            web.post("/api/spotify/client", self.set_client),
            web.get("/api/spotify/login", self.login),
            web.get("/api/spotify/callback", self.callback),
            web.post("/api/spotify/signout", self.signout),
        ]

    # -- lifecycle -----------------------------------------------------------------------
    def _speaker_offset(self) -> float:
        name = self.studio.cast.name
        cfg = self.load_config()
        return float(cfg.get("cast_offsets", {}).get(name, cfg.get("cast_offset_ms", 0))) / 1000.0

    def _file_status(self):
        """How much of a song from disk the speakers still have to play (for the opening-song hand-over)."""
        c = self.studio.cast
        if c.cast is None or not c.media_path:
            return None, False
        st = c.status()
        return max(0.0, float(st.get("duration") or 0.0) - float(st.get("position") or 0.0)), st.get("state") in ("PLAYING", "BUFFERING")

    def _start(self, body: dict) -> dict:
        self._stop()
        demo = bool(body.get("demo"))
        relay = Relay(sr=44100, channels=2, capacity_s=600.0)
        gate = Gate()
        cfg = JamSettings(target_delay=float(body.get("target_delay", 45.0)), max_gap=float(body.get("max_gap", 6.0)),
                          crossfade=max(0.0, min(12.0, float(body.get("crossfade", 0.0)))))
        use_cast = body.get("speakers", "cast") == "cast" and self.studio.cast.cast is not None
        if use_cast:
            ip = self.local_ip() or "127.0.0.1"
            output = CastOutput(self.studio.cast, lambda sid: f"http://{ip}:{self.studio.port}/api/jam/stream.wav?sid={sid}", self._speaker_offset)
        else:
            output = VirtualOutput(time.monotonic)
        if demo:
            from .jam_demo import DemoSpotify
            self.demo = DemoSpotify(relay, gate)
            spotify = self.demo
        else:
            if not self.spotify.signed_in:
                raise PermissionError("sign in to Spotify first")
            spotify = self.spotify
            self.capture = LoopbackCapture(relay, body.get("device") or None, gate)
            self.capture.start()
        # the demo learns its synthetic songs too, but in a folder of its own, not among the real ones
        library = JamLibrary(os.path.join(self.shows_dir, "_jam", "demo")) if demo else self.library
        from . import structure_model
        from .genre import WORKER
        genre_fn = WORKER.classify_audio if WORKER.available() else None
        segment_fn = structure_model.run_audio if structure_model.available() else None
        self.engine = JamEngine(relay, spotify, library, output, gate, cfg, genre_fn=genre_fn, segment_fn=segment_fn)
        self.runner = JamRunner(self.engine, file_status=self._file_status)
        self.mode = ("demo" if demo else "spotify") + (" to " + output.name if use_cast else ", preview only (no speakers)")
        if body.get("opener") and use_cast and not demo:
            self.engine.arm_handover()
        self.runner.start()
        if demo:
            self.demo.start()
        self.engine.say(f"jam started: {self.mode}, aiming {cfg.target_delay:.0f}s behind")
        return self._state()

    def _stop(self) -> None:
        if self.runner:
            self.runner.stop()
        if self.capture:
            self.capture.stop()
        if self.demo:
            self.demo.stop()
        if self.engine:
            self.engine.relay.close()
        self.engine = self.runner = self.capture = self.demo = None

    def _state(self) -> dict:
        base = {"running": self.engine is not None, "mode": self.mode, "redirect_uri": self.redirect_uri,
                "spotify": {"signed_in": self.spotify.signed_in, "client_id": self.spotify.client_id, "error": self.spotify.last_error},
                "speaker_connected": self.studio.cast.cast is not None, "speaker": self.studio.cast.name}
        if self.engine is None:
            return base
        st = self.engine.state()
        st["spotify"] = {**base["spotify"], **{k: v for k, v in st["spotify"].items() if k in ("device", "error") and v}}
        if self.capture is not None:
            st["capture"] = {"device": self.capture.device_name or "default output", "level": round(self.capture.level, 3), "error": self.capture.error}
        return {**base, **st, "now": time.monotonic()}

    async def _run(self, fn, *args):
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, fn, *args))
        except Exception as e:  # noqa: BLE001 - the UI shows the reason
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)

    # -- routes ---------------------------------------------------------------------------
    async def state(self, request):
        return web.json_response(self._state())

    async def outputs(self, request):
        return await self._run(loopback.list_outputs)

    async def start(self, request):
        return await self._run(self._start, await request.json())

    async def stop(self, request):
        return await self._run(lambda: (self._stop(), self._state())[1])

    async def handover(self, request):
        if self.engine is None:
            return web.json_response({"error": "start the jam first"}, status=400)
        return await self._run(lambda: (self.engine.arm_handover(), self._state())[1])

    async def link(self, request):
        body = await request.json()
        project = body.get("project")
        self.library.link(body["track_id"], None if project is None else str(project))
        return web.json_response({"ok": True})

    async def sync_check(self, request):
        if self.engine is None or not self.engine.streaming:
            return web.json_response({"error": "the speakers are not playing the jam yet"}, status=400)

        def run():
            from .calibrate import relay_sync_check
            r = relay_sync_check(self.engine)
            name = self.studio.cast.name
            if name and r["confidence"] >= 0.5:        # the correction is relative: add it to what was assumed
                cfg = self.load_config()
                new = int(round(self._speaker_offset() * 1000 + r["offset_ms"]))
                cfg.setdefault("cast_offsets", {})[name] = new
                self.save_config(cfg)
                r["saved_for"], r["speaker_offset_ms"] = name, new
            return r
        return await self._run(run)

    async def demo_action(self, request):
        if self.demo is None:
            return web.json_response({"error": "the demo is not running"}, status=400)
        action = request.match_info["action"]
        {"skip": self.demo.skip, "pause": self.demo.pause, "resume": self.demo.resume}.get(action, lambda: None)()
        return web.json_response({"ok": True})

    async def analysis(self, request):
        if self.engine is None:
            return web.json_response({"error": "not running"}, status=404)
        doc = await asyncio.get_running_loop().run_in_executor(None, self.engine.analysis_json, request.query.get("key", ""))
        return web.json_response(doc) if doc else web.json_response({"error": "no such track"}, status=404)

    async def stream(self, request):
        """The delayed audio, as an endless WAV, for the speakers to pull."""
        eng = self.engine
        out = eng.output if eng else None
        stream = getattr(out, "stream", None)
        if stream is None or (request.query.get("sid") and str(getattr(out, "sid", "")) != request.query["sid"]):
            raise web.HTTPNotFound()
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav", "Cache-Control": "no-store", "Accept-Ranges": "none"})
        await resp.prepare(request)
        loop = asyncio.get_running_loop()
        try:
            await resp.write(stream.header())
            while True:
                data = await loop.run_in_executor(None, stream.read, 8192, 0.5)
                if data is None:
                    continue
                if not data:
                    break
                await resp.write(data)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        return resp

    # -- Spotify sign-in --------------------------------------------------------------------
    async def set_client(self, request):
        body = await request.json()
        cid = str(body.get("client_id", "")).strip()
        if cid != self.store.get("client_id"):
            self.store.pop("refresh_token", None)
        self.store["client_id"] = cid
        self._save_store()
        return web.json_response(self._state())

    async def login(self, request):
        if not self.spotify.client_id:
            return web.Response(text="Enter your Spotify app's Client ID in the Jam panel first.", status=400)
        raise web.HTTPFound(self.spotify.auth_url(self.redirect_uri))

    async def callback(self, request):
        q = request.query
        page = lambda title, msg: web.Response(content_type="text/html", text=(
            "<!doctype html><meta charset=utf-8><title>Light Show Studio</title>"
            "<body style='font:16px system-ui;background:#0f1014;color:#e8eaf0;display:grid;place-items:center;height:100vh;margin:0'>"
            f"<div style='text-align:center'><h2>{html.escape(title)}</h2><p style='color:#8b92a3'>{html.escape(msg)}</p></div>"))
        if q.get("error"):
            return page("Spotify sign-in was cancelled", q["error"])
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.spotify.finish_auth, q.get("code", ""), q.get("state", ""), self.redirect_uri)
        except Exception as e:  # noqa: BLE001
            return page("Spotify sign-in failed", str(e))
        return page("Signed in to Spotify", "You can close this window and go back to the studio.")

    async def signout(self, request):
        self.spotify.sign_out()
        return web.json_response(self._state())
