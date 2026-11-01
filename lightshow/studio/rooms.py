"""Rooms: which lights and speakers stand where, and which rooms the party is in right now.

A room switched off (karaoke in the living room while beer pong goes on in the game room) leaves
the show: its lights fade back over FADE_S seconds to how they were before the show took them, and
its speakers go muted at once. Switched on again, the lights fade back in and the speakers unmute.

The speakers are never faded: a Google speaker chimes at every volume change, so a fade was a run of
beeps. Muting and unmuting is one change each. A speaker's own level is kept as it is: the Party
Hub's slider still shows, and still sets, the level it plays at once the room is back (a level set
while muted is only kept, as setting a level unmutes a Google speaker). The levels of the speakers a
room muted are kept in config.json (room_muted), so only those are ever unmuted, and a studio that
quits unmutes them on the way out.
"""

from __future__ import annotations

import re
import threading
import time
import uuid

FADE_S = 5.0                                        # the lights' fade; speakers mute and unmute at once
# What may be running on a speaker the studio touches: nothing, the backdrop, or the Default Media Receiver
# (the studio's own stream plays in it). Anything else (Spotify, YouTube...) is someone else casting.
OURS_OR_IDLE = {None, "", "E8C28D3C", "CC1AD845"}


def clean(rooms) -> list[dict]:
    """Rooms as saved: a name, on or off, and their lights (by MAC or device id) and speakers (by
    name). A light or a speaker stands in one room only: the first that claims it keeps it. "map": false
    leaves a room off the house map (a room away from the party, say an office with a test strip)."""
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
        out.append({"id": rid, "name": name, "on": bool(r.get("on", True)), "lights": lights, "speakers": speakers,
                    **({} if r.get("map", True) else {"map": False})})
    return out


