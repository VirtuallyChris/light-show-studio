"""The party's place in the studio server: starting and stopping it, and its HTTP routes."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import re
import sys
import threading
import time

from aiohttp import web

from . import loopback
from . import webjson
from .party import CastOutput, PartyEngine, PartyLibrary, PartyRunner, PartySettings, VirtualOutput
from .loopback import Gate
from .relay import Relay
from .spotify import PlayerAwareSpotify, PlayerState, SpotifyClient


# the start settings remembered for next time (and for a party that starts itself)
LAST_KEYS = ("device", "speakers", "pc_device", "target_delay", "crossfade", "skip_jumps", "source")
DEFAULT_OPENER = ""                                   # a party opens with this saved show (config.json party_opener), if it is there


def _hex_color(v) -> str | None:
    return v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else None


def _summary(library, tid: str) -> dict:
    """A learned song's problems, mood and genre (PartyLibrary.summary: without parsing the whole file)."""
    if hasattr(library, "summary"):
        return library.summary(tid) or {}
    return (library.learned(tid) or {}).get("analysis") or {}


class PartyService:
    def __init__(self, studio, shows_dir: str, load_config, save_config, local_ip):
        self.studio, self.shows_dir = studio, shows_dir
        self.load_config, self.save_config, self.local_ip = load_config, save_config, local_ip
        self.library = PartyLibrary(shows_dir)
        # learn ahead: queued songs learned before they play, from a copy found on YouTube (fetch_ahead)
        from . import labeler, lyrics
        from .fetch_ahead import FetchAhead
        self.fetch = FetchAhead(self.library, keep=lambda: bool(self.load_config().get("party_keep_youtube_copies", True)),
                                lyrics=lyrics.learn, features=labeler.learn_features)
        cfg = self.load_config()
        if cfg and "party_keep_youtube_copies" not in cfg:     # written in, so the setting is there to see and change
            cfg["party_keep_youtube_copies"] = True
            self.save_config(cfg)
        self._assigned: dict[str, int] = {}                  # track id -> the pair it got in "mix"
        self._moods: dict[str, dict | None] = {}             # track id -> its learned mood (or None)
        # Energy, 0..1: how much of the show comes through. Below 1 the studio lays the song's main
        # color over every show, more and more opaque, so the room calms down without going dark.
        self.party_energy = float(load_config().get("party_energy", 1.0))
        # Built up to 100% over a while (the phone remote's button under the slider): {from, start (epoch s), minutes, song}.
        # Kept in the config, so a restart carries on with it.
        self.energy_ramp: dict | None = load_config().get("energy_ramp") or None
        # the color moments switched on (phone remote buttons the user makes): {mode id: {on, at (monotonic s), color, paused}};
        # not kept over a restart
        self.color_moment_on: dict[str, dict] = {}
        self.store = dict(load_config().get("spotify", {}))
        self.spotify = SpotifyClient(self.store, save=self._save_store)
        self.player = PlayerState()                     # the studio's own Spotify player, as its page reports it
        # Learning a playlist: the studio's own player hears a party playlist ahead of time, through a silent party
        from .playlist_learning import PreListen
        self.playlist_learning = PreListen(self.spotify, self.library,
                                   start_party=lambda: self._start({"source": "studio", "speakers": "none", "target_delay": 20, "playlist_learning": True}),
                                   stop_party=self._stop, device_id=lambda: self.player.device_id,
                                   fetch=self.fetch if self._fetch_on() else None)
        self.player_enabled = True                      # False: a test copy that must not register a Spotify speaker
        from .player_browser import BROWSER_NAME, HIDDEN_OUTPUT, HiddenPlayer, own_hidden_output
        self.player_browser = BROWSER_NAME
        self.hidden = HiddenPlayer(f"http://127.0.0.1:{studio.port}/static/player.html",
                                   HIDDEN_OUTPUT if own_hidden_output() else load_config().get("party_hidden_output", HIDDEN_OUTPUT),
                                   avoid=lambda: load_config().get("party_room_output") or "")
        self.engine: PartyEngine | None = None
        self.runner: PartyRunner | None = None
        self.capture = None
        self.demo = None
        self.mode = ""
        self.source = ""                                # where Spotify plays for the running party: studio / app
        self.auto_armed = True                          # auto-start fires on playback beginning, not on it carrying on
        self.auto_started = False                       # the running party was started by playback, not by a button
        self.auto_error = ""
        self.opener: dict | None = None                 # the opening song, while a party opens with one
        self.room_wanted = False                        # the running party is to be heard in the room (not a preview)
        self._switching = False                         # the room's sound is moving to another output
        self._cast_failed = None                        # the Cast speaker that would not play, until Cast is pressed again
        self._cast_ok_at = 0.0                          # when the Cast speaker was last seen playing, or not due to
        self._hushed = False                            # the window closing stopped the music (a reload brings it back)
        self.sync_warning = ""
        self._syncing = False
        self._auto_sync_off = False
        self._sync_retry_at = 0.0
        self._synced_start = None
        self._served: dict[str, float] = {}             # stream sid -> seconds of audio sent
        self._found_genre: dict[str, dict] = {}         # track id -> what its analysis found (_found)
        self._art: dict = {}                            # album art relayed, by its URL

    def _save_store(self) -> None:
        cfg = self.load_config()
        cfg["spotify"] = dict(self.store)
        self.save_config(cfg)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.studio.port}/api/spotify/callback"

    def routes(self) -> list:
        return [
            web.get("/api/relay/state", self.state),
            web.get("/api/relay/outputs", self.outputs),
            web.post("/api/relay/start", self.start),
            web.post("/api/relay/stop", self.stop),
            web.post("/api/relay/handover", self.handover),
            web.post("/api/relay/synccheck", self.sync_check),
            web.post("/api/relay/demo/{action}", self.demo_action),
            web.get("/api/relay/analysis", self.analysis),
            web.get("/api/relay/stream.wav", self.stream),
            web.post("/api/spotify/client", self.set_client),
            web.get("/api/spotify/login", self.login),
            web.get("/api/spotify/callback", self.callback),
            web.post("/api/spotify/signout", self.signout),
            web.get("/api/spotify/token", self.token),
            web.post("/api/relay/sdk_state", self.sdk_state),
            web.post("/api/relay/prefs", self.prefs),
            web.post("/api/relay/colors", self.colors),
            # the phone remote: a phone app on the home network (the party)
            web.get("/party", self.party_page),
            web.get("/party/", self.party_slash),
            web.get("/party/manifest.webmanifest", self.party_manifest),
            web.get("/api/party/state", self.party_state),
            web.post("/api/party/colors", self.party_colors),
            web.post("/api/party/theme", self.party_theme),
            web.post("/api/party/style", self.party_style),
            web.post("/api/relay/style", self.track_style),
            web.post("/api/party/genre", self.party_genre),
            web.post("/api/relay/genre", self.track_genre),
            web.post("/api/party/playback", self.party_playback),
            web.post("/api/party/energy", self.party_energy_set),
            web.post("/api/party/energy_ramp", self.energy_ramp_set),
            web.post("/api/party/color_moment", self.color_moment_set),
            web.post("/api/party/color_moments", self.color_moments_set),
            web.get("/api/party/volumes", self.party_volumes),
            web.post("/api/party/volume", self.party_volume),
            web.get("/api/party/bulbstudio", self.party_bulbstudio),
            web.post("/api/party/bulbstudio", self.party_bulbstudio_set),
            web.post("/api/bulbstudio/open", self.bulbstudio_open),
            web.post("/api/party/even", self.party_even),
            web.post("/api/playlist_learning", self.playlist_learning_start),
            web.post("/api/playlist_learning/stop", self.playlist_learning_stop),
            web.get("/api/relay/art", self.art),
            web.post("/api/relay/player/open", self.player_open),
            web.post("/api/relay/player/transfer", self.player_transfer),
        ]

    # -- lifecycle -----------------------------------------------------------------------
    def _speaker_offset(self) -> float:
        name = self.studio.cast.name
        cfg = self.load_config()
        return float(cfg.get("cast_offsets", {}).get(name, cfg.get("cast_offset_ms", 0))) / 1000.0

    def _file_status(self):
        """How much of a song from disk the speakers still have to play (for the opening-song hand-over)."""
        o = self.opener
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
    FETCH_DELAY_S = 5.0

    def warm_ahead(self) -> None:
        """Start the learn-ahead process and load its models now, with the studio, not when a party starts
        (42 s later, last time, and the first song of the party waited for it). The real studio only."""
        from . import labeler
        threading.Thread(target=labeler.warm, daemon=True, name="labeler-warm").start()   # (~1 s: not on the first song)
        if self._fetch_on():
            from .fetch_ahead import ANALYSER
            from .model_worker import HOST
            threading.Thread(target=HOST.warm_all, daemon=True, name="models-warm").start()     # the models, at once
            threading.Thread(target=ANALYSER.warm, daemon=True, name="analyser-warm").start()

    def _fetch_on(self) -> bool:
        """Learn ahead is on (the default where spotDL is installed): songs are learned before they play,
        so the room needs to be only seconds behind, not a song."""
        from . import fetch_ahead
        return bool(self.load_config().get("party_fetch", True)) and fetch_ahead.available()

    def _start(self, body: dict) -> dict:
        self._stop()
        demo = bool(body.get("demo"))
        ahead = not demo and not body.get("playlist_learning") and self._fetch_on()
        if ahead and "target_delay" not in body:
            body = {**body, "target_delay": float(self.load_config().get("party_fetch_delay", self.FETCH_DELAY_S))}
        relay = Relay(sr=44100, channels=2, capacity_s=600.0)
        gate = Gate()
        cfg = PartySettings(target_delay=float(body.get("target_delay", 90.0)), max_gap=float(body.get("max_gap", 6.0)),
                          crossfade=max(0.0, min(12.0, float(body.get("crossfade", 0.0)))),
                          skip_jumps=bool(body.get("skip_jumps", False)), light_live=ahead,
                          even_levels=bool(self.load_config().get("party_even_levels", True)),
                          level_target=float(self.load_config().get("party_energy_target", -9.0)))
        hidden_name = self._hidden_output_name()
        if not demo and hidden_name is None:
            from .player_browser import hidden_problem
            raise ValueError(hidden_problem() or self.hidden.no_output_note())
        where = body.get("speakers", "cast")
        # Speakers asked for (Cast or this PC) means the room always hears it: on the Cast speaker
        # when one is connected, otherwise on this PC. Only "none" is a silent preview. (A party that
        # started by itself with "cast" saved and no speaker connected used to fall silent.)
        self.room_wanted = where in ("cast", "pc") and not demo
        if where in ("cast", "pc") and not demo:
            where = "cast" if self.studio.cast.cast is not None else "pc"
        elif where == "cast" and self.studio.cast.cast is None:
            where = "none"
        output = self._output_for(where, hidden_name, check=not demo)
        # A party opens with one of your saved shows (config.json party_opener, unless another is chosen): it plays
        # in the room with its lights while Spotify plays, unheard, behind it, and the room joins the party as
        # it ends, a whole song behind. So the party is always a song ahead: each song is heard to its end
        # before the room gets to it, and gets its full show from the first beat.
        # (learning ahead, the room is only seconds behind: no opening song is needed to fill a buffer)
        opener = None if demo or ahead or body.get("playlist_learning") or where not in ("cast", "pc") else self._opener_plan(body)
        if opener:
            cfg.target_delay = max(20.0, opener[1] - 2.0)
        if demo:
            from .party_demo import DemoSpotify
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
        library = PartyLibrary(os.path.join(self.shows_dir, "_party", "demo")) if demo else self.library
        from . import structure_model
        from .genre import WORKER
        genre_fn = WORKER.classify_audio if WORKER.available() else None
        segment_fn = structure_model.run_audio if structure_model.available() else None
        self.engine = PartyEngine(relay, spotify, library, output, gate, cfg, genre_fn=genre_fn, segment_fn=segment_fn)
        if self._music_gain() < 1.0:
            relay.set_duck(0.0, 0.0, self._music_gain())         # (started while a mode has the music down: down from the start)
        self.runner = PartyRunner(self.engine, file_status=self._file_status)
        if ahead:
            self.engine.on_queue = lambda tracks, now=None: self.fetch.submit(tracks, first=True, now=now)
            self.engine.on_context = lambda tracks: self.fetch.submit(tracks, first=False)     # behind the queue
            self.engine.on_mismatch = self.fetch.mismatched
        self.mode = ("demo" if demo else "spotify") + (" to " + output.name if where in ("cast", "pc") else ", preview only (no speakers)")
        self.runner.start()
        threading.Thread(target=self._keep_levels, args=(self.engine,), daemon=True, name="keep-levels").start()
        threading.Thread(target=self._keep_on_studio, args=(self.engine,), daemon=True, name="keep-on-studio").start()
        if demo:
            self.demo.start()
        self.opener = None
        if opener:
            self._begin_opener(*opener, via=where)
        self.engine.say(f"party started: {self.mode}, aiming {cfg.target_delay:.0f}s behind")
        self.source = "demo" if demo else body.get("source", "app")
        self.auto_started = bool(body.get("auto"))
        if body.get("playlist_learning"):
            self.mode = "Learning a playlist: learning songs, nothing plays in the room"
        if not demo and not body.get("playlist_learning"):             # a learning a playlist never becomes the party you start next
            cfg_all = self.load_config()
            last = {k: body[k] for k in LAST_KEYS if k in body}
            if ahead:                                        # the delay learning ahead is kept on its own
                cfg_all["party_fetch_delay"] = float(last.pop("target_delay", self.FETCH_DELAY_S))
                last["target_delay"] = cfg_all.get("party_last", {}).get("target_delay", 90.0)
            cfg_all["party_last"] = last
            self.save_config(cfg_all)
        return self._state()

    # -- the opening song --------------------------------------------------------------------------------
    def _opener_choice(self) -> str:
        """The saved show a party opens with ("" for none: the party starts 90 s behind instead)."""
        cfg = self.load_config()
        name = cfg.get("party_opener", DEFAULT_OPENER)
        return name if name and self._length(name) >= 60 else ""

    def _length(self, name: str) -> float:
        length_of = getattr(self.studio, "_show_length", None)
        return float(length_of(name)) if length_of else 0.0

    def _opener_plan(self, body: dict) -> tuple[str, float] | None:
        name = self._opener_choice()
        length = self._length(name) if name else 0.0
        return (name, length) if name and length >= 60 else None

    def _begin_opener(self, name: str, length: float, via: str) -> None:
        """Play the opening show in the room (its song on the party's speakers, or this page on this PC, and
        its lights) while Spotify fills the buffer behind it."""
        import uuid
        self.engine.open_with_song()                          # the room joins the party as the opener ends
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
        except Exception:  # noqa: BLE001 - nothing to resume yet: the party fills as soon as music plays
            pass
        self.engine.say(f"opening with {name}: the room joins the party as it ends, a song behind")

    def opener_status(self) -> dict:
        """The opening song as the page plays it (shaped like a moment): over once the party has the speakers."""
        o = self.opener
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
            return CastOutput(self.studio.cast, lambda sid: f"http://{ip}:{self.studio.port}/api/relay/stream.wav?sid={sid}",
                              self._speaker_offset)
        if where == "pc":
            from .audio_io import ProcessOutput
            from .local_output import DEFAULT_OFFSET_S
            pc_device = self.load_config().get("party_room_output") or None          # the default output, unless set
            if check and self._same_device(hidden_name if hidden_name is not None else self._hidden_output_name(), pc_device):
                raise ValueError(f"The default output is {hidden_name or self.hidden.hidden_output}, the output Spotify plays "
                                 "into unheard. Set your speakers or headphones as the default output in your sound settings.")
            offset = float(self.load_config().get("pc_output_offset_ms", DEFAULT_OFFSET_S * 1000)) / 1000.0
            return ProcessOutput(pc_device, offset)         # plays from its own process: the analysis cannot starve it
        return VirtualOutput(time.monotonic)

    def _output_kind(self, out) -> str:
        return "cast" if isinstance(out, CastOutput) else "pc" if hasattr(out, "device_name") else "none"

    def _follow_cast(self) -> None:
        """Casting moves the party: a Cast speaker connected mid-party takes the room's sound, and when
        it goes away the sound comes back to this PC. Checked every time the page asks for the state."""
        e = self.engine
        if e is None or self.source == "demo" or self._switching:
            return
        kind = self._output_kind(e.output)
        if kind == "none" and not self.room_wanted:
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
        usable = cast.cast is not None and cast.cast is not self._cast_failed
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
        since = max(self._cast_ok_at, getattr(e.output, "started_at", 0.0))
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
                    e._streamed_at = e.clock()               # (keep_delay leaves a fresh stream to settle)
                    if e.gate.paused:
                        new.pause()
                self.mode = "spotify to " + new.name
                e.say(f"the room is now on {new.name}")
        except Exception as ex:  # noqa: BLE001 - shown in the log; the party carries on where it was
            if self.engine is not None:
                self.engine.say(f"could not move the sound to {want}: {ex}")
        finally:
            self._switching = False

    def close_player(self) -> None:
        """The studio is closing: Edge's sound goes back to the default output, and the player closes."""
        self.hidden.stop()

    def _hidden_output_name(self) -> str | None:
        """What the party records: an output's name, or on a Mac the tap's. None if there is nothing to record."""
        if sys.platform == "darwin":
            from . import mac_capture
            return mac_capture.DEVICE if mac_capture.available() else None
        try:
            if sys.platform != "win32":
                from . import linux_audio
                linux_audio.ensure_sink()              # (it may not be there yet, or the player has gone)
            import soundcard as sc
            wanted = self.hidden.output_name()
            return next((s.name for s in sc.all_speakers() if wanted and wanted.lower() in s.name.lower()), None)
        except Exception:  # noqa: BLE001
            return None

    def _want_player(self) -> bool:
        return (self.player_enabled and self.spotify.signed_in and self.spotify.can_stream
                and self._last().get("source", "studio") == "studio")

    def hush(self) -> bool:
        """The studio's window is closing: the music stops at once (the room's speakers hold where they are and
        Spotify pauses), before anything else shuts down. A reload brings it back (unhush). Whether it did."""
        eng = self.engine
        if eng is None or eng.show_moment_hold:
            return False
        eng.hold("the studio is closing: the music stops")
        self._hushed = True
        return True

    def unhush(self) -> None:
        """It was a reload, not a close: the room carries on where it stopped."""
        if not self._hushed:
            return
        self._hushed = False
        eng = self.engine
        if eng is not None and eng.show_moment_hold:
            eng.release(False)

    STUDIO_DEVICE = "Light Show Studio"

    def keep_on_studio_once(self, eng, last: float, now: float) -> float:
        """One look: a party playing from the studio whose music Spotify plays somewhere else (resumed on the phone
        itself after a pause) is handed back to the studio's player, at most every 15 s. The party would take it
        for playing while the studio recorded silence. Returns when it last handed back."""
        if self.source != "studio" or not self.player.device_id or now - last < 15.0:
            return last
        np_ = getattr(eng, "now", None)
        dev = getattr(np_, "device", "") if np_ is not None else ""
        if not (np_ is not None and np_.playing and dev and dev != self.STUDIO_DEVICE):
            return last
        try:
            if self.spotify.transfer_playback(self.player.device_id, play=True):
                eng.say(f"Spotify started playing on {dev}: brought back to {self.STUDIO_DEVICE}")
        except Exception:  # noqa: BLE001 - looked at again in a moment
            pass
        return now

    def _keep_on_studio(self, eng) -> None:
        last = -1e9
        while self.engine is eng:
            time.sleep(2.0)
            if self.engine is eng:
                last = self.keep_on_studio_once(eng, last, time.monotonic())

    def _keep_levels(self, eng) -> None:
        """While this party plays, every 5 s: the party's speakers (the rooms that are on, and the one cast to) at
        the levels kept for them (Rooms.keep_levels): one changed in person keeps its new level; one that missed
        a change the studio just sent gets it again."""
        rooms, cast = getattr(self.studio, "rooms", None), getattr(self.studio, "cast", None)
        if rooms is None:
            return
        while self.engine is eng:
            time.sleep(5.0)
            if self.engine is not eng:
                return
            if not eng.streaming or eng.show_moment_hold or eng.gate.paused:
                continue
            names = [s for r in rooms.list() if r["on"] for s in r["speakers"]]
            if cast is not None and getattr(cast, "cast", None) is not None and cast.name and cast.name not in names:
                names.append(cast.name)
            try:
                for line in rooms.keep_levels(names):
                    eng.say(f"speaker volume: {line}")
            except Exception:  # noqa: BLE001 - looked at again in 5 s
                pass

    def _stop(self) -> None:
        self.fetch.forget()                                  # what is under way finishes; nothing new starts
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
        """Would the party play on the very output it is recording? (None means the default output.)"""
        if capture == playback:
            return True
        try:
            import soundcard as sc
            default = sc.default_speaker().name
        except Exception:  # noqa: BLE001
            return False
        return (capture or default) == (playback or default)

    def _state(self) -> dict:
        if self._want_player():
            self.hidden.start()                             # cheap when it is already running
        self._follow_cast()
        base = {"running": self.engine is not None, "mode": self.mode, "redirect_uri": self.redirect_uri,
                "player": {**self.player.to_json(), "browser": self.player_browser, **self.hidden.to_json()},
                "spotify": {"signed_in": self.spotify.signed_in, "client_id": self.spotify.client_id, "error": self.spotify.last_error,
                            "can_stream": self.spotify.can_stream},
                "speaker_connected": self.studio.cast.cast is not None, "speaker": self.studio.cast.name,
                "last": self._last(), "cast_device": self.load_config().get("party_cast_device", ""), "auto": self._auto_on(), "auto_error": self.auto_error, "auto_started": self.auto_started,
                "color_settings": self._color_settings(), "track_colors": self.library.colors(), "track_styles": self.library.styles(),
                "track_genres": self.library.genres(), "genre_choices": self._genre_choices(),
                "party_energy": self.party_energy, "color_moments": self.color_moments_status(),
                "show_moment": self._room_show_moment(),
                "playlist_learning": self.playlist_learning.status(), "opener_choice": self._opener_choice(),
                "fetch": {**self.fetch.status(), "on": self._fetch_on(),
                          "delay": float(self.load_config().get("party_fetch_delay", self.FETCH_DELAY_S))}}
        if self.engine is None:
            return base
        st = self.engine.state()
        st["spotify"] = {**base["spotify"], **{k: v for k, v in st["spotify"].items() if k in ("device", "error") and v}}
        if self.capture is not None:
            st["capture"] = {"device": self.capture.device_name or "default output", "level": round(self.capture.level, 3), "error": self.capture.error}
        out_error = getattr(self.engine.output, "error", "")
        if out_error:
            st["output_error"] = out_error
        st["output_kind"] = self._output_kind(self.engine.output)
        if self.sync_warning and st["output_kind"] == "cast":
            st["sync_warning"] = self.sync_warning
        if self.capture is not None and getattr(self.capture, "lost", 0):
            st["capture"]["lost_s"] = round(self.capture.lost, 2)
        if self._ramp_step((st.get("room") or {}).get("key")):
            base["party_energy"] = self.party_energy
        played = [st.get("room"), st.get("ahead"), *(st.get("queue") or [])]
        for t in played:                                     # learning ahead: where each first listen stands
            if t and t.get("kind") == "live" and t.get("id") and base["fetch"]["on"]:
                got = self.fetch.state_of(t["id"])
                if got:
                    t["learning"], t["learning_why"] = got
            elif t and t.get("kind") == "learned" and self._found(t).get("problems"):
                t["learning"], t["learning_why"] = "failed", "; ".join(self._found(t)["problems"])     # analysed, but not all of it
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
        if body.get("saved"):                                 # with the settings the party was last set up with
            body = {**self._last(), **body}
            if self._fetch_on():                              # (learning ahead keeps its own delay)
                body.pop("target_delay", None)
        if self.playlist_learning.busy() and self.playlist_learning.fetch is None:   # a real party always wins (one learning from copies needs no Spotify)
            await asyncio.get_running_loop().run_in_executor(None, self.playlist_learning.stop, "a party was started")
        return await self._run(self._start, body)

    # -- learning a playlist ------------------------------------------------------------------------------
    async def playlist_learning_start(self, request):
        """Learn a playlist's songs (or the queue's) ahead of the party: {source: a playlist link, or "queue"}."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        if self.engine is not None and not self.playlist_learning.busy() and self.playlist_learning.fetch is None:
            return web.json_response({"error": "stop the party first: Spotify can only play one thing at a time"}, status=400)
        source = str((await request.json()).get("source") or "").strip()
        return await self._run(self.playlist_learning.start, source)

    async def playlist_learning_stop(self, request):
        if not self._lan(request):
            raise web.HTTPForbidden()
        return await self._run(self.playlist_learning.stop)

    async def stop(self, request):
        return await self._run(lambda: (self._stop(), self._state())[1])

    async def handover(self, request):
        if self.engine is None:
            return web.json_response({"error": "start the party first"}, status=400)
        return await self._run(lambda: (self.engine.arm_handover(), self._state())[1])

    async def sync_check(self, request):
        if self.engine is None or not self.engine.streaming:
            return web.json_response({"error": "the speakers are not playing the party yet"}, status=400)
        if self._syncing:
            return web.json_response({"error": "a sync check is already listening; its result will be in the Party log"}, status=409)
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
        r = relay_sync_check(self.engine, mic_device=self.load_config().get("mic_device") or None, log=lambda *a: None)
        # a muted microphone hears pure silence: said on screen and on the phone until a check hears the room
        self.sync_warning = "The sync check heard nothing from the microphone. Is it muted?" if r.get("mic_silent") else ""
        name = self.studio.cast.name
        if name and r["confidence"] >= 0.5 and abs(r["offset_ms"]) <= 1500:   # the correction is relative: add it
            cfg = self.load_config()
            new = int(round(self._speaker_offset() * 1000 + r["offset_ms"]))
            cfg.setdefault("cast_offsets", {})[name] = new
            self.save_config(cfg)
            r["saved_for"], r["speaker_offset_ms"] = name, new
        return r

    AUTO_SYNC_AFTER_S = 8.0
    SILENT_RETRY_S = 30.0        # a muted microphone: listen again this often until it hears the room

    def _maybe_auto_sync(self, e) -> None:
        """Once a Cast speaker has been playing the party for a few seconds (a new cast, a reconnect, a
        restart), listen to the room and correct its delay. A speaker's delay can change when it or
        its group reconnects, and nothing else would notice. It only listens; nothing is played."""
        out = e.output
        if not isinstance(out, CastOutput) or self._auto_sync_off or self._syncing:
            return
        started = getattr(out, "started_at", 0.0)
        if getattr(e, "show_moment_hold", False) or (started and started == getattr(e, "no_sync_start", None)):
            return                                       # a doorbell, the music back after it, a catch-up: no new delay
        retry = self._sync_retry_at
        if not started or e.gate.paused or (started == self._synced_start and not (retry and time.monotonic() >= retry)):
            return
        status = getattr(self.studio.cast, "status", None)
        if time.monotonic() - started < self.AUTO_SYNC_AFTER_S or not callable(status) or status().get("state") != "PLAYING":
            return
        self._synced_start, self._syncing, self._sync_retry_at = started, True, 0.0

        def run():
            name = self.studio.cast.name or "the speaker"
            try:
                was = self.sync_warning
                r = self._run_sync()
                if r.get("mic_silent"):
                    self._sync_retry_at = time.monotonic() + self.SILENT_RETRY_S
                    if not was:
                        e.say("sync check: the microphone heard nothing (muted?); listening again every 30 s")
                elif r.get("saved_for"):
                    e.say(f"sync check: {name} is {r['speaker_offset_ms']} ms behind (corrected by {r['offset_ms']:+d} ms)")
                else:
                    e.say(f"sync check: could not hear {name} clearly enough; keeping its delay")
            except Exception as ex:  # noqa: BLE001 - no microphone, or it is busy: say so once and stop trying
                self._auto_sync_off = True
                self.sync_warning = f"The sync check could not use the microphone ({type(ex).__name__})."
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
        return await webjson.response(doc) if doc else web.json_response({"error": "no such track"}, status=404)

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
        served = self._served
        # Judged by what the speaker has played, not by what was sent: up to 20 s go into the
        # network at once, so a speaker that only probed the stream may already have "had" a lot.
        try:
            played = float(out.position()) if isinstance(out, CastOutput) and sid in served else 0.0
        except Exception:  # noqa: BLE001
            played = 0.0
        if isinstance(out, CastOutput) and played >= self.RECONNECT_AFTER_S:
            if eng.gate.paused and not eng.show_moment_hold:
                # The party is paused: a paused speaker's connection idles out and it asks again, every ten
                # minutes or so; restarting it there was a cast (the speaker's sound) each time, all night (the
                # user). Left be: it gets a fresh stream from the paused spot when the music plays again.
                if not eng._speaker_dropped:
                    eng._speaker_dropped = True
                    eng.say("the speaker let go of the stream during the pause: it restarts when the music does")
                raise web.HTTPGone()
            eng.say("the speaker lost its stream (Wi-Fi) and came back: restarting it where the room was")
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
            if e.gate.paused and not e.show_moment_hold:
                e._speaker_dropped = True                    # (paused meanwhile: it restarts with the music)
                return
            rc = e.room_capture_time()
            start = rc if rc is not None else max(e.relay.oldest_time, e.relay.capture_time - e.cfg.target_delay)
            out.start_stream(e.relay.open_stream(start))
            e._streamed_at = e.clock()                       # (keep_delay leaves a fresh stream to settle)
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
            return web.Response(text="Enter your Spotify app's Client ID in Settings first.", status=400)
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

        - Something starts playing on Light Show Studio with no party running: start one,
          with the settings last used. It fires on playback beginning, so after Stop the
          music can carry on without a party until it is paused or changed.
        - The player came back under a new device id (the studio window was reloaded)
          while it was playing a party: Spotify stopped when the old one vanished, so
          playback is handed back. The party's buffer covers the gap.
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
        except Exception as e:  # noqa: BLE001 - shown in Settings
            self.auto_error = f"{type(e).__name__}: {e}"

    def _opener_room(self) -> dict | None:
        """The opening song as the phone remote shows it in the room (one of your saved shows, from disk)."""
        o = self.opener_status()
        if o.get("phase") != "show":
            return None
        return {"id": None, "label": o["show"], "name": o["show"], "artists": [], "image": None, "kind": "custom", "opener": True,
                "duration": o["dur"], "position": round(o["progress"] * o["dur"], 1), "playing": True, "colors": None, "own": False,
                "theme_off": False}

    def _room_show_moment(self) -> dict:
        """What the room is playing instead of the party: a moment, else the opening song."""
        show_moment = self.studio.show_moments.status() if getattr(self.studio, "show_moments", None) else {}
        if show_moment.get("phase") and show_moment.get("phase") != "done":
            if self.opener:                                   # a moment cut the opening song short: the party follows the moment
                self.opener["cut"] = True
            return show_moment
        return self.opener_status() or show_moment

    def _last(self) -> dict:
        return {"source": "studio", "speakers": "pc", "target_delay": 90.0, **self.load_config().get("party_last", {})}

    # extras: two more colors a few of the small accents take now and then (orange and red at Halloween)
    DEFAULT_COLORS = {"on": True, "mode": "album", "primary": "#8000ff", "secondary": "#00ff00", "extras": False, "accent1": "#ff6000", "accent2": "#ff0000",
                      # "mix": each song gets one of these pairs, by its vibe, never the same pair twice in a row
                      "pairs": [["#8000ff", "#00ff00"], ["#8000ff", "#ff6000"], ["#00ff00", "#ff6000"], ["#ff0000", "#0040ff"]],
                      "rainbow": True,                     # in "mix", a song that goes all out gets rainbow drops
                      "wild": False}                       # No Limits Colors: each section its own stage colors (app.js stageScheme)

    def _found(self, t: dict) -> dict:
        """What a learned song's analysis found of its genre (read once: a learned file is megabytes)."""
        tid = t.get("id")
        if not tid or t.get("kind") != "learned":
            return {}
        found = self._found_genre
        if tid not in found:
            a = _summary(self.library, tid)                               # (problems, mood, genre: not the whole file)
            from .genre import restyle
            g = restyle(a.get("genre"), float((a.get("mood") or {}).get("drive", 0.6))) or {}
            found[tid] = {**g, "problems": list(a.get("problems") or [])}
        return found[tid]

    def _found_style(self, t: dict) -> str | None:
        return self._found(t).get("style")

    def _found_genres(self, t: dict) -> list:
        g = self._found(t)
        return (g.get("genres") or g.get("spotify") or [])[:2]

    def _genre_choices(self) -> list:
        return [{"key": k, "label": lbl, "words": words, "level": lv} for k, (lbl, words, lv) in self.library.GENRES.items()]

    def _vibe_order(self, track_id: str, n: int) -> list[int]:
        """Which pairs suit a track, best first, from what was learned about it the last time it played
        (unknown tracks take turns). The order follows the default pairs: purple/green spooky, purple/orange
        warm, green/orange bright, red/blue hard."""
        if track_id not in self._moods:                     # a learned file is megabytes: read each once
            self._moods[track_id] = _summary(self.library, track_id).get("mood")
        mood = self._moods[track_id]
        if not mood:
            k = len(self._assigned)
            return [(k + i) % n for i in range(n)]
        hard, warm = mood.get("drive", 0) > 0.62, mood.get("mode") == "major"
        pref = ([2, 1, 3, 0] if warm else [3, 0, 1, 2]) if hard else ([1, 2, 0, 3] if warm else [0, 1, 3, 2])
        return [i for i in pref if i < n] + [i for i in range(n) if i not in pref]

    def _assign_colors(self, tracks: list) -> dict:
        """In "mix": the pair each of these tracks (in play order) gets. Once given, a track keeps its pair."""
        jc = self._color_settings()
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

    def _color_settings(self) -> dict:
        """The party's own colors: every song in a primary and a secondary, or ("album") its cover's."""
        return {**self.DEFAULT_COLORS, **self.load_config().get("color_settings", {})}

    # -- the phone remote ---------------------------------------------------------------------
    @staticmethod
    def _lan(request) -> bool:
        """The phone remote answers phones on the home network (and this computer), never the internet."""
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

    async def party_slash(self, request):
        """/party/ typed by hand (or a bookmark with the slash): the phone remote, not a 404."""
        raise web.HTTPFound("/party")

    async def party_manifest(self, request):
        return web.json_response({
            "name": "phone remote", "short_name": "phone remote", "start_url": "/party", "scope": "/party",
            "display": "standalone", "background_color": "#0e0f14", "theme_color": "#0e0f14",
            "icons": [{"src": "/static/party/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/party/icon-512.png", "sizes": "512x512", "type": "image/png"}]},
            content_type="application/manifest+json")

    def _doorbell_show_moment(self) -> str:
        """The moment the real doorbell rings, while it is set up (the phone remote leaves its button out: the bell rings it)."""
        cfg = getattr(self, "load_config", dict)() or {}
        if not (cfg.get("addons") or {}).get("doorbell"):
            return ""
        bell = cfg.get("doorbell") or {}
        return str(bell.get("show_moment") or "") if bell.get("on") and bell.get("host") else ""

    def _party(self) -> dict:
        """What the phone remote shows: the song in the room, what comes next, and each one's colors."""
        st = self._state()
        jc, own_all, assigned = st.get("color_settings", {}), st.get("track_colors", {}), st.get("assigned_colors", {})
        styles, genres = st.get("track_styles", {}), st.get("track_genres", {})

        def colors(t):
            if t.get("kind") == "custom":
                return None, False                       # your own show: its colors are its own
            own = own_all.get(t.get("id"))
            if not jc.get("on", True) or (own and own.get("off")):
                return None, False                       # out of the party's colors: its cover's
            if own:
                return [own["primary"], own["secondary"]], True
            if jc.get("wild"):
                return None, False                       # No Limits: the studio picks section by section
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
                    "only": bool((own_all.get(t.get("id")) or {}).get("only")),
                    "style": t.get("style") or self._found_style(t), "vibe": styles.get(t.get("id")),
                    "genre_found": (t.get("genres") or self._found_genres(t) or [None])[0], "genre": genres.get(t.get("id")), **(extra or {})}
        room, ahead = st.get("room"), st.get("ahead")
        up = ([ahead] if ahead and (not room or ahead.get("key") != room.get("key")) else []) + list(st.get("queue") or [])
        return {"running": bool(st.get("running")), "streaming": bool(st.get("streaming")), "paused": bool(st.get("paused")),
                "mode": st.get("mode"), "delay": st.get("delay"), "target_delay": st.get("target_delay"),
                "sync_warning": st.get("sync_warning", ""),
                "color_mode": jc.get("mode"), "theme_on": bool(jc.get("on", True)), "party_energy": self.party_energy,
                "energy_ramp": getattr(self, "ramp_status", lambda: None)(),
                "color_moments": getattr(self, "color_moments_status", lambda: [])(),
                "even_levels": bool(getattr(self, "load_config", dict)().get("party_even_levels", True)),
                "color_source": "album" if not jc.get("on", True) else "wild" if jc.get("wild") else "album" if jc.get("mode") not in ("pick", "mix") else "party",
                "theme": ([jc.get("primary"), jc.get("secondary")] if jc.get("mode") == "pick" else jc.get("pairs") if jc.get("mode") == "mix" else []),
                "rooms": self.studio.rooms.list() if getattr(self.studio, "rooms", None) else [],
                "show_moments": self.studio.show_moments.list() if getattr(self.studio, "show_moments", None) else [],
                "doorbell_show_moment": getattr(self, "_doorbell_show_moment", lambda: "")(),
                "addons": {k: bool(v) for k, v in ((getattr(self, "load_config", dict)() or {}).get("addons") or {}).items()},
                "playlist_learning": self.playlist_learning.status() if getattr(self, "playlist_learning", None) else {},
                "show_moment": self.studio.show_moments.status() if getattr(self.studio, "show_moments", None) else {},
                "room": (self._opener_room() if hasattr(self, "_opener_room") else None) or item(room, {"position": room.get("position"), "playing": room.get("playing")} if room else None),
                "upcoming": [item(t) for t in up[:12]], "genre_choices": st.get("genre_choices") or [],
                "spotify_now": (ahead or {}).get("id"),            # the song Spotify plays now (a skip goes to the one after)
                "party_colors": self._color_sets() if hasattr(self, "_color_sets") else {}}

    async def party_state(self, request):
        if not self._lan(request):
            raise web.HTTPForbidden()
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    async def party_colors(self, request):
        """From the phone: a track's own two colors (kept for next time), a track out of the party's colors
        ({track_id, theme: false}: its cover's), back to the party's, or the party's colors on or off ({on})."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        if not body.get("track_id") and "on" in body:
            self._set_theme_on(bool(body["on"]))
        elif not body.get("track_id"):
            return web.json_response({"error": "no track"}, status=400)
        else:
            self._set_track_colors(body)
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    def _set_track_colors(self, body: dict) -> None:
        self.library.set_colors(str(body["track_id"]), _hex_color(body.get("primary")), _hex_color(body.get("secondary")),
                                off=body.get("theme") is False, only=bool(body.get("only")))

    def _set_genre(self, body: dict) -> None:
        if not body.get("track_id"):
            raise web.HTTPBadRequest(text="no track")
        self.library.set_genre(str(body["track_id"]), body.get("genre"))

    def _set_style(self, body: dict) -> None:
        tid = str(body.get("track_id") or "")
        if not tid:
            raise web.HTTPBadRequest(text="no track")
        self.library.set_style(tid, body.get("style"))

    async def party_style(self, request):
        """From the phone: a song's vibe ({track_id, style: chill | groove | dance | rave}, or none for the
        one its analysis finds), kept for that track; the song playing is rebuilt in it at once."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        self._set_style(await request.json())
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    async def party_genre(self, request):
        """From the phone: a song's genre ({track_id, genre: one of genre_choices' keys}, or none for the one
        the analysis hears), kept for that track; the song playing is rebuilt in it at once."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        self._set_genre(await request.json())
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    async def track_genre(self, request):
        if not self._local(request):
            raise web.HTTPForbidden()
        self._set_genre(await request.json())
        return await self._run(self._state)

    async def track_style(self, request):
        if not self._local(request):
            raise web.HTTPForbidden()
        self._set_style(await request.json())
        return await self._run(self._state)

    async def party_playback(self, request):
        """Play, pause, skip or go back on Spotify, the way the phone's own controls would. (A song gone back
        to was heard or learned ahead already: it gets its full show.)"""
        if not self._lan(request):
            raise web.HTTPForbidden()
        action = (await request.json()).get("action")
        fn = self._playback_fn(action)
        if fn is None:
            return web.json_response({"error": "unknown action"}, status=400)
        ok = await asyncio.get_running_loop().run_in_executor(None, fn)
        return web.json_response({"ok": bool(ok)})

    def _playback_fn(self, action: str):
        """What play, pause, skip or back does now (the demo's own, or Spotify's), or None."""
        if self.demo is not None:
            fn = {"next": self.demo.skip, "pause": self.demo.pause, "resume": self.demo.resume}.get(action)
            return (lambda: fn() or True) if fn else None
        fn = {"pause": self.spotify.pause, "resume": self.spotify.resume, "next": self.spotify.next_track,
              "previous": self.spotify.previous_track}.get(action)
        if action == "resume" and self.source == "studio" and self.player.device_id:
            dev = self.player.device_id
            fn = lambda: self.spotify.resume_on(dev)    # (the studio's player, woken if Spotify let it go)
        return fn

    async def party_volumes(self, request):
        """The speaker the party plays on (its volume is the cast's), and every speaker and group's own."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        cast = self.studio.cast

        rooms = getattr(self.studio, "rooms", None)
        bulbs = self._bulbstudio()

        def read():
            st = cast.status()
            devices = cast.device_volumes()
            in_use = bulbs.speakers_in_use() if bulbs else {}      # (a bulb show playing on it: its volume is set here too)
            for d in devices:
                if d["name"] in in_use:
                    d["bulb_studio_show"] = in_use[d["name"]]
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
                    # a speaker muted (its room off, or by hand) keeps the level for later and stays muted:
                    # sending a level to a Google speaker unmutes it
                    quiet = n in muted or cast.is_muted(n)
                    if rooms:
                        rooms.remember_level(n, v, sent=not quiet)   # kept (and being sent: see Rooms.keep_levels)
                    if n in muted:
                        rooms.set_saved_volume(n, v)
                    elif not quiet:
                        cast.set_device_volume(n, v)
                return {"levels": levels}
            try:
                return web.json_response(await asyncio.get_running_loop().run_in_executor(None, set_all))
            except Exception as e:  # noqa: BLE001
                return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)
        if rooms and (body.get("name") or cast.cast is not None):
            rooms.remember_level(str(body.get("name") or cast.name), level, sent=True)
        def via_group(name, set_it):
            """Set a volume; a group's moves every speaker in it. The speakers muted before (by hand, or by their
            room) are muted again if the group's change unmuted them (the user), and the others' new levels
            become the kept ones."""
            if not cast.is_group(name):
                return set_it()
            cast.device_volumes()                                # (every speaker's state read first)
            before = cast.muted_speakers()
            out = set_it()

            def after():
                again = cast.mute_again(before)
                if again and self.engine is not None:
                    self.engine.say(f"group volume: kept muted {', '.join(again)}")
                if rooms:
                    rooms.keep_as_they_are(wait_s=0)
            threading.Thread(target=after, daemon=True, name="group-volume").start()
            return out
        if body.get("name") and rooms and str(body["name"]) in rooms.muted():
            fn = lambda: (rooms.set_saved_volume(str(body["name"]), level), {"name": body["name"], "volume": level, "muted": True})[1]
        elif body.get("name"):
            fn = lambda: via_group(str(body["name"]), lambda: cast.set_device_volume(str(body["name"]), level))
        else:
            if cast.cast is None:
                return web.json_response({"error": "not casting"}, status=400)
            fn = lambda: via_group(None, lambda: cast.set_volume(level))
        try:
            return web.json_response(await asyncio.get_running_loop().run_in_executor(None, fn))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=400)

    def _bulbstudio(self):
        """Bulb Studio, while its add-on is on (else None: not asked about, never started)."""
        bulbs = getattr(self.studio, "bulbstudio", None)
        return bulbs if bulbs is not None and (self.load_config().get("addons") or {}).get("bulbstudio") else None

    async def party_bulbstudio(self, request):
        """Bulb Studio's shows that can run, each on or off (their switches on the Party screen and the phone)."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        bulbs = self._bulbstudio()
        if bulbs is None:
            return web.json_response({"open": False, "shows": []})
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, bulbs.list))

    async def bulbstudio_open(self, request):
        """The studio's Bulb Studio button: Bulb Studio running, and the address of its window ({ok, url, error})."""
        if not self._local(request):
            raise web.HTTPForbidden()
        bulbs = self._bulbstudio()
        if bulbs is None:
            return web.json_response({"ok": False, "url": "", "error": "the Bulb Studio add-on is off"})
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, bulbs.open_window))

    async def party_bulbstudio_set(self, request):
        """Switch a bulb show on or off ({id, on}). Never on the speaker the party plays on."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        bulbs, cast = self._bulbstudio(), self.studio.cast
        if bulbs is None or not body.get("id"):
            return web.json_response({"ok": False, "error": "no such bulb show"}, status=400)
        busy = {cast.name} if cast.cast is not None and cast.name else set()
        try:
            out = await asyncio.get_running_loop().run_in_executor(None, bulbs.set, str(body["id"]), bool(body.get("on")), busy)
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return web.json_response(out)

    async def party_even(self, request):
        """From the phone: every song at about the same volume, on or off ({on}); kept for next time."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        on = bool((await request.json()).get("on", True))
        cfg = self.load_config()
        cfg["party_even_levels"] = on
        self.save_config(cfg)
        if self.engine is not None:
            self.engine.even_levels(on)
        return web.json_response({"even_levels": on})

    async def party_energy_set(self, request):
        """The slider: the level, at once (a build-up under way stops: the hand on the slider wins)."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        self.party_energy = max(0.0, min(1.0, float((await request.json()).get("level", 1.0))))
        self.energy_ramp = None
        self._save_level()
        return web.json_response({"party_energy": self.party_energy, "energy_ramp": None})

    # -- the Energy, built up over an hour ---------------------------------------------
    # The button under the slider (the user): from where it is to 100% over an hour, moving only as a new song
    # starts in the room (each song plays at one level, the next a step up), to where the hour says it should be.
    RAMP_MINUTES = 60

    def _save_level(self) -> None:
        cfg = self.load_config()
        cfg["party_energy"] = round(self.party_energy, 3)
        if self.energy_ramp:
            cfg["energy_ramp"] = self.energy_ramp
        else:
            cfg.pop("energy_ramp", None)
        self.save_config(cfg)

    def _ramp_step(self, song_key: str | None, now: float | None = None) -> bool:
        """A new song in the room during a build-up: the level to where the hour has got to (100% ends it).
        True when the level moved."""
        r = self.energy_ramp
        if not r or not song_key or song_key == r.get("song"):
            return False
        r["song"] = song_key
        if r.get("waiting"):                              # (the song playing as the button was pressed keeps its level)
            r["waiting"] = False
            self._save_level()
            return False
        now = time.time() if now is None else now
        k = max(0.0, min(1.0, (now - float(r["start"])) / (60.0 * float(r.get("minutes") or self.RAMP_MINUTES))))
        was = self.party_energy
        self.party_energy = max(was, round(float(r["from"]) + (1.0 - float(r["from"])) * k, 2))   # (never down)
        if self.party_energy >= 1.0:
            self.party_energy, self.energy_ramp = 1.0, None
        self._save_level()
        return self.party_energy != was

    def ramp_status(self) -> dict | None:
        r = self.energy_ramp
        if not r:
            return None
        minutes = float(r.get("minutes") or self.RAMP_MINUTES)
        return {"from": r["from"], "minutes": minutes, "left_s": max(0, round(float(r["start"]) + 60 * minutes - time.time()))}

    async def energy_ramp_set(self, request):
        """The button: {on} starts a build-up from the level now to 100% over an hour, or stops it."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        if body.get("on") and self.party_energy < 1.0:
            room = None
            if self.engine is not None:
                room = (self.engine.state().get("room") or {}).get("key")
            self.energy_ramp = {"from": round(self.party_energy, 3), "start": time.time(),
                               "minutes": float(body.get("minutes") or self.RAMP_MINUTES), "song": room, "waiting": room is None}
        else:
            self.energy_ramp = None
        self._save_level()
        return web.json_response({"party_energy": self.party_energy, "energy_ramp": self.ramp_status()})

    # -- color moments -------------------------------------------------------------------------------
    # Buttons on the phone remote that hold the party for a moment, each one made by the user (phone remote: More settings,
    # Color moments): every light one color at full brightness (the studio page draws it), and the music turned down in the
    # stream (the speakers stay at their volume) or paused, fading in and out over the mode's own time. A mode may
    # ask twice before it turns on (an accidental tap would stop the party). Its color can be changed while it is on;
    # each time it turns on it starts at its own.
    DEFAULT_MODES = [
        {"id": "announce", "name": "Announcement", "music": 0.2, "pause": False, "fade": 5.0, "color": "#ffaa5a", "confirm": False},
        {"id": "stop", "name": "Stop the party", "music": 0.0, "pause": True, "fade": 0.0, "color": "#ffaa5a", "confirm": True},
    ]
    MAX_MODES = 12

    @staticmethod
    def _cover_color(c) -> str | None:
        """A picked color, as #rrggbb, or None when it is not one."""
        c = str(c or "").strip().lower()
        return c if len(c) == 7 and c[0] == "#" and all(ch in "0123456789abcdef" for ch in c[1:]) else None

    @classmethod
    def clean_color_moments(cls, color_moments) -> list[dict]:
        """Color moments as kept: {id, name, music (0..1 of itself), pause, fade (s), color, confirm}, checked."""
        import uuid
        out, ids = [], set()
        for m in color_moments if isinstance(color_moments, list) else []:
            if not isinstance(m, dict):
                continue
            mid = re.sub(r"[^a-z0-9_-]", "", str(m.get("id") or "").lower())[:40]
            while not mid or mid in ids:
                mid = uuid.uuid4().hex[:8]
            ids.add(mid)
            num = lambda k, d, lo, hi: max(lo, min(hi, float(m.get(k, d) if m.get(k) is not None else d)))
            out.append({"id": mid, "name": str(m.get("name") or "").strip()[:40] or "Mode", "music": round(num("music", 0.2, 0.0, 1.0), 3),
                        "pause": bool(m.get("pause")), "fade": round(num("fade", 5.0, 0.0, 30.0), 2),
                        "color": cls._cover_color(m.get("color")) or "#ffaa5a", "confirm": bool(m.get("confirm", True))})
        return out[:cls.MAX_MODES]

    def color_moments(self) -> list[dict]:
        cfg = self.load_config()
        return self.clean_color_moments(cfg["color_moments"]) if "color_moments" in cfg else [dict(m) for m in self.DEFAULT_MODES]

    def color_moments_status(self) -> list[dict]:
        """Every mode, on or off, its color now, and how long ago it was switched (the studio page fades from then)."""
        now, out = time.monotonic(), []
        for m in self.color_moments():
            st = self.color_moment_on.get(m["id"]) or {"on": False, "at": -1e9}
            out.append({**m, "own": m["color"], "on": bool(st["on"]), "age": round(max(0.0, now - st["at"]), 2), "color": st.get("color") or m["color"]})
        return out

    def _music_gain(self) -> float:
        """The music's level with the color moments that are on: the quietest one asks for (1: none on)."""
        ms = {m["id"]: m for m in self.color_moments()}
        return min([ms[i]["music"] for i, st in self.color_moment_on.items() if st.get("on") and i in ms and not ms[i]["pause"]] or [1.0])

    def _music_playing(self) -> bool:
        now = getattr(self.engine, "now", None) if self.engine is not None else None
        return bool(now.playing) if now is not None and now.track is not None else self.engine is not None

    def _mode(self, mid: str, on: bool | None, color: str | None = None) -> dict:
        """Switch a mode on (its own color, or the one asked for) or off, or only change its color."""
        m = next((x for x in self.color_moments() if x["id"] == mid), None)
        if m is None:
            raise ValueError(f"no mode {mid!r}")
        st = self.color_moment_on.setdefault(mid, {"on": False, "at": -1e9, "paused": False})
        if on is not None and on != st["on"]:
            if m["pause"]:
                if on:
                    was = self._music_playing()
                    st.update(on=True, at=time.monotonic(), paused=was, color=color or m["color"])
                    if was:
                        self._playback_fn("pause")()
                else:
                    paused = st.get("paused")
                    st.update(on=False, at=time.monotonic(), paused=False)
                    if paused:
                        self._playback_fn("resume")()          # (only what it paused: music paused already stays paused)
            else:
                st.update(on=on, at=time.monotonic(), color=(color or m["color"]) if on else st.get("color"))
                if self.engine is not None:
                    self.engine.duck_music(self._music_gain(), m["fade"])
        elif color:
            st["color"] = color
        return {"color_moments": self.color_moments_status()}

    async def color_moment_set(self, request):
        """A mode's button: {id, on?, color? (#rrggbb)}. The studio page is told first, so the lights change as the
        music does (a pause asks Spotify, which takes a moment)."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        mid, on, color = str(body.get("id") or ""), (bool(body["on"]) if "on" in body else None), self._cover_color(body.get("color"))
        was = next((x for x in self.color_moments_status() if x["id"] == mid), None)
        if was is None:
            return web.json_response({"error": f"no mode {mid!r}"}, status=404)
        ahead = {**was, "age": 0} if on is None or on == was["on"] else {**was, "on": on, "age": 0, "color": (color or (was["color"] if not on else next(
            m["color"] for m in self.color_moments() if m["id"] == mid)))}
        if on is None and color:
            ahead["color"] = color
        await self._tell_studio(mid, ahead)
        return await self._run(self._mode, mid, on, color)

    def _set_color_moments(self, color_moments) -> dict:
        """Keep the color moments as edited on the phone remote; one taken away while it is on is switched off first."""
        new = self.clean_color_moments(color_moments)
        keep, before = {m["id"] for m in new}, self._music_gain()
        for mid, st in list(self.color_moment_on.items()):
            if st.get("on") and mid not in keep:
                self._mode(mid, False)
        cfg = self.load_config()
        cfg["color_moments"] = new
        self.save_config(cfg)
        for mid in list(self.color_moment_on):
            if mid not in keep:
                del self.color_moment_on[mid]
        if self.engine is not None and self._music_gain() != before:
            self.engine.duck_music(self._music_gain(), 1.0)       # (a mode on, edited to a new level: over a second)
        return {"color_moments": self.color_moments_status()}

    async def color_moments_set(self, request):
        """The color moments as edited: {color moments: [{id?, name, music, pause, fade, color, confirm}]}."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        return await self._run(self._set_color_moments, body.get("color_moments"))

    async def _tell_studio(self, name: str, state: dict) -> None:
        tell = getattr(self.studio, "tell_studio", None)
        if tell is not None:
            await tell({"type": "cover", "name": name, "state": state})

    async def colors(self, request):
        """Set the party's colors ({mode, primary, secondary}), or one track's ({track_id, primary,
        secondary}; without the colors, the track goes back to the party's)."""
        if not self._local(request):
            raise web.HTTPForbidden()
        body = await request.json()
        if body.get("track_id"):
            self._set_track_colors(body)
        else:
            self._set_color_settings(body)
        # off the event loop: handing out pairs afresh reads what was learned about each queued track
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._state))

    # The party's colors, as kept: what every song plays in ("pick": two colors; "mix": a pair for each song;
    # "album": its cover's), and named sets of them (a Halloween set...) to switch between.
    SET_KEYS = ("mode", "primary", "secondary", "pairs", "rainbow", "wild")

    def _set_color_settings(self, body: dict) -> None:
        """Change the party's colors: {on}, {mode}, {primary, secondary}, {pairs}, {extras}, {rainbow}, {wild}; and the
        named sets: {use: name} puts one on, {save_as: name} keeps the colors as they are under a name,
        {save: true} keeps them in the set they came from, {delete: name} forgets one."""
        cfg = self.load_config()
        cur = {**self.DEFAULT_COLORS, **cfg.get("color_settings", {})}
        sets = dict(cfg.get("color_sets") or {})
        if body.get("use") in sets:
            cur.update({k: v for k, v in sets[body["use"]].items() if k in self.SET_KEYS})
            cur["set"], cur["on"] = body["use"], True
        if "on" in body:
            cur["on"] = bool(body["on"])
        if body.get("mode") in ("pick", "mix", "album"):
            cur["mode"] = body["mode"]
        for k in ("primary", "secondary", "accent1", "accent2"):
            if _hex_color(body.get(k)):
                cur[k] = _hex_color(body[k])
        for k in ("extras", "rainbow", "wild"):
            if k in body:
                cur[k] = bool(body[k])
        if body.get("source") in ("album", "party", "wild"):     # the three ways a song gets its colors
            cur["on"], cur["wild"] = body["source"] != "album", body["source"] == "wild"
            if body["source"] == "party" and cur.get("mode") not in ("pick", "mix"):
                cur["mode"] = "mix"                              # (album art is its own choice now)
        if isinstance(body.get("pairs"), list):
            pairs = [[_hex_color(p[0]), _hex_color(p[1])] for p in body["pairs"][:8] if isinstance(p, list) and len(p) == 2]
            if pairs and all(a and b for a, b in pairs):
                cur["pairs"] = pairs
        name = str(body.get("save_as") or "").strip()[:30]
        if name:
            sets[name], cur["set"] = {k: cur[k] for k in self.SET_KEYS}, name
        elif body.get("save") and cur.get("set") in sets:
            sets[cur["set"]] = {k: cur[k] for k in self.SET_KEYS}
        if body.get("delete") in sets:
            sets.pop(body["delete"])
            if cur.get("set") == body["delete"]:
                cur["set"] = ""
        self._assigned.clear()                                  # pairs or mode changed: hand them out afresh
        cfg["color_settings"], cfg["color_sets"] = cur, sets
        self.save_config(cfg)

    def _color_sets(self) -> dict:
        """The party's colors for the phone: as they are, the set they came from (and whether they have been
        changed since), and every set's name."""
        cfg = self.load_config()
        cur, sets = {**self.DEFAULT_COLORS, **cfg.get("color_settings", {})}, cfg.get("color_sets") or {}
        name = cur.get("set") if cur.get("set") in sets else ""
        edited = bool(name) and any(sets[name].get(k) != cur.get(k) for k in self.SET_KEYS)
        return {"mode": cur["mode"], "primary": cur["primary"], "secondary": cur["secondary"], "pairs": cur["pairs"],
                "set": name, "edited": edited, "sets": sorted(sets, key=str.lower)}

    async def party_theme(self, request):
        """From the phone: the party's colors and their named sets (see _set_color_settings)."""
        if not self._lan(request):
            raise web.HTTPForbidden()
        body = await request.json()
        await asyncio.get_running_loop().run_in_executor(None, self._set_color_settings, body)
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self._party))

    def _set_theme_on(self, on: bool) -> None:
        """The party's colors for every song, or (off) every song in its own album cover's colors."""
        cfg = self.load_config()
        cfg["color_settings"] = {**self.DEFAULT_COLORS, **cfg.get("color_settings", {}), "on": bool(on)}
        self.save_config(cfg)

    def _auto_on(self) -> bool:
        return bool(self.load_config().get("party_auto", True))

    async def art(self, request):
        """Album art, relayed so the studio page may read its colors. Spotify's image server only."""
        url = request.query.get("url", "")
        if not url.startswith("https://i.scdn.co/image/"):
            raise web.HTTPForbidden()
        cache = self._art
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
        """Settings saved as they are changed: where it plays, auto-start, the delay, how a skip behaves."""
        if not self._local(request):
            raise web.HTTPForbidden()
        body = await request.json()
        cfg = self.load_config()
        if "auto" in body:
            cfg["party_auto"] = bool(body["auto"])
            self.auto_armed = True
        if body.get("source") in ("studio", "app"):
            cfg["party_last"] = {**cfg.get("party_last", {}), "source": body["source"]}
        for key in ("speakers", "crossfade", "skip_jumps"):
            if key in body:
                cfg["party_last"] = {**cfg.get("party_last", {}), key: body[key]}
        if "cast_device" in body:                             # the speaker it plays on, connected when it starts
            cfg["party_cast_device"] = str(body["cast_device"] or "")
        if "delay" in body:                                   # (learning ahead keeps its own)
            if self._fetch_on():
                cfg["party_fetch_delay"] = float(body["delay"])
            else:
                cfg["party_last"] = {**cfg.get("party_last", {}), "target_delay": float(body["delay"])}
        if "opener" in body:                                  # the saved show every party opens with ("" for none)
            cfg["party_opener"] = str(body["opener"] or "")
        if "fetch" in body:                                   # learn ahead from YouTube copies
            cfg["party_fetch"] = bool(body["fetch"])
            self.playlist_learning.fetch = self.fetch if body["fetch"] else None
        self.save_config(cfg)
        return web.json_response(self._state())

    async def player_open(self, request):
        return await self._run(lambda: (self.hidden.start(), self._state())[1])

    async def player_transfer(self, request):
        if not self._local(request):
            raise web.HTTPForbidden()
        if not self.player.device_id:
            return web.json_response({"error": "the studio player is not ready yet"}, status=400)
        return await self._run(lambda: {"ok": self.spotify.transfer_playback(self.player.device_id)})

    async def signout(self, request):
        self.spotify.sign_out()
        return web.json_response(self._state())
