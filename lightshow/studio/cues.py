"""Cues: buttons on the Party Hub that stop the party for a moment (a doorbell, shot o'clock).

A cue stops the music at once, plays one of your saved shows (its sound on the speakers, its lights
in the room), then brings the music straight back. In a jam the room is behind Spotify; pausing
Spotify pauses the room at once and holds the buffer, so the room picks up exactly where it stopped.

The music is never faded: a Google speaker chimes at every volume change, so a fade on the speakers
was a run of beeps, and a fade in the audio itself would reach the room late (a speaker buffers up
to 20 s ahead). The speakers' volume is never touched.

Where the show plays:
- the jam is on Cast speakers (or the studio is casting): the show's song goes to the same speaker
  or group, and the studio page plays its lights following that speaker; afterwards the speakers get
  the jam's stream back from exactly where the room stopped;
- the jam is on this PC (or there is no jam and no cast): the studio page plays the song on this PC
  and its lights;
- a silent preview jam: the lights only.
Without a jam, Spotify is paused only if something is playing, and resumed only if it was.

A test copy of the studio (--no-lights) runs every cue as a rehearsal: each step happens on its real
timing and the Party Hub follows along, but no speaker, light or Spotify is touched.
"""

from __future__ import annotations

import re
import threading
import time
import uuid

DEFAULT_CUES = [
    {"id": "doorbell", "name": "Doorbell", "show": ""},
    {"id": "shot", "name": "Shot o'clock", "show": ""},
]


def clean(cues) -> list[dict]:
    out, ids = [], set()
    for c in cues if isinstance(cues, list) else []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", cid) or cid in ids:
            cid = uuid.uuid4().hex[:10]
        ids.add(cid)
        out.append({"id": cid, "name": str(c.get("name") or "").strip()[:30] or f"Cue {len(out) + 1}",
                    "show": str(c.get("show") or "")[:120]})
    return out[:12]


class Rehearsal:
    """Stands in for the speakers, Spotify and the show: remembers what would have been done."""

    def __init__(self, show_length):
        self.show_length = show_length                  # show name -> seconds
        self.calls: list = []

    def pause_music(self) -> None:
        self.calls.append("pause")

    def resume_music(self) -> None:
        self.calls.append("resume")

    def play_show(self, name: str) -> float:
        self.calls.append(("show", name))
        return float(self.show_length(name) or 0.0)

    def stop_show(self) -> None:
        self.calls.append("stop show")


class LiveHooks:
    """The real thing: the jam (or the studio's cast), Spotify and the speakers."""

    def __init__(self, cast, jam, song, show_length):
        self.cast, self.jam = cast, jam                  # CastManager, JamService
        self.song = song                                 # show name -> (audio URL for the speaker, file path)
        self.show_length = show_length
        self.engine = jam.engine if jam is not None else None
        kind = jam._output_kind(self.engine.output) if self.engine is not None else None
        if kind == "cast" or (kind is None and cast.cast is not None):
            self.via = "cast"
        elif kind == "none":
            self.via = "none"                            # a silent preview jam: the lights only
        else:
            self.via = "pc"
        self.device = cast.name if self.via == "cast" else ""
        self.paused_spotify = False

    def pause_music(self) -> None:
        if self.engine is not None:
            self.engine.hold()
            return
        sp = getattr(self.jam, "spotify", None)
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

    def resume_music(self) -> None:
        if self.engine is not None:
            self.engine.release(restart=self.via == "cast")
        elif self.paused_spotify:
            try:
                self.jam.spotify.resume()
            except Exception:  # noqa: BLE001
                pass


class CueRunner:
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

    # -- the cues -------------------------------------------------------------------------------
    def list(self) -> list[dict]:
        cfg = self.load_config()
        return clean(cfg["cues"]) if "cues" in cfg else clean(DEFAULT_CUES)

    def replace(self, cues) -> list[dict]:
        cfg = self.load_config()
        cfg["cues"] = clean(cues)
        self.save_config(cfg)
        return cfg["cues"]

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
        cue = next((c for c in self.list() if c["id"] == cid), None)
        if cue is None:
            raise LookupError("no such cue")
        if not cue["show"]:
            raise ValueError(f"{cue['name']} has no show yet: pick one in Settings")
        if self.busy():
            raise RuntimeError(f"{self.state.get('name', 'A cue')} is still running")
        self.hooks = hooks or (self.live() if self.live else Rehearsal(self.show_length))
        self.stop_flag.clear()
        self.state = {"id": cue["id"], "name": cue["name"], "phase": "starting", "rehearsal": isinstance(self.hooks, Rehearsal), "steps": [],
                      "run": uuid.uuid4().hex[:8], "show": cue["show"], "via": getattr(self.hooks, "via", "none"),
                      "device": getattr(self.hooks, "device", "")}
        self.thread = threading.Thread(target=self._run, args=(cue,), daemon=True)
        self.thread.start()
        return self.status()

    def stop(self) -> dict:
        """End the show early: the music comes back as it would at the show's end."""
        self.stop_flag.set()
        return self.status()

    def _phase(self, name: str, dur: float = 0.0) -> None:
        self.state.update(phase=name, t0=self.clock(), dur=dur)
        self.state["steps"] = self.state.get("steps", []) + [name]

    def _run(self, cue: dict) -> None:
        h = self.hooks
        try:
            self._phase("pause")
            h.pause_music()
            dur = h.play_show(cue["show"])
            self._phase("show", dur)
            end = self.clock() + dur
            while self.clock() < end and not self.stop_flag.is_set():
                self.sleep(min(0.1, max(0.0, end - self.clock())))
            if self.stop_flag.is_set():
                h.stop_show()
            self._phase("resume")
            h.resume_music()
            self._phase("done")
        except Exception as e:  # noqa: BLE001 - the music must come back whatever went wrong
            self.state["error"] = f"{type(e).__name__}: {e}"
            try:
                h.resume_music()
            except Exception:  # noqa: BLE001
                pass
            self._phase("done")
