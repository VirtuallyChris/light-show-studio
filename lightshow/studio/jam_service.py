"""Jam mode's place in the studio server: starting and stopping it, and its HTTP routes."""

from __future__ import annotations

import asyncio
import html
import os
import time

from aiohttp import web

from . import loopback
from .jam import CastOutput, JamEngine, JamLibrary, JamRunner, JamSettings, VirtualOutput
from .loopback import Gate
from .relay import Relay
from .spotify import PlayerAwareSpotify, PlayerState, SpotifyClient


# the start settings remembered for next time (and for a jam that starts itself)
LAST_KEYS = ("device", "speakers", "pc_device", "target_delay", "crossfade", "skip_jumps", "source")


class JamService:
    def __init__(self, studio, shows_dir: str, load_config, save_config, local_ip):
        self.studio, self.shows_dir = studio, shows_dir
        self.load_config, self.save_config, self.local_ip = load_config, save_config, local_ip
        self.library = JamLibrary(shows_dir)
        self.store = dict(load_config().get("spotify", {}))
        self.spotify = SpotifyClient(self.store, save=self._save_store)
        self.player = PlayerState()                     # the studio's own Spotify player, as its page reports it
        self.player_browser = "Microsoft Edge, hidden"
        self.player_enabled = True                      # False: a test copy that must not register a Spotify speaker
        from .player_browser import HiddenPlayer
        self.hidden = HiddenPlayer(f"http://127.0.0.1:{studio.port}/static/player.html",
                                   load_config().get("jam_hidden_output", "S/PDIF"))
        self.engine: JamEngine | None = None
        self.runner: JamRunner | None = None
        self.capture = None
        self.demo = None
        self.mode = ""
        self.source = ""                                # where Spotify plays for the running jam: studio / app
        self.auto_armed = True                          # auto-start fires on playback beginning, not on it carrying on
        self.auto_started = False                       # the running jam was started by playback, not by a button
        self.auto_error = ""

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
            web.get("/api/spotify/token", self.token),
            web.post("/api/jam/sdk_state", self.sdk_state),
            web.post("/api/jam/prefs", self.prefs),
            web.post("/api/jam/player/open", self.player_open),
            web.post("/api/jam/player/transfer", self.player_transfer),
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
                          crossfade=max(0.0, min(12.0, float(body.get("crossfade", 0.0)))),
                          skip_jumps=bool(body.get("skip_jumps", False)))
        hidden_name = self._hidden_output_name()
        if not demo and hidden_name is None:
            raise ValueError(f"there is no output named like {self.hidden.hidden_output!r} for Spotify to play into unheard")
        where = body.get("speakers", "cast")
        use_cast = where == "cast" and self.studio.cast.cast is not None
        if where == "cast" and not use_cast:
            where = "none"
        if use_cast:
            ip = self.local_ip() or "127.0.0.1"
            output = CastOutput(self.studio.cast, lambda sid: f"http://{ip}:{self.studio.port}/api/jam/stream.wav?sid={sid}", self._speaker_offset)
        elif where == "pc":
            from .audio_io import ProcessOutput
            pc_device = self.load_config().get("jam_room_output") or None          # the default output, unless set
            if not demo and self._same_device(hidden_name, pc_device):
                raise ValueError(f"Windows' default output is {hidden_name}, the output Spotify plays into unheard. "
                                 "Set your speakers or headphones as the default output in Windows' sound settings.")
            from .local_output import DEFAULT_OFFSET_S
            offset = float(self.load_config().get("pc_output_offset_ms", DEFAULT_OFFSET_S * 1000)) / 1000.0
            output = ProcessOutput(pc_device, offset)       # plays from its own process: the analysis cannot starve it
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
            if body.get("source", "app") == "studio":
                if not self.spotify.can_stream:
                    raise PermissionError("the studio player needs a new Spotify sign-in: press Sign in to Spotify again")
                spotify = PlayerAwareSpotify(self.spotify, self.player)    # the hidden player
                if self.player_enabled:
                    self.hidden.start()
            from .audio_io import ProcessCapture
            self.capture = ProcessCapture(relay, hidden_name, gate)   # records the hidden output, from its own process
            self.capture.start()
        # the demo learns its synthetic songs too, but in a folder of its own, not among the real ones
        library = JamLibrary(os.path.join(self.shows_dir, "_jam", "demo")) if demo else self.library
        from . import structure_model
        from .genre import WORKER
        genre_fn = WORKER.classify_audio if WORKER.available() else None
        segment_fn = structure_model.run_audio if structure_model.available() else None
        self.engine = JamEngine(relay, spotify, library, output, gate, cfg, genre_fn=genre_fn, segment_fn=segment_fn)
        self.runner = JamRunner(self.engine, file_status=self._file_status)
        self.mode = ("demo" if demo else "spotify") + (" to " + output.name if where in ("cast", "pc") and use_cast or where == "pc" else ", preview only (no speakers)")
        if body.get("opener") and use_cast and not demo:
            self.engine.arm_handover()
        self.runner.start()
        if demo:
            self.demo.start()
        self.engine.say(f"jam started: {self.mode}, aiming {cfg.target_delay:.0f}s behind")
        self.source = "demo" if demo else body.get("source", "app")
        self.auto_started = bool(body.get("auto"))
        if not demo:
            cfg_all = self.load_config()
            cfg_all["jam_last"] = {k: body[k] for k in LAST_KEYS if k in body}
            self.save_config(cfg_all)
        return self._state()

    def close_player(self) -> None:
        """The studio is closing: Edge's sound goes back to the default output, and the player closes."""
        self.hidden.stop()

    def _hidden_output_name(self) -> str | None:
        try:
            import soundcard as sc
            return next((s.name for s in sc.all_speakers() if self.hidden.hidden_output.lower() in s.name.lower()), None)
        except Exception:  # noqa: BLE001
            return None

    def _want_player(self) -> bool:
        return (self.player_enabled and self.spotify.signed_in and self.spotify.can_stream
                and self._last().get("source", "studio") == "studio")

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

    @staticmethod
    def _same_device(capture: str | None, playback: str | None) -> bool:
        """Would the jam play on the very output it is recording? (None means the default output.)"""
        if capture == playback:
            return True
        try:
            import soundcard as sc
            default = sc.default_speaker().name
        except Exception:  # noqa: BLE001
            return False
        return (capture or default) == (playback or default)

    def _open_player(self) -> None:
        self.hidden.start()

    def _state(self) -> dict:
        if self._want_player():
            self.hidden.start()                             # cheap when it is already running
        base = {"running": self.engine is not None, "mode": self.mode, "redirect_uri": self.redirect_uri,
                "player": {**self.player.to_json(), "browser": self.player_browser, **self.hidden.to_json()},
                "spotify": {"signed_in": self.spotify.signed_in, "client_id": self.spotify.client_id, "error": self.spotify.last_error,
                            "can_stream": self.spotify.can_stream},
                "speaker_connected": self.studio.cast.cast is not None, "speaker": self.studio.cast.name,
                "last": self._last(), "auto": self._auto_on(), "auto_error": self.auto_error, "auto_started": self.auto_started}
        if self.engine is None:
            return base
        st = self.engine.state()
        st["spotify"] = {**base["spotify"], **{k: v for k, v in st["spotify"].items() if k in ("device", "error") and v}}
        if self.capture is not None:
            st["capture"] = {"device": self.capture.device_name or "default output", "level": round(self.capture.level, 3), "error": self.capture.error}
        out_error = getattr(self.engine.output, "error", "")
        if out_error:
            st["output_error"] = out_error
        out = self.engine.output
        st["output_kind"] = "cast" if isinstance(out, CastOutput) else "pc" if hasattr(out, "device_name") else "none"
        if self.capture is not None and getattr(self.capture, "lost", 0):
            st["capture"]["lost_s"] = round(self.capture.lost, 2)
        return {**base, **st, "now": time.monotonic()}

    async def _run(self, fn, *args):
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, fn, *args))
        except Exception as e:  # noqa: BLE001 - the UI shows the reason
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)

    # -- routes ---------------------------------------------------------------------------
    async def state(self, request):
        # Off the event loop: the light frames come in on it, and the engine's lock can be held
        # for a moment by a Spotify request. Blocking here stalled the lights.
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._state))

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

    # -- the studio's own Spotify player ----------------------------------------------------
    @staticmethod
    def _local(request) -> bool:
        """The studio listens on the whole network (Cast speakers fetch audio from it), but these
        routes hand out a Spotify token and steer playback: this computer only."""
        return (request.remote or "") in ("127.0.0.1", "::1", "localhost")

    async def token(self, request):
        if not self._local(request):
            raise web.HTTPForbidden()
        try:
            tok = await asyncio.get_running_loop().run_in_executor(None, self.spotify.token)
            return web.json_response({"token": tok})
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)}, status=401)

    async def sdk_state(self, request):
        if not self._local(request):
            raise web.HTTPForbidden()
        report = await request.json()
        p, now = self.player, time.monotonic()
        before_dev = p.device_id
        was_playing = p.track is not None and not p.paused and now - p.heard < 15.0
        p.update(report)
        job = self._after_report(report, before_dev, was_playing)
        if job is not None:
            asyncio.get_running_loop().run_in_executor(None, job)
        return web.json_response({"ok": True})

    def _after_report(self, report: dict, before_dev: str, was_playing: bool):
        """What a player report sets going, if anything: returns a job for a worker thread.

        - Something starts playing on Light Show Studio with no jam running: start one,
          with the settings last used. It fires on playback beginning, so after Stop the
          music can carry on without a jam until it is paused or changed.
        - The player came back under a new device id (the studio window was reloaded)
          while it was playing a jam: Spotify stopped when the old one vanished, so
          playback is handed back. The jam's buffer covers the gap.
        """
        p = self.player
        playing = p.ready and p.track is not None and not p.paused
        if "device_id" in report and p.device_id and before_dev and p.device_id != before_dev:
            if was_playing and self.engine is not None and self.source == "studio":
                dev = p.device_id
                return lambda: self.spotify.transfer_playback(dev, play=True)
            return None
        if not playing:
            self.auto_armed = True
            return None
        if self.auto_armed and self.engine is None and self._auto_on() and self.spotify.signed_in:
            self.auto_armed = False
            return self._auto_start
        if self.engine is not None:
            self.auto_armed = False
        return None

    def _auto_start(self) -> None:
        body = {**self._last(), "source": "studio", "auto": True}
        try:
            self._start(body)
            self.auto_error = ""
        except Exception as e:  # noqa: BLE001 - shown in the Jam panel
            self.auto_error = f"{type(e).__name__}: {e}"

    def _last(self) -> dict:
        return {"source": "studio", "speakers": "pc", "target_delay": 45.0, **self.load_config().get("jam_last", {})}

    def _auto_on(self) -> bool:
        return bool(self.load_config().get("jam_auto", True))

    async def prefs(self, request):
        """Settings the panel saves as they are changed: auto-start, and where Spotify plays."""
        if not self._local(request):
            raise web.HTTPForbidden()
        body = await request.json()
        cfg = self.load_config()
        if "auto" in body:
            cfg["jam_auto"] = bool(body["auto"])
            self.auto_armed = True
        if body.get("source") in ("studio", "app"):
            cfg["jam_last"] = {**cfg.get("jam_last", {}), "source": body["source"]}
        self.save_config(cfg)
        return web.json_response(self._state())

    async def player_open(self, request):
        return await self._run(lambda: (self._open_player(), self._state())[1])

    async def player_transfer(self, request):
        if not self._local(request):
            raise web.HTTPForbidden()
        if not self.player.device_id:
            return web.json_response({"error": "the studio player is not ready yet"}, status=400)
        return await self._run(lambda: {"ok": self.spotify.transfer_playback(self.player.device_id)})

    async def signout(self, request):
        self.spotify.sign_out()
        return web.json_response(self._state())
