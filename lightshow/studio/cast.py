"""Google Cast playback for the studio.

Plays a project's audio on a Cast device or speaker group (the studio serves
the file over HTTP) and reports the speakers' playback position so the light
show can follow what is actually being heard. Cast adds a second or two of
buffering and its own clock, so the position comes from the device's status
messages, extrapolated between them, rather than from the browser.
"""

from __future__ import annotations

import os
import threading
import time

from . import audiotrim

CONTENT_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
                 ".m4a": "audio/mp4", ".aiff": "audio/aiff", ".aif": "audio/aiff", ".opus": "audio/ogg"}


class CastManager:
    def __init__(self, interface_ip: str | None):
        self.interface_ip = interface_ip
        self.lock = threading.Lock()
        self.zc = None
        self.browser = None
        self.casts: list = []
        self.cast = None
        self.name = ""
        self.last_time = 0.0       # position reported by the device
        self.last_at = 0.0         # monotonic time of that report
        self.state = "IDLE"
        self.duration = 0.0
        self.media_url = ""
        self._poller = None
        self._stop_poll = threading.Event()
        self.base = 0.0            # where in the song the current stream begins
        self.media_path = ""       # local file behind the stream, for reloading at a new point
        self.media_root = ""       # stream URL without its start parameter
        self.title = ""

    # -- discovery -----------------------------------------------------------------
    def discover(self, timeout: float = 5.0) -> list[dict]:
        import pychromecast
        import zeroconf

        with self.lock:
            if self.browser is not None:
                self.browser.stop_discovery()
            if self.zc is None:
                self.zc = zeroconf.Zeroconf(interfaces=[self.interface_ip]) if self.interface_ip else zeroconf.Zeroconf()
            casts, self.browser = pychromecast.get_chromecasts(timeout=timeout, zeroconf_instance=self.zc)
            self.casts = list(casts)
        out = []
        for c in self.casts:
            i = c.cast_info
            out.append({"name": i.friendly_name, "model": i.model_name, "type": i.cast_type, "host": i.host})
        out.sort(key=lambda d: (d["type"] != "group", d["name"].lower()))
        return out

    # -- connection ------------------------------------------------------------------
    def connect(self, name: str) -> dict:
        if not self.casts:
            self.discover()
        target = next((c for c in self.casts if c.cast_info.friendly_name.lower() == name.lower()), None)
        if target is None:
            raise LookupError(f"no Cast device named {name!r}")
        self.disconnect()
        target.wait(timeout=10)
        self.cast = target
        self.name = target.cast_info.friendly_name
        mc = target.media_controller
        mc.register_status_listener(self)
        self._stop_poll.clear()
        self._poller = threading.Thread(target=self._poll, daemon=True)
        self._poller.start()
        return self.status()

    def disconnect(self) -> None:
        self._stop_poll.set()
        cast, self.cast = self.cast, None     # detach first so status() and the poller see it gone
        if cast is not None:
            try:
                cast.media_controller.stop()
                time.sleep(0.3)                # let the stop reach the speaker before dropping the socket
            except Exception:  # noqa: BLE001
                pass
            try:
                cast.disconnect(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        self.name = ""
        self.state = "IDLE"
        self.media_url = self.media_path = self.media_root = ""
        self.base = 0.0

    # -- pychromecast status listener --------------------------------------------------
    def new_media_status(self, status) -> None:
        # Times reported by the device count from the start of the stream it was
        # given, which begins at self.base seconds into the song.
        state = status.player_state or "IDLE"
        when = float(status.current_time or 0.0)
        if state in ("IDLE", "UNKNOWN") and when <= 0.01:
            # The session ended; it did not rewind. Keep the position so playback
            # can be picked up where it stopped.
            self.state = state
            return
        self.last_time = when
        self.last_at = time.monotonic()
        self.state = state
        if not self.duration and status.duration:
            self.duration = float(status.duration) + self.base

    def load_media_failed(self, item, error_code) -> None:
        self.state = f"ERROR {error_code}"

    def _poll(self) -> None:
        """Ask the device for a status message every second so drift stays small."""
        while not self._stop_poll.wait(1.0):
            cast = self.cast
            if cast is None:
                break
            try:
                cast.media_controller.update_status()
            except Exception:  # noqa: BLE001
                pass

    # -- transport ---------------------------------------------------------------------
    def play(self, url: str, path: str, title: str, start: float = 0.0) -> dict:
        if self.cast is None:
            raise RuntimeError("not connected to a Cast device")
        self.media_root, self.media_path, self.title = url, path, title
        self.duration = audiotrim.total_duration(path)
        return self._load(start)

    def _load(self, start: float) -> dict:
        """Hand the speaker a stream that already begins where we want it.

        The speaker is never asked to seek: it plays what it is given from the
        top. That makes its delay a fixed property of the speaker rather than
        something that changes with every start position.
        """
        if self.cast is None:
            raise RuntimeError("not connected to a Cast device")
        exact = audiotrim.exact_start(self.media_path, max(0.0, start))
        sep = "&" if "?" in self.media_root else "?"
        url = self.media_root if exact <= 0 else f"{self.media_root}{sep}start={exact:.4f}"
        ctype = CONTENT_TYPES.get(os.path.splitext(self.media_path)[1].lower(), "audio/mpeg")
        self.media_url, self.base = url, exact
        mc = self.cast.media_controller
        mc.play_media(url, ctype, title=self.title, autoplay=True)
        mc.block_until_active(timeout=15)
        self.last_time, self.last_at, self.state = 0.0, time.monotonic(), "BUFFERING"
        return self.status()

    def pause(self) -> dict:
        if self.cast is not None:
            self.cast.media_controller.pause()
        return self.status()

    def resume(self) -> dict:
        if self.cast is None:
            return self.status()
        # A session the speaker dropped while paused cannot be resumed, only reloaded.
        if self.state not in ("PLAYING", "PAUSED", "BUFFERING") and self.media_path:
            return self._load(self.base + self.last_time)
        self.cast.media_controller.play()
        return self.status()

    def seek(self, t: float) -> dict:
        """Start a new stream at t rather than asking the speaker to find it."""
        t = max(0.0, t)
        if self.cast is None or not self.media_path:
            self.last_time, self.last_at, self.base = 0.0, time.monotonic(), t
            return self.status()
        return self._load(t)

    def stop(self) -> dict:
        if self.cast is not None:
            try:
                self.cast.media_controller.stop()
            except Exception:  # noqa: BLE001
                pass
        self.state = "IDLE"
        self.base, self.last_time, self.last_at = 0.0, 0.0, time.monotonic()
        return self.status()

    def set_volume(self, level: float) -> dict:
        if self.cast is not None:
            self.cast.set_volume(max(0.0, min(1.0, level)))
        return self.status()

    def status(self) -> dict:
        playing = self.state == "PLAYING"
        pos = self.base + self.last_time + (time.monotonic() - self.last_at if playing else 0.0)
        vol = None
        if self.cast is not None and self.cast.status is not None:
            vol = round(float(self.cast.status.volume_level), 2)
        return {"connected": self.cast is not None, "device": self.name, "state": self.state, "position": round(pos, 3),
                "duration": self.duration, "volume": vol, "media_url": self.media_url}
