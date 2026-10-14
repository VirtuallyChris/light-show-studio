"""Jam mode's place in the studio server: starting and stopping it, and its HTTP routes."""

from __future__ import annotations

import asyncio
import colorsys
import html
import ipaddress
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
DEFAULT_OPENER = "If Youre Too Shy (Let Me Know)"     # a jam opens with this saved show, if it is there


def extras_for(pair) -> list[str] | None:
    """The two "now and then" colors for a pair: the hues farthest from both, evenly spread across the
    wider gap between them on the color wheel, either side of its middle (purple and green get red and
    orange). The studio's
    jam.js does the same, so the phone shows what the lights will do."""
    if not pair or len(pair) < 2:
        return None
    hues = []
    for hx in pair[:2]:
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, s_, v = colorsys.rgb_to_hsv(r, g, b)
        if s_ < 0.2 or v < 0.2:
            return ["#ff6000", "#ff0000"]               # a grey or black: no hue to steer by
        hues.append(h * 360)
    a = (hues[1] - hues[0]) % 360
    start, span = (hues[0], a) if a >= 180 else (hues[1], 360 - a)
    out = []
    for k in (0.38, 0.62):                                  # either side of the point farthest from both
        r, g, b = colorsys.hsv_to_rgb(((start + span * k) % 360) / 360, 1, 1)
        out.append("#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255)))
    return out


class JamService:
    def __init__(self, studio, shows_dir: str, load_config, save_config, local_ip):
        self.studio, self.shows_dir = studio, shows_dir
        self.load_config, self.save_config, self.local_ip = load_config, save_config, local_ip
        self.library = JamLibrary(shows_dir)
        # learn ahead: queued songs learned before they play, from a copy found on YouTube (fetch_ahead)
        from .fetch_ahead import FetchAhead
        self.fetch = FetchAhead(self.library)
        self._assigned: dict[str, int] = {}                  # track id -> the pair it got in "mix"
        self._moods: dict[str, dict | None] = {}             # track id -> its learned mood (or None)
        # Party level, 0..1: how much of the show comes through. Below 1 the studio lays the song's main
        # color over every show, more and more opaque, so the room calms down without going dark.
        self.party_level = float(load_config().get("party_level", 1.0))
        self.store = dict(load_config().get("spotify", {}))
        self.spotify = SpotifyClient(self.store, save=self._save_store)
        self.player = PlayerState()                     # the studio's own Spotify player, as its page reports it
        # pre-listen: the studio's own player hears a party playlist ahead of time, through a silent jam
        from .prelisten import PreListen
        self.prelisten = PreListen(self.spotify, self.library,
                                   start_jam=lambda: self._start({"source": "studio", "speakers": "none", "target_delay": 20, "prelisten": True}),
                                   stop_jam=self._stop, device_id=lambda: self.player.device_id,
                                   fetch=self.fetch if self._fetch_on() else None)
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
            # the Party Hub: a phone app on the home network (Jam mode)
            web.get("/party", self.party_page),
            web.get("/party/manifest.webmanifest", self.party_manifest),
            web.get("/api/party/state", self.party_state),
            web.post("/api/party/colors", self.party_colors),
            web.post("/api/party/playback", self.party_playback),
            web.post("/api/party/level", self.party_level_set),
            web.get("/api/party/volumes", self.party_volumes),
            web.post("/api/party/volume", self.party_volume),
            web.post("/api/prelisten", self.prelisten_start),
            web.post("/api/prelisten/stop", self.prelisten_stop),
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
        o = getattr(self, "opener", None)
        if o and not o.get("cut"):
            gone = time.monotonic() - o["t0"]
            if o["via"] != "cast" or gone < 10:               # this PC's page plays it, or the speaker is still loading it
                return max(0.0, o["dur"] - gone), gone < o["dur"]
        c = self.studio.cast
        if c.cast is None or not c.media_path:
            return None, False
        st = c.status()
        return max(0.0, float(st.get("duration") or 0.0) - float(st.get("position") or 0.0)), st.get("state") in ("PLAYING", "BUFFERING")

    # How far behind the room is when songs are learned ahead. Less is livelier, at a cost: the studio hears of
    # a new song about a second late, a speaker holds only this much against a Wi-Fi hiccup, and a song is
    # lined up by ear 7 s in, so below that each song starts on Spotify's clock and may shift a little then.
    FETCH_DELAY_S = 3.0

    def _fetch_on(self) -> bool:
        """Learn ahead is on (the default where spotDL is installed): songs are learned before they play,
        so the room needs to be only seconds behind, not a song."""
        from . import fetch_ahead
        return bool(self.load_config().get("jam_fetch", True)) and fetch_ahead.available()

    def _start(self, body: dict) -> dict:
        self._stop()
        demo = bool(body.get("demo"))
        ahead = not demo and not body.get("prelisten") and self._fetch_on()
        if ahead and "target_delay" not in body:
            body = {**body, "target_delay": float(self.load_config().get("jam_fetch_delay", self.FETCH_DELAY_S))}
        relay = Relay(sr=44100, channels=2, capacity_s=600.0)
        gate = Gate()
        cfg = JamSettings(target_delay=float(body.get("target_delay", 90.0)), max_gap=float(body.get("max_gap", 6.0)),
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
        # A jam opens with one of your saved shows (If You're Too Shy, unless another is chosen): it plays
        # in the room with its lights while Spotify plays, unheard, behind it, and the room joins the jam as
        # it ends, a whole song behind. So the jam is always a song ahead: each song is heard to its end
        # before the room gets to it, and gets its full show from the first beat.
        # (learning ahead, the room is only seconds behind: no opening song is needed to fill a buffer)
        opener = None if demo or ahead or body.get("prelisten") or where not in ("cast", "pc") else self._opener_plan(body)
        if opener:
            cfg.target_delay = max(20.0, opener[1] - 2.0)
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
        if ahead:
            self.engine.on_queue = lambda tracks: self.fetch.submit(tracks, first=True)
            self.engine.on_mismatch = self.fetch.mismatched
        self.mode = ("demo" if demo else "spotify") + (" to " + output.name if where in ("cast", "pc") and use_cast or where == "pc" else ", preview only (no speakers)")
        self.runner.start()
        if demo:
            self.demo.start()
        self.opener = None
        if opener:
            self._begin_opener(*opener, via=where)
        self.engine.say(f"jam started: {self.mode}, aiming {cfg.target_delay:.0f}s behind")
        self.source = "demo" if demo else body.get("source", "app")
        self.auto_started = bool(body.get("auto"))
        if body.get("prelisten"):
            self.mode = "pre-listen: learning songs, nothing plays in the room"
        if not demo and not body.get("prelisten"):             # a pre-listen never becomes the jam you start next
            cfg_all = self.load_config()
            last = {k: body[k] for k in LAST_KEYS if k in body}
            if ahead:                                        # the delay learning ahead is kept on its own
                cfg_all["jam_fetch_delay"] = float(last.pop("target_delay", self.FETCH_DELAY_S))
                last["target_delay"] = cfg_all.get("jam_last", {}).get("target_delay", 90.0)
            cfg_all["jam_last"] = last
            self.save_config(cfg_all)
        return self._state()

    # -- the opening song --------------------------------------------------------------------------------
    def _opener_choice(self) -> str:
        """The saved show a jam opens with ("" for none: the jam starts 90 s behind instead)."""
        cfg = self.load_config()
        name = cfg.get("jam_opener", DEFAULT_OPENER)
        return name if name and self._length(name) >= 60 else ""

    def _length(self, name: str) -> float:
        length_of = getattr(self.studio, "_show_length", None)
        return float(length_of(name)) if length_of else 0.0

    def _opener_plan(self, body: dict) -> tuple[str, float] | None:
        name = body["opener_show"] if "opener_show" in body else self._opener_choice()
        length = self._length(name) if name else 0.0
        return (name, length) if name and length >= 60 else None

    def _begin_opener(self, name: str, length: float, via: str) -> None:
        """Play the opening show in the room (its song on the jam's speakers, or this page on this PC, and
        its lights) while Spotify fills the buffer behind it."""
        import uuid
        self.engine.open_with_song()                          # the room joins the jam as the opener ends
        if via == "cast" and self.studio.cast.cast is not None:
            url, path = self.studio._show_song(name)
            self.studio.cast.play(url, path, name, 0.0)
        else:
            via = "pc"                                        # this page plays it on this PC
        self.opener = {"id": "opener", "name": "Opening song", "show": name, "via": via, "device": self.studio.cast.name if via == "cast" else "",
                       "run": uuid.uuid4().hex[:8], "t0": time.monotonic(), "dur": length, "rehearsal": False}
        try:                                                  # Spotify starts now, unheard, filling the buffer
            np = self.engine.spotify.now_playing()
            if np is not None and not np.playing:
                self.engine.spotify.resume()
        except Exception:  # noqa: BLE001 - nothing to resume yet: the jam fills as soon as music plays
            pass
        self.engine.say(f"opening with {name}: the room joins the jam as it ends, a song behind")

    def opener_status(self) -> dict:
        """The opening song as the page plays it (shaped like a cue): over once the jam has the speakers."""
        o = getattr(self, "opener", None)
        if not o:
            return {}
        done = self.engine is None or self.engine.streaming or o.get("cut") or time.monotonic() - o["t0"] > o["dur"] + 10
        st = {k: v for k, v in o.items() if k not in ("t0", "cut")}
        st["phase"] = "done" if done else "show"
        st["progress"] = round(min(1.0, (time.monotonic() - o["t0"]) / max(1.0, o["dur"])), 3)
        return st

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
        if getattr(self, "fetch", None) is not None:
            self.fetch.forget()                              # what is under way finishes; nothing new starts
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
                "jam_colors": self._jam_colors(), "track_colors": self.library.colors(), "party_level": self.party_level,
                "cue": self._room_cue(),
                "prelisten": self.prelisten.status(), "opener_choice": self._opener_choice(),
                "fetch": {**self.fetch.status(), "on": self._fetch_on(),
                          "delay": float(self.load_config().get("jam_fetch_delay", self.FETCH_DELAY_S))}}
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
        played = [st.get("room"), st.get("ahead"), *(st.get("queue") or [])]
        for t in played:                                     # learning ahead: where each first listen stands
            if t and t.get("kind") == "live" and t.get("id") and base.get("fetch", {}).get("on"):
                got = self.fetch.state_of(t["id"])
                if got:
                    t["learning"], t["learning_why"] = got
        return {**base, **st, "now": time.monotonic(), "assigned_colors": self._assign_colors(played)}

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
        body = await request.json()
        if self.prelisten.busy() and self.prelisten.fetch is None:   # a real jam always wins (one learning from copies needs no Spotify)
            await asyncio.get_running_loop().run_in_executor(None, self.prelisten.stop, "a jam was started")
        return await self._run(self._start, body)

    # -- pre-listen ------------------------------------------------------------------------------
    async def prelisten_start(self, request):
        """Learn a playlist's songs (or the queue's) ahead of the party: {source: a playlist link, or "queue"}."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        if self.engine is not None and not self.prelisten.busy() and self.prelisten.fetch is None:
            return web.json_response({"error": "stop the jam first: Spotify can only play one thing at a time"}, status=400)
        source = str((await request.json()).get("source") or "").strip()
        return await self._run(self.prelisten.start, source)

    async def prelisten_stop(self, request):
        if not self._lan(request):
            raise web.HTTPForbidden()
        return await self._run(self.prelisten.stop)

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
        if self._fetch_on():
            body.pop("target_delay", None)                  # learning ahead keeps its own (short) delay
        if self.studio.cast.cast is not None:
            body["speakers"] = "cast"                       # casting: the room is the Cast speaker
        try:
            self._start(body)
            self.auto_error = ""
        except Exception as e:  # noqa: BLE001 - shown in the Jam panel
            self.auto_error = f"{type(e).__name__}: {e}"

    def _opener_room(self) -> dict | None:
        """The opening song as the Party Hub shows it in the room (one of your saved shows, from disk)."""
        o = self.opener_status()
        if o.get("phase") != "show":
            return None
        return {"id": None, "label": o["show"], "name": o["show"], "artists": [], "image": None, "kind": "custom", "opener": True,
                "duration": o["dur"], "position": round(o["progress"] * o["dur"], 1), "playing": True, "colors": None, "own": False,
                "theme_off": False}

    def _room_cue(self) -> dict:
        """What the room is playing instead of the jam: a cue, else the opening song."""
        cue = self.studio.cues.status() if getattr(self.studio, "cues", None) else {}
        if cue.get("phase") and cue.get("phase") != "done":
            if getattr(self, "opener", None):                 # a cue cut the opening song short: the jam follows the cue
                self.opener["cut"] = True
            return cue
        return self.opener_status() or cue

    def _last(self) -> dict:
        return {"source": "studio", "speakers": "pc", "target_delay": 90.0, **self.load_config().get("jam_last", {})}

    # extras: two more colors a few of the small accents take now and then (orange and red at Halloween)
    DEFAULT_COLORS = {"on": True, "mode": "album", "primary": "#8000ff", "secondary": "#00ff00", "extras": False, "accent1": "#ff6000", "accent2": "#ff0000",
                      # "mix": each song gets one of these pairs, by its vibe, never the same pair twice in a row
                      "pairs": [["#8000ff", "#00ff00"], ["#8000ff", "#ff6000"], ["#00ff00", "#ff6000"], ["#ff0000", "#0040ff"]],
                      "rainbow": True}                     # in "mix", a song that goes all out gets rainbow drops

    def _vibe_order(self, track_id: str, n: int) -> list[int]:
        """Which pairs suit a track, best first, from what was learned about it the last time it played
        (unknown tracks take turns). The order follows the default pairs: purple/green spooky, purple/orange
        warm, green/orange bright, red/blue hard."""
        if track_id not in self._moods:                     # a learned file is megabytes: read each once
            self._moods[track_id] = ((self.library.learned(track_id) or {}).get("analysis") or {}).get("mood")
        mood = self._moods[track_id]
        if not mood:
            k = len(self._assigned)
            return [(k + i) % n for i in range(n)]
        hard, warm = mood.get("drive", 0) > 0.62, mood.get("mode") == "major"
        pref = ([2, 1, 3, 0] if warm else [3, 0, 1, 2]) if hard else ([1, 2, 0, 3] if warm else [0, 1, 3, 2])
        return [i for i in pref if i < n] + [i for i in range(n) if i not in pref]

    def _assign_colors(self, tracks: list) -> dict:
        """In "mix": the pair each of these tracks (in play order) gets. Once given, a track keeps its pair."""
        jc = self._jam_colors()
        pairs = jc.get("pairs") or []
        if jc.get("mode") != "mix" or not pairs:
            return {}
        out, prev = {}, None
        for t in tracks:
            tid = (t or {}).get("id")
            if not tid or t.get("kind") == "custom":
                continue
            if tid not in self._assigned:
                order = self._vibe_order(tid, len(pairs))
                self._assigned[tid] = next((i for i in order if i != prev), order[0])
            prev = self._assigned[tid]
            out[tid] = pairs[prev % len(pairs)]
        return out

    def _jam_colors(self) -> dict:
        """The jam's own colors: every song in a primary and a secondary, or ("album") its cover's."""
        return {**self.DEFAULT_COLORS, **self.load_config().get("jam_colors", {})}

    # -- the Party Hub ---------------------------------------------------------------------
    @staticmethod
    def _lan(request) -> bool:
        """The Party Hub answers phones on the home network (and this computer), never the internet."""
        try:
            ip = ipaddress.ip_address((request.remote or "").split("%")[0])
        except ValueError:
            return False
        return ip.is_private or ip.is_loopback

    async def party_page(self, request):
        if not self._lan(request):
            raise web.HTTPForbidden()
        resp = web.FileResponse(os.path.join(os.path.dirname(__file__), "static", "party", "index.html"))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def party_manifest(self, request):
        return web.json_response({
            "name": "Party Hub", "short_name": "Party Hub", "start_url": "/party", "scope": "/party",
            "display": "standalone", "background_color": "#0e0f14", "theme_color": "#0e0f14",
            "icons": [{"src": "/static/party/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/party/icon-512.png", "sizes": "512x512", "type": "image/png"}]},
            content_type="application/manifest+json")

    def _party(self) -> dict:
        """What the Party Hub shows: the song in the room, what comes next, and each one's colors."""
        st = self._state()
        jc, own_all, assigned = st.get("jam_colors", {}), st.get("track_colors", {}), st.get("assigned_colors", {})

        def colors(t):
            if t.get("kind") == "custom":
                return None, False                       # your own show: its colors are its own
            own = own_all.get(t.get("id"))
            if not jc.get("on", True) or (own and own.get("off")):
                return None, False                       # out of the party's colors: its cover's
            if own:
                return [own["primary"], own["secondary"]], True
            if jc.get("mode") == "pick":
                return [jc["primary"], jc["secondary"]], False
            if jc.get("mode") == "mix":
                return assigned.get(t.get("id")), False
            return None, False                           # the album cover's colors: worked out in the studio

        def item(t, extra=None):
            if not t:
                return None
            pair, own = colors(t)
            return {"id": t.get("id"), "label": t.get("label"), "name": t.get("name"), "artists": t.get("artists"),
                    "image": t.get("image"), "art": t.get("art") or t.get("image"), "kind": t.get("kind"), "duration": t.get("duration"), "colors": pair, "own": own,
                    "theme_off": bool((own_all.get(t.get("id")) or {}).get("off")),
                    "only": bool((own_all.get(t.get("id")) or {}).get("only")), **(extra or {})}
        room, ahead = st.get("room"), st.get("ahead")
        up = ([ahead] if ahead and (not room or ahead.get("key") != room.get("key")) else []) + list(st.get("queue") or [])
        return {"running": bool(st.get("running")), "streaming": bool(st.get("streaming")), "paused": bool(st.get("paused")),
                "mode": st.get("mode"), "delay": st.get("delay"), "target_delay": st.get("target_delay"),
                "color_mode": jc.get("mode"), "theme_on": bool(jc.get("on", True)), "party_level": self.party_level,
                "theme": ([jc.get("primary"), jc.get("secondary")] if jc.get("mode") == "pick" else jc.get("pairs") if jc.get("mode") == "mix" else []),
                "rooms": self.studio.rooms.list() if getattr(self.studio, "rooms", None) else [],
                "cues": self.studio.cues.list() if getattr(self.studio, "cues", None) else [],
                "prelisten": self.prelisten.status() if getattr(self, "prelisten", None) else {},
                "cue": self.studio.cues.status() if getattr(self.studio, "cues", None) else {},
                "room": (self._opener_room() if hasattr(self, "_opener_room") else None) or item(room, {"position": room.get("position"), "playing": room.get("playing")} if room else None),
                "upcoming": [item(t) for t in up[:12]]}

    async def party_state(self, request):
        if not self._lan(request):
            raise web.HTTPForbidden()
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    async def party_colors(self, request):
        """From the phone: a track's own two colors (kept for next time), a track out of the party's colors
        ({track_id, theme: false}: its cover's), back to the jam's, or the party's colors on or off ({on})."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        hexcolor = lambda v: v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else None
        if not body.get("track_id") and "on" in body:
            self._set_theme_on(bool(body["on"]))
        elif not body.get("track_id"):
            return web.json_response({"error": "no track"}, status=400)
        else:
            self.library.set_colors(str(body["track_id"]), hexcolor(body.get("primary")), hexcolor(body.get("secondary")),
                                    off=body.get("theme") is False, only=bool(body.get("only")))
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    async def party_playback(self, request):
        """Play, pause or skip on Spotify, the way the phone's own controls would."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        action = (await request.json()).get("action")
        if self.demo is not None:
            {"next": self.demo.skip, "pause": self.demo.pause, "resume": self.demo.resume}.get(action, lambda: None)()
            return web.json_response({"ok": True})
        fn = {"pause": self.spotify.pause, "resume": self.spotify.resume, "next": self.spotify.next_track}.get(action)
        if fn is None:
            return web.json_response({"error": "unknown action"}, status=400)
        ok = await asyncio.get_running_loop().run_in_executor(None, fn)
        return web.json_response({"ok": bool(ok)})

    async def party_volumes(self, request):
        """The speaker the jam plays on (its volume is the cast's), and every speaker and group's own."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        cast = self.studio.cast

        rooms = getattr(self.studio, "rooms", None)

        def read():
            st = cast.status()
            devices = cast.device_volumes()
            # a speaker its room has muted shows the level it is set to (it plays at that once the room
            # is back), not what a fade has it at right now
            muted = rooms.muted() if rooms else {}
            for d in devices:
                if d["name"] in muted:
                    d["volume"], d["muted"] = muted[d["name"]], True
            return {"cast": {"name": st["device"], "volume": st["volume"]} if st["connected"] else None,
                    "devices": devices}
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, read))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    async def party_volume(self, request):
        """Set a volume: the cast's ({level}), or one speaker's or group's ({name, level})."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        level = max(0.0, min(1.0, float(body.get("level", 0))))
        cast = self.studio.cast
        rooms = getattr(self.studio, "rooms", None)
        if isinstance(body.get("levels"), dict):                 # a room's volume: each of its speakers at once
            levels = {str(n): max(0.0, min(1.0, float(v))) for n, v in body["levels"].items()}

            def set_all():
                muted = rooms.muted() if rooms else {}
                for n, v in levels.items():
                    if n in muted:
                        rooms.set_saved_volume(n, v)
                    else:
                        cast.set_device_volume(n, v)
                return {"levels": levels}
            try:
                return web.json_response(await asyncio.get_running_loop().run_in_executor(None, set_all))
            except Exception as e:  # noqa: BLE001
                return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)
        if body.get("name") and rooms and str(body["name"]) in rooms.muted():
            fn = lambda: (rooms.set_saved_volume(str(body["name"]), level), {"name": body["name"], "volume": level, "muted": True})[1]
        elif body.get("name"):
            fn = lambda: cast.set_device_volume(str(body["name"]), level)
        else:
            if cast.cast is None:
                return web.json_response({"error": "not casting"}, status=400)
            fn = lambda: cast.set_volume(level)
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, fn))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)

    async def party_level_set(self, request):
        if not self._lan(request):
            raise web.HTTPForbidden()
        self.party_level = max(0.0, min(1.0, float((await request.json()).get("level", 1.0))))
        cfg = self.load_config()
        cfg["party_level"] = round(self.party_level, 3)
        self.save_config(cfg)
        return web.json_response({"party_level": self.party_level})

    async def colors(self, request):
        """Set the jam's colors ({mode, primary, secondary}), or one track's ({track_id, primary,
        secondary}; without the colors, the track goes back to the jam's)."""
        if not self._local(request):
            raise web.HTTPForbidden()
        body = await request.json()
        hexcolor = lambda v: v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else None
        if body.get("track_id"):
            self.library.set_colors(str(body["track_id"]), hexcolor(body.get("primary")), hexcolor(body.get("secondary")),
                                    off=body.get("theme") is False, only=bool(body.get("only")))
        else:
            cfg = self.load_config()
            cur = {**self.DEFAULT_COLORS, **cfg.get("jam_colors", {})}
            if "on" in body:
                cur["on"] = bool(body["on"])
            if body.get("mode") in ("pick", "mix", "album"):
                cur["mode"] = body["mode"]
            for k in ("primary", "secondary", "accent1", "accent2"):
                if hexcolor(body.get(k)):
                    cur[k] = hexcolor(body[k])
            if "extras" in body:
                cur["extras"] = bool(body["extras"])
            if "rainbow" in body:
                cur["rainbow"] = bool(body["rainbow"])
            if isinstance(body.get("pairs"), list):
                pairs = [[hexcolor(p[0]), hexcolor(p[1])] for p in body["pairs"][:8] if isinstance(p, list) and len(p) == 2]
                if pairs and all(a and b for a, b in pairs):
                    cur["pairs"] = pairs
            self._assigned.clear()                              # pairs or mode changed: hand them out afresh
            cfg["jam_colors"] = cur
            self.save_config(cfg)
        # off the event loop: handing out pairs afresh reads what was learned about each queued track
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._state))

    def _set_theme_on(self, on: bool) -> None:
        """The party's colors for every song, or (off) every song in its own album cover's colors."""
        cfg = self.load_config()
        cfg["jam_colors"] = {**self.DEFAULT_COLORS, **cfg.get("jam_colors", {}), "on": bool(on)}
        self.save_config(cfg)

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
        if "opener" in body:                                  # the saved show every jam opens with ("" for none)
            cfg["jam_opener"] = str(body["opener"] or "")
        if "fetch" in body:                                   # learn ahead from YouTube copies
            cfg["jam_fetch"] = bool(body["fetch"])
            self.prelisten.fetch = self.fetch if body["fetch"] else None
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
