"""Moments: buttons on the phone remote that stop the party for a moment (a doorbell, shot o'clock).

A moment cuts straight to one of your saved shows (its sound on the speakers, its lights in the room),
plays it to its very end, then the music fades back in over MUSIC_BACK_S, from where it stopped. (The
user asked for exactly this: no fade out first, no songs overlapping, the same 2 s fade in after every moment; the per-moment fade settings are gone.) In a party the room is behind Spotify; Spotify is paused, the
buffer held, and the room picks up exactly where it stopped.

In a party on the speakers (or this PC), the show's song is played inside the party's own stream: the
speakers get one fresh stream, as for a skip, with the song first and the music after it, fading in.
The speakers are never paused, and never load the show's song, so nothing waits in between. The songs
are decoded ahead of time and kept in memory.

The fade is in the audio itself, never on the speakers' volume: a Google speaker chimes at every
volume change. Without a party there is no audio of ours to fade, and the music just stops and starts.

Where the show plays:
- the party is on Cast speakers (or the studio is casting): the show's song goes to the same speaker
  or group, and the studio page plays its lights following that speaker; afterwards the speakers get
  the party's stream back from exactly where the room stopped;
- the party is on this PC (or there is no party and no cast): the studio page plays the song on this PC
  and its lights;
- a silent preview party: the lights only.
Without a party, Spotify is paused only if something is playing, and resumed only if it was.

A test copy of the studio (--no-lights) runs every moment as a rehearsal: each step happens on its real
timing and the phone remote follows along, but no speaker, light or Spotify is touched.
"""

from __future__ import annotations

import random
import re
import threading
import time
import uuid

DEFAULT_SHOW_MOMENTS = [
    {"id": "doorbell", "name": "Doorbell", "show": ""},
    {"id": "shot", "name": "Shot o'clock", "show": ""},
]
MUSIC_BACK_S = 2.0         # after every moment, the music fades back in over this


def clean(show_moments) -> list[dict]:
    out, ids = [], set()
    for c in show_moments if isinstance(show_moments, list) else []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", cid) or cid in ids:
            cid = uuid.uuid4().hex[:10]
        ids.add(cid)
        # its shows: one picked at random each time it runs (a doorbell's spooky ones); "show" is the first,
        # for a moment saved with one show and for whatever reads only that
        shows = []
        for s in (c.get("shows") if isinstance(c.get("shows"), list) else [c.get("show")]):
            s = str(s or "").strip()[:120]
            if s and s not in shows:
                shows.append(s)
        shows = shows[:20]
        out.append({"id": cid, "name": str(c.get("name") or "").strip()[:30] or f"Moment {len(out) + 1}",
                    "show": shows[0] if shows else "", "shows": shows})
    return out[:12]


class Rehearsal:
    """Stands in for the speakers, Spotify and the show: remembers what would have been done."""

    def __init__(self, show_length):
        self.show_length = show_length                  # show name -> seconds
        self.calls: list = []

    def pause_music(self) -> None:
        self.calls.append("pause")

    def resume_music(self, fade_in: float = 0.0) -> bool:
        self.calls.append("resume")
        return fade_in > 0

    def play_show(self, name: str) -> float:
        self.calls.append(("show", name))
        return float(self.show_length(name) or 0.0)

    def stop_show(self) -> None:
        self.calls.append("stop show")


