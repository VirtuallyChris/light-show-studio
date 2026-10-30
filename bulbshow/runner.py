"""One running show: a speaker playing the sound on a loop, and a bulb following it.

The speaker is handed an endless stream (sound.LoopReader) and reports how far into it it is. The
show's moment is that position, wrapped round the sound's length, less the speaker's own delay (the
Cast offset the studio measured for it, from the studio's config), plus the show's light timing.
The bulb is given the color of that moment fifty times a second; the bulb itself decides what is
worth sending (a cloud bulb only sends changes, within Govee's allowance).

Several runners run at once, one per show, each with its own speaker and bulb.

A preview on this PC (`here`) is the same with no speaker: the editor plays the sound itself and the runner
keeps time with a clock that starts when the bulb is ready (the page starts the sound on that clock).
"""

from __future__ import annotations

import threading
import time
import uuid

from . import effects
from .sound import LoopReader, Sound
from .speakers import FakeCast


class ShowRunner:
    TICK = 0.02
    RECONNECT_AFTER_S = 3.0        # a speaker that had played less of its stream than this was only starting it

    def __init__(self, show_id: str, show: dict, sound: Sound, speakers, bulb, url_for, cast_offset_s: float = 0.0,
                 start_at: float = 0.0, clock=time.monotonic, log=print, here: bool = False):
        self.id, self.show, self.sound = show_id, show, sound
        self.speakers, self.bulb, self.url_for = speakers, bulb, url_for
        self.cast_offset = float(cast_offset_s)
        self.start_at = float(start_at)
        self.clock, self.log = clock, log
        self.cast = None
        self.sid = ""
        self.reader = None
        self.base = 0.0              # where in the loop the current stream begins
        self.loop_t = None           # where in the loop the light is (the room plus the light's lead)
        self.heard_t = None          # where in the loop the sound is
        self.here = here             # a preview: the sound plays in the editor, not on a speaker
        self.state = "starting"
        self.error = ""
        self._stop = threading.Event()
        self.thread = None
        self.lock = threading.Lock()

    @property
    def speaker(self) -> str:
        return "this PC" if self.here else self.show.get("speaker", "")

    @property
    def light_lead(self) -> float:
        return float(self.show.get("light_ms") or 0) / 1000.0

    @property
    def cloud(self) -> bool:
        return getattr(self.bulb, "via", "") == "cloud"

    # -- life ----------------------------------------------------------------------------
    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"show {self.id}", daemon=True)
        self.thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        if wait and self.thread is not None:
            self.thread.join(timeout=20)

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def update_show(self, show: dict) -> None:
        """Edits made while the show runs play on the next tick (a new sound needs a restart)."""
        self.show = show
        if self.reader is not None:
            self.reader.gain = self.volume

    @property
    def volume(self) -> float:
        v = self.show.get("volume")
        return 1.0 if v is None else max(0.0, min(1.0, float(v)))

    # -- the stream ------------------------------------------------------------------------
    def _new_stream(self, at: float) -> None:
        with self.lock:
            self.sid = uuid.uuid4().hex[:12]
            self.base = at % self.sound.duration
            self.reader = LoopReader(self.sound, self.base, self.volume)
        self.cast.play_stream(self.url_for(self.sid), "audio/wav", self.show.get("name") or "Bulb Show")

    def reader_for(self, sid: str):
        """The stream a speaker asks for. A second request for a stream it had already been playing is
        a speaker that lost its connection (Wi-Fi): it gets a fresh stream from where the room was, so its
        clock and the lights stay together. Returns the reader, or None (asked again: being restarted)."""
        with self.lock:
            if sid != self.sid or self.reader is None:
                return None
            if self.reader.sent / self.sound.sr < self.RECONNECT_AFTER_S:
                self.reader = LoopReader(self.sound, self.base, self.volume)   # (a probe: start it over)
                return self.reader
        self.log(f"{self.show.get('name')}: {self.speaker} lost its stream and came back; restarting it where the room was")
        threading.Thread(target=self._restart_stream, daemon=True).start()
        return None

    def _restart_stream(self) -> None:
        if self._stop.is_set() or self.cast is None:
            return
        try:
            self._new_stream(self.loop_t if self.loop_t is not None else self.base)
        except Exception as e:  # noqa: BLE001
            self.error = f"{self.speaker}: {e}"

    # -- the clock -------------------------------------------------------------------------
    def moment(self, position: float) -> float | None:
        """Where in the loop the room is, for a speaker `position` seconds into its stream (None before
        the first sound has reached the room)."""
        heard = position - self.cast_offset
        if heard < 0:
            return None
        lead = self.light_lead + float(getattr(self.bulb, "lead", 0.0))      # (a cloud bulb's own delay too)
        return (self.base + heard + lead) % self.sound.duration

    def _run(self) -> None:
        idle_since, played = None, False
        try:
            self.state = "connecting"
            self.bulb.begin()
            if self._stop.is_set():
                return
            self.cast = FakeCast(self.speaker, self.clock) if self.here else self.speakers.open(self.speaker)
            if self._stop.is_set():
                return
            self._new_stream(self.start_at)
            self.state = "buffering"
            while not self._stop.wait(self.TICK):
                st = self.cast.status()
                state = str(st.get("state") or "")
                now = self.clock()
                if state == "PLAYING":
                    idle_since, played = None, True
                    pos = float(st.get("position") or 0)
                    t = self.moment(pos)
                    if t is not None:
                        self.heard_t = (self.base + pos - self.cast_offset) % self.sound.duration
                        self.loop_t = t
                        self.bulb.apply(effects.state(self.show, t, cloud=self.cloud))
                        self.state = "playing"
                elif state in ("BUFFERING", "PAUSED"):
                    idle_since = None
                    self.state = state.lower()
                else:
                    idle_since = idle_since or now
                    if now - idle_since > (10 if played else 30):
                        raise RuntimeError(f"{self.speaker} stopped playing (another app may have taken it over)")
                self.error = self.bulb.error           # (a cloud hiccup clears once a command gets through)
        except Exception as e:  # noqa: BLE001 - one show failing never stops the others
            self.error = str(e) or e.__class__.__name__
            self.log(f"{self.show.get('name')}: {self.error}")
        finally:
            self.state = "error" if self.error and not self._stop.is_set() else "stopped"
            cast, self.cast = self.cast, None
            if cast is not None:
                try:
                    cast.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.bulb.end()
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict:
        color = getattr(self.bulb, "color", None)
        return {"id": self.id, "state": self.state, "error": self.error, "position": self.loop_t,
                "speaker": self.speaker, "via": getattr(self.bulb, "via", ""), "heard": self.heard_t, "here": self.here,
                "color": effects.code(color) if color else None, "sent": getattr(self.bulb, "sent", 0)}