class Rooms:
    def __init__(self, load_config, save_config, cast=None, touch: bool = True):
        self.load_config, self.save_config = load_config, save_config
        self.cast = cast                                 # the CastManager: finds speakers by name
        self.touch = touch                               # False on a test copy: never touches a speaker
        self.lock = threading.Lock()
        self.gen: dict[str, int] = {}                    # speaker -> the change it is on (a newer one ends an older)
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

    # -- whose speakers they are -----------------------------------------------------------------------
    # The studio only ever changes a speaker's volume or mute while it is casting (to it, or to a group it is
    # in: a whole-house group is all of them), and never one someone else is casting to: someone else in the house
    # was casting to the office speaker, the jam played on this PC, and switching the Office on in the Party Hub unmuted
    # it under her. When the studio lets go of the speakers, every one it muted plays again (Rooms.unmute_all,
    # from CastManager.disconnect), so none is left muted for whoever casts next.
    def casting(self) -> bool:
        return self.cast is not None and getattr(self.cast, "cast", None) is not None

    @staticmethod
    def foreign(c) -> bool:
        """Is someone else casting to this speaker (another app running on it)?"""
        st = getattr(c, "status", None)
        return st is not None and getattr(st, "app_id", None) not in OURS_OR_IDLE

    def sync_speakers(self, wait: bool = False) -> list[threading.Thread]:
        """Mute the speakers of rooms that are off, and unmute those of rooms that are on again: while the
        studio is casting only (see casting)."""
        if not self.touch or self.cast is None or not self.casting():
            return []
        want = {s for r in self.list() if not r["on"] for s in r["speakers"]}
        have = set(self.muted())
        threads = []
        for name in sorted(want | have):
            g = self.gen.get(name, 0)                    # > 0 muting, < 0 unmuting, 0 resting
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
        self.gen[name] = g if out else -g                # the sign says which way it is going
        t = threading.Thread(target=self._switch, args=(name, out, g), daemon=True)
        t.start()
        return t

    def _current(self, name: str, g: int) -> bool:
        return abs(self.gen.get(name, 0)) == g

    def _switch(self, name: str, out: bool, g: int) -> None:
        try:
            c = self.cast._device(name)
            if c is None:
                raise LookupError("not on the network")
            saved = self.muted().get(name)
            if out and (self.foreign(c) or not self.casting()):
                return                                   # someone else plays there, or the studio stopped casting
            if out:
                live = float(c.status.volume_level) if c.status is not None else None
                level = saved if saved is not None else live
                if level is None:
                    raise ConnectionError("not answering")
                self._store(name, level)                 # kept first: the Party Hub shows it from now on
                if self._current(name, g):
                    c.set_volume_muted(True)             # one change: one chime at most, its level untouched
            else:
                if saved is None:
                    return
                live = float(c.status.volume_level) if c.status is not None else None
                if live is not None and abs(live - saved) < 0.005:
                    c.set_volume_muted(False)            # its level is still the one it plays at
                else:
                    c.set_volume(saved)                  # a level set while muted: setting it unmutes it too
                    time.sleep(0.3)
                    if c.status is not None and c.status.volume_muted:
                        c.set_volume_muted(False)
                self._store(name, None)
            self.errors.pop(name, None)
        except Exception as e:  # noqa: BLE001 - one speaker that does not answer must not stop the rest
            self.errors[name] = f"{type(e).__name__}: {e}"
            if out and self._current(name, g):
                self._store(name, None)                  # not muted after all: tried again next time
        finally:
            if self._current(name, g):
                self.gen[name] = 0

    # -- the levels the party plays at ----------------------------------------------------------------
    # Every level is kept (config.json speaker_levels): set on the Party Hub, or changed in person (on the
    # speaker, or in the Google Home app). The person in the room wins: while the jam plays, a speaker found
    # at another level, still there at the next look, has that level kept as its own, and one muted by hand
    # stays muted; the studio never turns it back (the user). Only a change the studio itself sent in the
    # last SENT_S seconds that the speaker missed is sent again, once (a Google speaker chimes per change).
    SENT_S = 15.0

    def levels(self) -> dict[str, float]:
        return dict(self.load_config().get("speaker_levels", {}))

    def remember_level(self, name: str, level: float, sent: bool = False) -> None:
        """Keep a speaker's level. sent: the studio is setting the speaker to it now (the Party Hub), so for a
        little while a speaker found elsewhere missed it, rather than someone changing it."""
        with self.lock:
            cfg = self.load_config()
            cfg.setdefault("speaker_levels", {})[str(name)] = round(max(0.0, min(1.0, float(level))), 3)
            self.save_config(cfg)
        if sent:
            self.__dict__.setdefault("_sent_at", {})[str(name)] = self.clock()

    clock = staticmethod(time.monotonic)

    def keep_as_they_are(self, wait_s: float = 1.5, sleep=time.sleep) -> None:
        """A group's volume was moved on purpose (the Party Hub's cast volume, the jam casting to "Everywhere"):
        each speaker in it moved with it. Once they have settled, the levels they are at are the ones kept (it
        set them all back 5 s later, as if someone had turned them down)."""
        if not self.touch or self.cast is None or not self.casting():
            return
        sleep(wait_s)
        off = self.__dict__.setdefault("_off_seen", {})
        for name in list(self.levels()):
            try:
                c = self.cast._device(name)
                if c is None or c.status is None or getattr(c.cast_info, "cast_type", "") == "group":
                    continue
                self.remember_level(name, float(c.status.volume_level))
                off[name] = 0
            except Exception:  # noqa: BLE001 - a speaker that does not answer keeps its kept level
                continue

    def keep_levels(self, names, clock=None) -> list[str]:
        """Look at each of these speakers once. One found off its kept level twice running: changed in person,
        so its level is kept from now on (muted by hand: left muted); or, just after the studio set it, it missed
        that: sent again, once. Returns what it did. Only while the studio is casting, and never a speaker
        someone else is casting to."""
        if not self.touch or self.cast is None or not self.casting():
            return []
        clock = clock or self.clock
        kept, muted, did = self.levels(), self.muted(), []
        off = self.__dict__.setdefault("_off_seen", {})
        last = self.__dict__.setdefault("_set_at", {})
        sent = self.__dict__.setdefault("_sent_at", {})
        for name in names:
            if name in muted or self.gen.get(name, 0):
                continue                                         # its room is off, or a mute is on its way
            try:
                c = self.cast._device(name)
                if c is None or c.status is None or getattr(c.cast_info, "cast_type", "") == "group" or self.foreign(c):
                    continue
                live = float(c.status.volume_level)               # (muted by hand: left as it is)
            except Exception:  # noqa: BLE001 - a speaker that does not answer is looked at next time
                continue
            if name not in kept:                                 # first seen: its level now is the one kept
                self.remember_level(name, live)
                kept[name] = live
                continue
            moved = abs(live - kept[name]) > 0.02
            off[name] = off.get(name, 0) + 1 if moved else 0
            if off[name] < 2:
                continue                                         # (once could be in passing: a look later)
            if clock() - sent.get(name, -1e9) < self.SENT_S:
                # the studio set it a moment ago and the speaker did not take it: sent again, once
                if clock() - last.get(name, -1e9) < 10.0:
                    continue
                try:
                    c.set_volume(kept[name])
                    did.append(f"{name}: missed {kept[name]:.2f}, sent again")
                    last[name], off[name] = clock(), 0
                    sent.pop(name, None)
                except Exception:  # noqa: BLE001
                    pass
                continue
            self.remember_level(name, live)                      # changed in person: that is its level now
            did.append(f"{name}: {kept[name]:.2f} -> {live:.2f} on the speaker, kept")
            kept[name], off[name] = live, 0
        return did

    def unmute_all(self) -> None:
        """The studio is quitting: every speaker a room muted plays again at its own level, at once."""
        if not self.touch or self.cast is None:
            return
        for name, level in self.muted().items():
            self.gen[name] = abs(self.gen.get(name, 0)) + 1          # ends any change still on its way
            try:
                c = self.cast._device(name)
                if c is not None:
                    c.set_volume(level)
                    c.set_volume_muted(False)
                    self._store(name, None)
            except Exception:  # noqa: BLE001
                pass