class LiveHooks:
    """The real thing: the party (or the studio's cast), Spotify and the speakers."""

    def __init__(self, cast, party, song, show_length, pcm=None):
        self.cast, self.party = cast, party                  # CastManager, PartyService
        self.song = song                                 # show name -> (audio URL for the speaker, file path)
        self.show_length = show_length
        self.pcm = pcm                                   # show name -> (int16 frames, rate) at the party's rate, or None
        self.engine = party.engine if party is not None else None
        kind = party._output_kind(self.engine.output) if self.engine is not None else None
        if kind == "cast" or (kind is None and cast.cast is not None):
            self.via = "cast"
        elif kind == "none":
            self.via = "none"                            # a silent preview party: the lights only
        else:
            self.via = "pc"
        self.device = cast.name if self.via == "cast" else ""
        self.paused_spotify = False

    # -- a moment played inside the party's stream ---------------------------------------------------
    def stream_show_moment(self, name: str) -> float | None:
        """Put the show's song into the party's stream, the music fading back in after it. Returns its length,
        or None when it cannot be (no party on speakers, or the song is not ready)."""
        why = ("no party" if self.engine is None else "the party is not on speakers" if self.via not in ("cast", "pc")
               else "the party's speakers have not started" if not getattr(self.engine, "streaming", False) else "" if self.pcm else "no audio loader")
        got = None if why else self.pcm(name)
        if not why and got is None:
            why = f"{name}'s audio could not be loaded"
        if not why:
            frames, rate = got
            if self.engine.show_moment_insert(frames, MUSIC_BACK_S):
                return len(frames) / float(rate)
            why = "no stream to put it in"
        if self.engine is not None and hasattr(self.engine, "say"):
            self.engine.say(f"a moment the old way (its song loaded on the speakers): {why}")
        return None

    def show_moment_position(self) -> float | None:
        return self.engine.show_moment_position() if self.engine is not None else None

    def music_back(self) -> None:
        """The song is over; the music fades back in (already in the stream)."""
        self.engine.show_moment_over()

    def cut_short(self, fade_in: float) -> None:
        """Ended early: the music comes back from where it held, fading in."""
        self.engine.release(restart=True, fade_in=fade_in)

    def pause_music(self) -> None:
        if self.engine is not None:
            self.engine.hold()
            return
        sp = getattr(self.party, "spotify", None)
        try:
            if sp is not None and sp.signed_in and sp.now_playing().playing:
                sp.pause()
                self.paused_spotify = True
        except Exception:  # noqa: BLE001 - no Spotify: the show plays anyway
            pass

    def play_show(self, name: str) -> float:
        length = float(self.show_length(name) or 0.0)
        if self.via == "cast":
            url, path = self.song(name)
            self.cast.play(url, path, name, 0.0)
        return length                                    # "pc" and "none": the studio page plays it

    def stop_show(self) -> None:
        if self.via == "cast":
            try:
                self.cast.stop()
            except Exception:  # noqa: BLE001
                pass

    def resume_music(self, fade_in: float = 0.0) -> bool:
        """The music comes back, fading in when it is the party's own audio. True if it fades."""
        if self.engine is not None:
            # the speakers (or this PC) get the party's stream back from where the room stopped
            restart = self.via in ("cast", "pc")
            self.engine.release(restart=restart, fade_in=fade_in if restart else 0.0)
            return restart and fade_in > 0
        if self.paused_spotify:
            try:
                self.party.spotify.resume()
            except Exception:  # noqa: BLE001
                pass
        return False


