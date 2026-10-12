"""Rooms: which lights and speakers stand where, and which rooms the party is in right now.

A room switched off (karaoke in the living room while beer pong goes on in the game room) leaves
the show: its lights fade back to how they were before the show took them, and its speakers fade
out and go muted. Switched on again, both fade back in. Each fade takes FADE_S seconds.

A speaker's own level is kept: it fades to nothing and goes muted (it is not set back to its level
while muted, because setting a level unmutes a Google speaker), and the Party Hub's slider still
shows, and still sets, the level it plays at once the room is back, when it fades up to it. The levels of the speakers a room muted are kept in config.json
(room_muted), so only those are ever unmuted, and a studio that quits unmutes them on the way out.
"""

from __future__ import annotations

import re
import threading
import time
import uuid

FADE_S = 5.0
STEPS_PER_S = 5                                     # volume steps a second during a fade


def clean(rooms) -> list[dict]:
    """Rooms as saved: a name, on or off, and their lights (by MAC or device id) and speakers (by
    name). A light or a speaker stands in one room only: the first that claims it keeps it."""
    out, lights_seen, speakers_seen, ids = [], set(), set(), set()
    for r in rooms if isinstance(rooms, list) else []:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", rid) or rid in ids:
            rid = uuid.uuid4().hex[:10]
        ids.add(rid)
        lights = [str(k) for k in r.get("lights") or [] if k and str(k) not in lights_seen]
        speakers = [str(s) for s in r.get("speakers") or [] if s and str(s) not in speakers_seen]
        lights_seen |= set(lights)
        speakers_seen |= set(speakers)
        name = str(r.get("name") or "").strip()[:40] or f"Room {len(out) + 1}"
        out.append({"id": rid, "name": name, "on": bool(r.get("on", True)), "lights": lights, "speakers": speakers})
    return out


def _ease(k: float) -> float:
    k = max(0.0, min(1.0, k))
    return k * k * (3 - 2 * k)


