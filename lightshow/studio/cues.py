"""Cues: buttons on the Party Hub that stop the party for a moment (a doorbell, shot o'clock).

A cue fades the music out (if set), pauses Spotify, plays one of your saved shows (its sound on the
speakers, its lights in the room), then resumes Spotify and fades the music back in (if set). In a
jam the room is about 90 s behind Spotify; pausing Spotify pauses the room at once and holds the
buffer, so the room picks up exactly where it stopped.

For now every cue runs as a rehearsal: each step happens on its real timing and the Party Hub
follows along, but no speaker, light or Spotify is touched. Playing the show on the jam's speakers
and handing them back afterwards is the part still to be wired (and tried on the real speakers).
"""

from __future__ import annotations

import re
import threading
import time
import uuid

STEPS_PER_S = 10                                   # volume steps a second during a fade

DEFAULT_CUES = [
    {"id": "doorbell", "name": "Doorbell", "show": "", "fade_out": {"on": True, "s": 1.5}, "fade_in": {"on": True, "s": 3.0}},
    {"id": "shot", "name": "Shot o'clock", "show": "", "fade_out": {"on": True, "s": 2.0}, "fade_in": {"on": True, "s": 4.0}},
]


def _fade(v) -> dict:
    v = v if isinstance(v, dict) else {}
    try:
        s = float(v.get("s", 2.0))
    except (TypeError, ValueError):
        s = 2.0
    return {"on": bool(v.get("on", True)), "s": round(max(0.0, min(30.0, s)), 2)}


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
                    "show": str(c.get("show") or "")[:120], "fade_out": _fade(c.get("fade_out")), "fade_in": _fade(c.get("fade_in"))})
    return out[:12]


class Rehearsal:
    """Stands in for the speakers, Spotify and the show: remembers what would have been done."""

    def __init__(self, show_length):
        self.show_length = show_length                  # show name -> seconds
        self.level = 0.4
        self.calls: list = []

    def volume(self) -> float:
        return self.level

    def set_volume(self, v: float) -> None:
        self.level = v

    def pause_music(self) -> None:
        self.calls.append("pause")

    def resume_music(self) -> None:
        self.calls.append("resume")

    def play_show(self, name: str) -> float:
        self.calls.append(("show", name))
        return float(self.show_length(name) or 0.0)

    def stop_show(self) -> None:
        self.calls.append("stop show")


class CueRunner:
    def __init__(self, load_config, save_config, show_length, clock=time.monotonic, sleep=time.sleep):
        self.load_config, self.save_config = load_config, save_config
        self.show_length = show_length
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
        self.hooks = hooks or Rehearsal(self.show_length)
        self.stop_flag.clear()
        self.state = {"id": cue["id"], "name": cue["name"], "phase": "starting", "rehearsal": isinstance(self.hooks, Rehearsal), "steps": []}
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

    def _ramp(self, a: float, b: float, secs: float, watch_stop: bool) -> None:
        n = max(1, int(secs * STEPS_PER_S))
        for i in range(1, n + 1):
            if watch_stop and self.stop_flag.is_set():
                return
            k = i / n
            self.hooks.set_volume(a + (b - a) * k * k * (3 - 2 * k))
            self.sleep(secs / n)

    def _run(self, cue: dict) -> None:
        h = self.hooks
        level = h.volume()
        try:
            fo, fi = cue["fade_out"], cue["fade_in"]
            if fo["on"] and fo["s"] > 0:
                self._phase("fade_out", fo["s"])
                self._ramp(level, 0.0, fo["s"], watch_stop=False)
            self._phase("pause")
            h.pause_music()
            h.set_volume(level)                          # the show plays at the party's own level
            dur = h.play_show(cue["show"])
            self._phase("show", dur)
            end = self.clock() + dur
            while self.clock() < end and not self.stop_flag.is_set():
                self.sleep(min(0.1, max(0.0, end - self.clock())))
            if self.stop_flag.is_set():
                h.stop_show()
            if fi["on"] and fi["s"] > 0:
                h.set_volume(0.0)
            self._phase("resume")
            h.resume_music()
            if fi["on"] and fi["s"] > 0:
                self._phase("fade_in", fi["s"])
                self._ramp(0.0, level, fi["s"], watch_stop=False)
            h.set_volume(level)
            self._phase("done")
        except Exception as e:  # noqa: BLE001 - the music must come back whatever went wrong
            self.state["error"] = f"{type(e).__name__}: {e}"
            try:
                h.resume_music()
                h.set_volume(level)
            except Exception:  # noqa: BLE001
                pass
            self._phase("done")