class ShowMomentRunner:
    def __init__(self, load_config, save_config, show_length, clock=time.monotonic, sleep=time.sleep, live=None):
        self.load_config, self.save_config = load_config, save_config
        self.show_length = show_length
        self.live = live                                 # () -> LiveHooks for the real thing; None: rehearsals only
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state: dict = {}
        self.hooks = None
        self.bags: dict[str, list[str]] = {}             # moment id -> its shows still to come this round
        self.last_show: dict[str, str] = {}

    def pick(self, show_moment: dict) -> str:
        """One of the moment's shows at random, none again until every one has played (a shuffled bag, refilled
        when empty), and a new round never starting with the one just played."""
        shows = show_moment.get("shows") or ([show_moment["show"]] if show_moment.get("show") else [])
        if len(shows) <= 1:
            return shows[0] if shows else ""
        cid = show_moment["id"]
        bag = [s for s in self.bags.get(cid, []) if s in shows]
        if not bag:
            bag = random.sample(shows, len(shows))
            if bag[0] == self.last_show.get(cid):
                bag.append(bag.pop(0))
        show = bag.pop(0)
        self.bags[cid], self.last_show[cid] = bag, show
        return show

    # -- the moments -------------------------------------------------------------------------------
    def list(self) -> list[dict]:
        cfg = self.load_config()
        return clean(cfg["show_moments"]) if "show_moments" in cfg else clean(DEFAULT_SHOW_MOMENTS)

    def replace(self, show_moments) -> list[dict]:
        cfg = self.load_config()
        cfg["show_moments"] = clean(show_moments)
        self.save_config(cfg)
        return cfg["show_moments"]

    # -- running one ----------------------------------------------------------------------------
    def status(self) -> dict:
        st = dict(self.state)
        if st.get("phase") and st.get("phase") != "done" and st.get("t0") is not None and st.get("dur"):
            st["progress"] = round(max(0.0, min(1.0, (self.clock() - st["t0"]) / st["dur"])), 3)
        st.pop("t0", None)
        return st

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def run(self, cid: str, hooks=None) -> dict:
        show_moment = next((c for c in self.list() if c["id"] == cid), None)
        if show_moment is None:
            raise LookupError("no such show_moment")
        if not show_moment["show"]:
            raise ValueError(f"{show_moment['name']} has no show yet: pick one in Settings")
        if self.busy():
            raise RuntimeError(f"{self.state.get('name', 'A moment')} is still running")
        show_moment = {**show_moment, "show": self.pick(show_moment)}
        self.hooks = hooks or (self.live() if self.live else Rehearsal(self.show_length))
        self.stop_flag.clear()
        self.state = {"id": show_moment["id"], "name": show_moment["name"], "phase": "starting", "rehearsal": isinstance(self.hooks, Rehearsal), "steps": [],
                      "run": uuid.uuid4().hex[:8], "show": show_moment["show"], "via": getattr(self.hooks, "via", "none"),
                      "device": getattr(self.hooks, "device", "")}
        self.thread = threading.Thread(target=self._run, args=(show_moment,), daemon=True)
        self.thread.start()
        return self.status()

    def stop(self) -> dict:
        """End the show early: the music comes back as it would at the show's end."""
        self.stop_flag.set()
        return self.status()

    def _phase(self, name: str, dur: float = 0.0) -> None:
        self.state.update(phase=name, t0=self.clock(), dur=dur)
        self.state["steps"] = self.state.get("steps", []) + [name]

    def _wait(self, secs: float) -> None:
        end = self.clock() + secs
        while self.clock() < end:
            self.sleep(min(0.1, max(0.0, end - self.clock())))

    def _run(self, show_moment: dict) -> None:
        h = self.hooks
        try:
            dur = h.stream_show_moment(show_moment["show"]) if hasattr(h, "stream_show_moment") else None
        except Exception:  # noqa: BLE001 - its song would not load for the stream: played the ordinary way, which says why
            dur = None
        if dur:
            return self._run_in_stream(h, dur)
        try:
            self._phase("pause")
            h.pause_music()
            dur = h.play_show(show_moment["show"])
            self._phase("show", dur)
            end = self.clock() + dur
            while self.clock() < end and not self.stop_flag.is_set():
                self.sleep(min(0.1, max(0.0, end - self.clock())))
            if self.stop_flag.is_set():
                h.stop_show()
            self._phase("resume")
            if h.resume_music(MUSIC_BACK_S):
                self._phase("fade_in", MUSIC_BACK_S)
                self._wait(MUSIC_BACK_S)
            self._phase("done")
        except Exception as e:  # noqa: BLE001 - the music must come back whatever went wrong
            self.state["error"] = f"{type(e).__name__}: {e}"
            try:
                h.resume_music()
            except Exception:  # noqa: BLE001
                pass
            self._phase("done")

    def _run_in_stream(self, h, dur: float) -> None:
        """The song is in the party's stream already: follow it by the speakers' own position (they take a
        moment to start a fresh stream) to its very end, then hand the room back as the music fades in."""
        self.state["stream"] = True                      # the page plays only its lights, never its sound
        self._phase("show", dur)
        t0, stopped = self.clock(), False
        try:
            while True:
                if self.stop_flag.is_set():
                    stopped = True
                    break
                p = h.show_moment_position()
                at = p if p is not None else self.clock() - t0
                left = dur - at
                if left <= 0 or self.clock() - t0 > dur + 10:
                    break
                self.sleep(min(0.1, max(0.01, left)))
            if stopped:
                h.cut_short(MUSIC_BACK_S)
            else:
                h.music_back()
            self._phase("fade_in", MUSIC_BACK_S)
            self._wait(MUSIC_BACK_S)
            self._phase("done")
        except Exception as e:  # noqa: BLE001 - the music must come back whatever went wrong
            self.state["error"] = f"{type(e).__name__}: {e}"
            try:
                h.cut_short(0.0)
            except Exception:  # noqa: BLE001
                pass
            self._phase("done")