class Rooms:
    def __init__(self, load_config, save_config, cast=None, touch: bool = True, fade_s: float = FADE_S):
        self.load_config, self.save_config = load_config, save_config
        self.cast = cast                                 # the CastManager: finds speakers by name
        self.touch = touch                               # False on a test copy: never touches a speaker
        self.fade_s = fade_s
        self.lock = threading.Lock()
        self.gen: dict[str, int] = {}                    # speaker -> the fade it is on (a newer one ends an older)
        self.errors: dict[str, str] = {}

    # -- the rooms --------------------------------------------------------------------------------
    def list(self) -> list[dict]:
        return clean(self.load_config().get("rooms", []))

    def replace(self, rooms) -> list[dict]:
        with self.lock:
            cfg = self.load_config()
            cfg["rooms"] = clean(rooms)
            self.save_config(cfg)
            return cfg["rooms"]

    def set_on(self, rid: str, on: bool) -> list[dict]:
        with self.lock:
            cfg = self.load_config()
            rooms = clean(cfg.get("rooms", []))
            for r in rooms:
                if r["id"] == rid:
                    r["on"] = bool(on)
            cfg["rooms"] = rooms
            self.save_config(cfg)
            return rooms

    def off_light_keys(self) -> set[str]:
        return {k for r in self.list() if not r["on"] for k in r["lights"]}

    def room_of_light(self, keys) -> dict | None:
        keys = {k for k in keys if k}
        return next((r for r in self.list() if keys & set(r["lights"])), None)

    # -- the speakers -------------------------------------------------------------------------------
    def muted(self) -> dict[str, float]:
        """The speakers a room has muted, with the level each is set to (and plays at once back)."""
        return dict(self.load_config().get("room_muted", {}))

    def _store(self, name: str, level: float | None) -> None:
        with self.lock:
            cfg = self.load_config()
            m = cfg.setdefault("room_muted", {})
            if level is None:
                m.pop(name, None)
            else:
                m[name] = round(float(level), 3)
            self.save_config(cfg)

    def set_saved_volume(self, name: str, level: float) -> bool:
        """A volume set for a speaker its room has muted: kept for when the room is back. Not sent to the
        speaker now: setting a level unmutes a Google speaker. False if no room has it muted."""
        if name not in self.muted():
            return False
        self._store(name, max(0.0, min(1.0, float(level))))
        return True

    def sync_speakers(self, wait: bool = False) -> list[threading.Thread]:
        """Fade out the speakers of rooms that are off, and back in those of rooms that are on again."""
        if not self.touch or self.cast is None:
            return []
        want = {s for r in self.list() if not r["on"] for s in r["speakers"]}
        have = set(self.muted())
        threads = []
        for name in sorted(want | have):
            g = self.gen.get(name, 0)                    # > 0 fading out, < 0 fading in, 0 resting
            if name in want and not (g > 0 or (g == 0 and name in have)):
                threads.append(self._start(name, out=True))
            elif name not in want and name in have and not g < 0:
                threads.append(self._start(name, out=False))
        if wait:
            for t in threads:
                t.join()
        return threads

    def _start(self, name: str, out: bool) -> threading.Thread:
        g = abs(self.gen.get(name, 0)) + 1
        self.gen[name] = g if out else -g                # the sign says which way it is fading
        t = threading.Thread(target=self._fade, args=(name, out, g), daemon=True)
        t.start()
        return t

    def _current(self, name: str, g: int) -> bool:
        return abs(self.gen.get(name, 0)) == g

    def _fade(self, name: str, out: bool, g: int) -> None:
        try:
            c = self.cast._device(name)
            if c is None:
                raise LookupError("not on the network")
            saved = self.muted().get(name)
            if out:
                live = float(c.status.volume_level) if c.status is not None else None
                level = saved if saved is not None else live
                if level is None:
                    raise ConnectionError("not answering")
                self._store(name, level)                 # kept first: the Party Hub shows it from now on
                start = live if live is not None else level
                t0 = time.monotonic()                    # by the clock: each change takes the speaker a moment
                while True:
                    if not self._current(name, g):
                        return
                    k = min(1.0, (time.monotonic() - t0) / max(1e-3, self.fade_s))
                    c.set_volume(start * (1 - _ease(k)))
                    if k >= 1.0:
                        break
                    time.sleep(1.0 / STEPS_PER_S)
                # Muted at nothing. (Setting the level again would unmute a Google speaker: tried.) Its own
                # level is kept here, shown on the Party Hub and set again when the room is back.
                c.set_volume_muted(True)
            else:
                if saved is None:
                    return
                c.set_volume(0.0)
                time.sleep(0.2)
                c.set_volume_muted(False)
                t0 = time.monotonic()
                while True:
                    if not self._current(name, g):
                        return
                    k = min(1.0, (time.monotonic() - t0) / max(1e-3, self.fade_s))
                    c.set_volume(saved * _ease(k))
                    if k >= 1.0:
                        break
                    time.sleep(1.0 / STEPS_PER_S)
                c.set_volume(saved)
                self._store(name, None)
            self.errors.pop(name, None)
        except Exception as e:  # noqa: BLE001 - one speaker that does not answer must not stop the rest
            self.errors[name] = f"{type(e).__name__}: {e}"
            if out and self._current(name, g):
                self._store(name, None)                  # not muted after all: tried again next time
        finally:
            if self._current(name, g):
                self.gen[name] = 0

    def unmute_all(self) -> None:
        """The studio is quitting: every speaker a room muted plays again at its own level, at once."""
        if not self.touch or self.cast is None:
            return
        for name, level in self.muted().items():
            self.gen[name] = abs(self.gen.get(name, 0)) + 1          # ends any fade still running
            try:
                c = self.cast._device(name)
                if c is not None:
                    c.set_volume(level)
                    c.set_volume_muted(False)
                    self._store(name, None)
            except Exception:  # noqa: BLE001
                pass
