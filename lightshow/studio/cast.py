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
        self.on_release = None     # called when the studio lets go of the speakers (Rooms.unmute_all)

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
    @staticmethod
    def _answered(c) -> bool:
        sc = getattr(c, "socket_client", None)
        return c.status is not None and bool(getattr(sc, "is_connected", True))

    def connect(self, name: str) -> dict:
        if not self.casts:
            self.discover()
        target = next((c for c in self.casts if c.cast_info.friendly_name.lower() == name.lower()), None)
        if target is None:
            raise LookupError(f"no Cast device named {name!r}")
        self.disconnect()
        target.wait(timeout=10)
        if not self._answered(target):
            # A group lives on whichever of its speakers leads it, and that can change (a speaker
            # rejoining Wi-Fi); the address found earlier may be stale. Look again, once.
            try:
                target.disconnect(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self.discover()
            target = next((c for c in self.casts if c.cast_info.friendly_name.lower() == name.lower()), None)
            if target is None:
                raise LookupError(f"{name} is not on the network right now (a speaker in it may be off or rejoining Wi-Fi)")
            target.wait(timeout=10)
            if not self._answered(target):
                try:
                    target.disconnect(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
                raise ConnectionError(f"{name} is not answering (a speaker in it may be off or on weak Wi-Fi)")
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
        if cast is not None and callable(self.on_release):      # the speakers are no longer the studio's
            threading.Thread(target=self.on_release, daemon=True).start()

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

    def play_stream(self, url: str, content_type: str, title: str) -> dict:
        """Play an endless live stream (Jam mode's relay) rather than a file.

        There is nothing to seek in and no length; the position the speaker
        reports is simply how much of the stream it has played.
        """
        if self.cast is None:
            raise RuntimeError("not connected to a Cast device")
        self.media_root = self.media_path = ""
        self.media_url, self.title, self.base, self.duration = url, title, 0.0, 0.0
        mc = self.cast.media_controller
        mc.play_media(url, content_type, title=title, stream_type="LIVE", autoplay=True)
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

    # -- every speaker's own volume (the Party Hub's settings) ----------------------------------
    def _device(self, name: str):
        if not self.casts:
            self.discover()
        c = next((c for c in self.casts if c.cast_info.friendly_name == name), None)
        if c is not None and not self._answered(c):
            c.wait(timeout=3)                            # a socket of its own; reading a volume plays nothing
        return c

    def device_volumes(self) -> list[dict]:
        """Every speaker and group on the network with its volume (0..1), groups first."""
        if not self.casts:
            self.discover()
        out = []
        for c in list(self.casts):
            i = c.cast_info
            vol = None
            try:
                if not self._answered(c):
                    c.wait(timeout=3)
                if c.status is not None:
                    vol = round(float(c.status.volume_level), 2)
            except Exception:  # noqa: BLE001 - a speaker that does not answer just shows no level
                pass
            out.append({"name": i.friendly_name, "type": i.cast_type, "model": i.model_name, "volume": vol})
        out.sort(key=lambda d: (d["type"] != "group", d["name"].lower()))
        return out

    def is_group(self, name: str | None) -> bool:
        """Is this a speaker group (by name; None: the one being cast to)?"""
        try:
            c = self._device(name) if name else self.cast
            return c is not None and getattr(c.cast_info, "cast_type", "") == "group"
        except Exception:  # noqa: BLE001
            return False

    def muted_speakers(self) -> set[str]:
        """The speakers (not groups) muted right now, of those that answer."""
        out = set()
        for c in list(self.casts):
            try:
                if getattr(c.cast_info, "cast_type", "") != "group" and self._answered(c) and c.status.volume_muted:
                    out.add(c.cast_info.friendly_name)
            except Exception:  # noqa: BLE001
                pass
        return out

    def is_muted(self, name: str) -> bool:
        try:
            c = self._device(name)
            return bool(c is not None and c.status is not None and c.status.volume_muted)
        except Exception:  # noqa: BLE001
            return False

    def mute_again(self, names, wait_s: float = 1.5, sleep=time.sleep) -> list[str]:
        """After a group's volume moved: the speakers that were muted before it and are not now, muted again
        (a speaker muted, by hand or by its room, stays muted when its group's volume changes: the user)."""
        sleep(wait_s)
        did = []
        for name in names:
            try:
                c = self._device(name)
                if c is not None and c.status is not None and not c.status.volume_muted:
                    c.set_volume_muted(True)
                    did.append(name)
            except Exception:  # noqa: BLE001
                pass
        return did

    def set_device_volume(self, name: str, level: float) -> dict:
        c = self._device(name)
        if c is None:
            raise ValueError(f"no speaker called {name!r}")
        c.set_volume(max(0.0, min(1.0, float(level))))
        return {"name": name, "volume": round(max(0.0, min(1.0, float(level))), 2)}

    def status(self) -> dict:
        playing = self.state == "PLAYING"
        pos = self.base + self.last_time + (time.monotonic() - self.last_at if playing else 0.0)
        vol = None
        if self.cast is not None and self.cast.status is not None:
            vol = round(float(self.cast.status.volume_level), 2)
        return {"connected": self.cast is not None, "device": self.name, "state": self.state, "position": round(pos, 3),
                "duration": self.duration, "volume": vol, "media_url": self.media_url}
