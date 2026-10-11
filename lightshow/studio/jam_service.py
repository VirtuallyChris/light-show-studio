"""Jam mode's place in the studio server: starting and stopping it, and its HTTP routes."""

from __future__ import annotations

import asyncio
import html
import os
import re
import threading
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
            web.post("/api/jam/colors", self.colors),
            web.get("/api/jam/art", self.art),
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
        # Speakers asked for (Cast or this PC) means the room always hears it: on the Cast speaker
        # when one is connected, otherwise on this PC. Only "none" is a silent preview. (A jam that
        # started by itself with "cast" saved and no speaker connected used to fall silent.)
        self.room_wanted = where in ("cast", "pc") and not demo
        if where in ("cast", "pc") and not demo:
            where = "cast" if self.studio.cast.cast is not None else "pc"
        elif where == "cast" and self.studio.cast.cast is None:
            where = "none"
        use_cast = where == "cast"
        output = self._output_for(where, hidden_name, check=not demo)
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

    def _output_for(self, where: str, hidden_name: str | None = None, check: bool = True):
        """The room's output: the Cast speaker, this PC's default output, or nothing (preview)."""
        if where == "cast" and self.studio.cast.cast is not None:
            ip = self.local_ip() or "127.0.0.1"
            # Raw audio. MP3 was tried to lighten the Wi-Fi load, but libsndfile's MP3 encoder
            # (1.2.2, the one installed) turned music into noise: 0.59 correlation with the original
            # and noise 36 dB above the music over 16 kHz. It sounded "like death" on the speakers.
            return CastOutput(self.studio.cast, lambda sid: f"http://{ip}:{self.studio.port}/api/jam/stream.wav?sid={sid}",
                              self._speaker_offset)
        if where == "pc":
            from .audio_io import ProcessOutput
            from .local_output import DEFAULT_OFFSET_S
            pc_device = self.load_config().get("jam_room_output") or None          # the default output, unless set
            if check and self._same_device(hidden_name if hidden_name is not None else self._hidden_output_name(), pc_device):
                raise ValueError(f"Windows' default output is {hidden_name or self.hidden.hidden_output}, the output Spotify plays "
                                 "into unheard. Set your speakers or headphones as the default output in Windows' sound settings.")
            offset = float(self.load_config().get("pc_output_offset_ms", DEFAULT_OFFSET_S * 1000)) / 1000.0
            return ProcessOutput(pc_device, offset)         # plays from its own process: the analysis cannot starve it
        return VirtualOutput(time.monotonic)

    def _output_kind(self, out) -> str:
        return "cast" if isinstance(out, CastOutput) else "pc" if hasattr(out, "device_name") else "none"

    def _follow_cast(self) -> None:
        """Casting moves the jam: a Cast speaker connected mid-jam takes the room's sound, and when
        it goes away the sound comes back to this PC. Checked every time the page asks for the state."""
        e = self.engine
        if e is None or self.source == "demo" or getattr(self, "_switching", False):
            return
        kind = self._output_kind(e.output)
        if kind == "none" and not getattr(self, "room_wanted", False):
            return                                      # a preview stays a preview
        cast = self.studio.cast
        if kind == "cast" and self._cast_stuck(e):
            # The speaker is not playing what it was sent (unreachable, or dropped out): the room
            # would sit silent with its clock frozen. Move to this PC until Cast is pressed again.
            self._cast_failed = cast.cast
            e.say(f"{cast.name or 'the Cast speaker'} is not playing (not reachable?): the room is on this PC now. Press Cast to try it again.")
            # let go of it, so the studio stops showing it as casting and a new Cast starts clean
            threading.Thread(target=cast.disconnect, daemon=True).start()
        if kind == "cast":
            self._maybe_auto_sync(e)
        usable = cast.cast is not None and cast.cast is not getattr(self, "_cast_failed", None)
        want = "cast" if usable else "pc"
        if want != kind:
            self._switching = True
            threading.Thread(target=self._switch_output, args=(want,), daemon=True).start()

    CAST_GRACE_S = 15.0

    def _cast_stuck(self, e) -> bool:
        """Has the Cast speaker failed to play for CAST_GRACE_S while it should have been?"""
        now = time.monotonic()
        if e.gate.paused or not e.streaming:
            self._cast_ok_at = now
            return False
        status = getattr(self.studio.cast, "status", None)
        if not callable(status) or status().get("state") in ("PLAYING", "BUFFERING", "PAUSED"):
            self._cast_ok_at = now
            return False
        since = max(getattr(self, "_cast_ok_at", 0.0), getattr(e.output, "started_at", 0.0))
        return now - since > self.CAST_GRACE_S

    def _switch_output(self, want: str) -> None:
        """Hand the room's sound to another output at the very point it had reached."""
        try:
            e = self.engine
            if e is None:
                return
            new = self._output_for(want)
            with e.lock:
                rc = e.room_capture_time()
                old = e.output
                old.stop()
                e.output = new
                if e.streaming:
                    start = rc if rc is not None else max(e.relay.oldest_time, e.relay.capture_time - e.cfg.target_delay)
                    new.start_stream(e.relay.open_stream(start, pace=not isinstance(new, VirtualOutput)))
                    if e.gate.paused:
                        new.pause()
                self.mode = "spotify to " + new.name
                e.say(f"the room is now on {new.name}")
        except Exception as ex:  # noqa: BLE001 - shown in the log; the jam carries on where it was
            if self.engine is not None:
                self.engine.say(f"could not move the sound to {want}: {ex}")
        finally:
            self._switching = False

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
        self._follow_cast()
        base = {"running": self.engine is not None, "mode": self.mode, "redirect_uri": self.redirect_uri,
                "player": {**self.player.to_json(), "browser": self.player_browser, **self.hidden.to_json()},
                "spotify": {"signed_in": self.spotify.signed_in, "client_id": self.spotify.client_id, "error": self.spotify.last_error,
                            "can_stream": self.spotify.can_stream},
                "speaker_connected": self.studio.cast.cast is not None, "speaker": self.studio.cast.name,
                "last": self._last(), "auto": self._auto_on(), "auto_error": self.auto_error, "auto_started": self.auto_started,
                "jam_colors": self._jam_colors(), "track_colors": self.library.colors()}
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
        st["output_kind"] = self._output_kind(out)
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
        if getattr(self, "_syncing", False):
            return web.json_response({"error": "a sync check is already listening; its result will be in the Jam log"}, status=409)
        self._syncing = True

        def run():
            try:
                return self._run_sync()
            finally:
                self._syncing = False
        return await self._run(run)

    def _run_sync(self) -> dict:
        """Listen to the room, measure how late the Cast speaker really is, and keep a confident answer."""
        from .calibrate import relay_sync_check
        r = relay_sync_check(self.engine, log=lambda *a: None)
        name = self.studio.cast.name
        if name and r["confidence"] >= 0.5 and abs(r["offset_ms"]) <= 1500:   # the correction is relative: add it
            cfg = self.load_config()
            new = int(round(self._speaker_offset() * 1000 + r["offset_ms"]))
            cfg.setdefault("cast_offsets", {})[name] = new
            self.save_config(cfg)
            r["saved_for"], r["speaker_offset_ms"] = name, new
        return r

    AUTO_SYNC_AFTER_S = 8.0

    def _maybe_auto_sync(self, e) -> None:
        """Once a Cast speaker has been playing the jam for a few seconds (a new cast, a reconnect, a
        restart), listen to the room and correct its delay. A speaker's delay can change when it or
        its group reconnects, and nothing else would notice. It only listens; nothing is played."""
        out = e.output
        if not isinstance(out, CastOutput) or getattr(self, "_auto_sync_off", False) or getattr(self, "_syncing", False):
            return
        started = getattr(out, "started_at", 0.0)
        if not started or started == getattr(self, "_synced_start", None) or e.gate.paused:
            return
        status = getattr(self.studio.cast, "status", None)
        if time.monotonic() - started < self.AUTO_SYNC_AFTER_S or not callable(status) or status().get("state") != "PLAYING":
            return
        self._synced_start, self._syncing = started, True

        def run():
            name = self.studio.cast.name or "the speaker"
            try:
                r = self._run_sync()
                if r.get("saved_for"):
                    e.say(f"sync check: {name} is {r['speaker_offset_ms']} ms behind (corrected by {r['offset_ms']:+d} ms)")
                else:
                    e.say(f"sync check: could not hear {name} clearly enough; keeping its delay")
            except Exception as ex:  # noqa: BLE001 - no microphone, or it is busy: say so once and stop trying
                self._auto_sync_off = True
                e.say(f"automatic sync check is off: the microphone could not be used ({type(ex).__name__})")
            finally:
                self._syncing = False
        threading.Thread(target=run, daemon=True).start()

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

    RECONNECT_AFTER_S = 3.0      # a speaker that had played less of the stream than this was only starting it

    async def stream(self, request):
        """The delayed audio, as an endless WAV, for the speakers to pull.

        A speaker that loses its connection (Wi-Fi) asks for the same stream again. Picking up
        where the connection broke would put the speaker's clock out of step with what it plays,
        and the lights would drift; so the stream is restarted cleanly at the point the room had
        reached, with its clock starting over too.
        """
        eng = self.engine
        out = eng.output if eng else None
        stream = getattr(out, "stream", None)
        sid = request.query.get("sid", "")
        if stream is None or (sid and str(getattr(out, "sid", "")) != sid):
            raise web.HTTPNotFound()
        served = self.__dict__.setdefault("_served", {})        # sid -> seconds of audio sent
        # Judged by what the speaker has played, not by what was sent: up to 20 s go into the
        # network at once, so a speaker that only probed the stream may already have "had" a lot.
        try:
            played = float(out.position()) if isinstance(out, CastOutput) and sid in served else 0.0
        except Exception:  # noqa: BLE001
            played = 0.0
        if isinstance(out, CastOutput) and played >= self.RECONNECT_AFTER_S:
            eng.say(f"the speaker lost its stream (Wi-Fi) and came back: restarting it where the room was")
            threading.Thread(target=self._restart_cast, args=(out,), daemon=True).start()
            raise web.HTTPGone()
        served.setdefault(sid, 0.0)
        resp = web.StreamResponse(headers={"Content-Type": "audio/wav", "Cache-Control": "no-store", "Accept-Ranges": "none"})
        await resp.prepare(request)
        loop = asyncio.get_running_loop()
        try:
            await resp.write(stream.header())
            frame_bytes = 2 * stream.relay.channels
            while True:
                data = await loop.run_in_executor(None, stream.read, 8192, 0.5)
                if data is None:
                    continue
                if not data:
                    break
                served[sid] = served.get(sid, 0.0) + len(data) / frame_bytes / stream.relay.sr
                await resp.write(data)
        except (ConnectionResetError, ConnectionError, OSError, asyncio.CancelledError, RuntimeError):
            if eng is not None and served.get(sid, 0.0) >= self.RECONNECT_AFTER_S and getattr(out, "stream", None) is stream:
                eng.say("the speaker's connection dropped (Wi-Fi)")
        return resp

    def _restart_cast(self, out) -> None:
        e = self.engine
        if e is None or e.output is not out:
            return
        with e.lock:
            rc = e.room_capture_time()
            start = rc if rc is not None else max(e.relay.oldest_time, e.relay.capture_time - e.cfg.target_delay)
            out.start_stream(e.relay.open_stream(start))
            if e.gate.paused:
                out.pause()

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
        if self.studio.cast.cast is not None:
            body["speakers"] = "cast"                       # casting: the room is the Cast speaker
        try:
            self._start(body)
            self.auto_error = ""
        except Exception as e:  # noqa: BLE001 - shown in the Jam panel
            self.auto_error = f"{type(e).__name__}: {e}"

    def _last(self) -> dict:
        return {"source": "studio", "speakers": "pc", "target_delay": 45.0, **self.load_config().get("jam_last", {})}

    # extras: two more colors a few of the small accents take now and then (orange and red at Halloween)
    DEFAULT_COLORS = {"mode": "album", "primary": "#8000ff", "secondary": "#00ff00", "extras": False, "accent1": "#ff6000", "accent2": "#ff0000"}

    def _jam_colors(self) -> dict:
        """The jam's own colors: every song in a primary and a secondary, or ("album") its cover's."""
        return {**self.DEFAULT_COLORS, **self.load_config().get("jam_colors", {})}

    async def colors(self, request):
        """Set the jam's colors ({mode, primary, secondary}), or one track's ({track_id, primary,
        secondary}; without the colors, the track goes back to the jam's)."""
        if not self._local(request):
            raise web.HTTPForbidden()
        body = await request.json()
        hexcolor = lambda v: v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else None
        if body.get("track_id"):
            self.library.set_colors(str(body["track_id"]), hexcolor(body.get("primary")), hexcolor(body.get("secondary")))
        else:
            cfg = self.load_config()
            cur = {**self.DEFAULT_COLORS, **cfg.get("jam_colors", {})}
            if body.get("mode") in ("pick", "album"):
                cur["mode"] = body["mode"]
            for k in ("primary", "secondary", "accent1", "accent2"):
                if hexcolor(body.get(k)):
                    cur[k] = hexcolor(body[k])
            if "extras" in body:
                cur["extras"] = bool(body["extras"])
            cfg["jam_colors"] = cur
            self.save_config(cfg)
        return web.json_response(self._state())

    def _auto_on(self) -> bool:
        return bool(self.load_config().get("jam_auto", True))

    async def art(self, request):
        """Album art, relayed so the studio page may read its colors. Spotify's image server only."""
        url = request.query.get("url", "")
        if not url.startswith("https://i.scdn.co/image/"):
            raise web.HTTPForbidden()
        cache = self.__dict__.setdefault("_art", {})
        if url not in cache:
            import urllib.request

            def fetch():
                with urllib.request.urlopen(url, timeout=10) as r:
                    return r.read(2_000_000), r.headers.get("Content-Type", "image/jpeg")
            try:
                cache[url] = await asyncio.get_running_loop().run_in_executor(None, fetch)
            except Exception:  # noqa: BLE001
                raise web.HTTPNotFound()
            while len(cache) > 40:
                cache.pop(next(iter(cache)))
        body, ctype = cache[url]
        return web.Response(body=body, content_type=ctype.split(";")[0])

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
