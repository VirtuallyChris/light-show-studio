"""Google Cast speakers: listing them, and opening one for a show.

Each running show gets a speaker of its own, connected afresh every time it starts (a pychromecast
connection cannot be reopened once closed). The studio's CastManager does the casting and reports
how far into the stream the speaker is. The speakers' volume is never touched here.
"""

from __future__ import annotations

import threading
import time

from lightshow import govee_lan
from lightshow.studio.cast import CastManager
from lightshow.studio.rooms import OURS_OR_IDLE


class Speakers:
    def __init__(self):
        self.ip = govee_lan.local_ip()
        self.lock = threading.Lock()
        self.cached: list[dict] = []
        self.cached_at = 0.0

    def _zeroconf(self):
        """A new discovery engine for every lookup: pychromecast's stop_discovery() closes the one it was
        given, so a shared one died with the first lookup that ended, and the next show to start failed
        with "The event loop is not running"."""
        import zeroconf
        return zeroconf.Zeroconf(interfaces=[self.ip]) if self.ip else zeroconf.Zeroconf()

    def list(self, fresh: bool = False) -> list[dict]:
        import pychromecast
        with self.lock:
            if self.cached and not fresh and time.monotonic() - self.cached_at < 120:
                return self.cached
            casts, browser = pychromecast.get_chromecasts(timeout=5, zeroconf_instance=self._zeroconf())
            browser.stop_discovery()
            out = [{"name": c.cast_info.friendly_name, "model": c.cast_info.model_name, "group": c.cast_info.cast_type == "group"}
                   for c in casts]
            out.sort(key=lambda d: (d["group"], d["name"].lower()))
            self.cached, self.cached_at = out, time.monotonic()
            return out

    def open(self, name: str) -> CastManager:
        """A connected CastManager for the speaker. Its discovery keeps running for as long as the show
        does: the connection finds (and refinds) the speaker through it, and with discovery stopped
        connecting times out."""
        import pychromecast
        with self.lock:
            casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[name], discovery_timeout=8,
                                                                  zeroconf_instance=self._zeroconf())
        if not casts:
            browser.stop_discovery()
            raise LookupError(f"{name} is not on the network right now")
        mgr = ShowCast(self.ip, browser)
        mgr.casts = list(casts)
        try:
            mgr.connect(name)
        except Exception:
            mgr.disconnect()
            mgr.stop_browser()
            raise
        return mgr


class ShowCast(CastManager):
    """The studio's CastManager, holding on to the discovery that found its speaker until it lets go."""

    def __init__(self, interface_ip, browser):
        super().__init__(interface_ip)
        self.show_browser = browser

    def disconnect(self) -> None:
        """Let the speaker go. It is stopped only while it still plays the show: one taken over (a doorbell cue,
        the party's group, Spotify) is left playing that (CastManager's own disconnect would stop it too)."""
        was = self.cast is not None              # (connect() calls this first, before there is a speaker: keep looking then)
        if was and not self.playing_ours():
            cast, self.cast = self.cast, None
            self._stop_poll.set()
            try:
                cast.disconnect(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        super().disconnect()
        if was:
            self.stop_browser()

    def playing_ours(self) -> bool:
        st = getattr(getattr(self.cast, "media_controller", None), "status", None)
        return bool(self.media_url) and getattr(st, "content_id", None) == self.media_url and not self.held_by()

    def held_by(self) -> str:
        """The app another sender is playing on the speaker ("" when it is free, showing its backdrop, or in
        the Default Media Receiver, where the show and the studio play)."""
        st = getattr(self.cast, "status", None)
        app = getattr(st, "app_id", None)
        return "" if app in OURS_OR_IDLE else str(getattr(st, "display_name", None) or "another app")

    def stop_browser(self) -> None:
        browser, self.show_browser = self.show_browser, None
        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception:  # noqa: BLE001
                pass


class FakeCast:
    """Stands in for a speaker (tests, and a copy run with --no-cast): plays nothing, and reports a
    position that runs with the clock from the moment a stream is handed to it."""

    def __init__(self, name: str, clock=time.monotonic):
        self.name, self.clock = name, clock
        self.t0 = None
        self.url = ""
        self.state = "IDLE"
        self.holder = ""             # another app playing on it ("" when free)

    def held_by(self) -> str:
        return self.holder

    def play_stream(self, url, content_type, title):
        self.url, self.t0, self.state = url, self.clock(), "PLAYING"
        return self.status()

    def status(self):
        pos = 0.0 if self.t0 is None else self.clock() - self.t0
        return {"connected": True, "device": self.name, "state": self.state, "position": round(pos, 3), "media_url": self.url}

    def stop(self):
        self.state = "IDLE"
        return self.status()

    def disconnect(self):
        self.state = "IDLE"


class FakeSpeakers:
    def __init__(self, names=("Office Home 2", "Kitchen Home", "Game Home")):
        self.names = list(names)
        self.opened: list[FakeCast] = []
        self.held: dict[str, str] = {}   # speaker -> another app playing on it

    def list(self, fresh: bool = False):
        return [{"name": n, "model": "virtual", "group": False} for n in self.names]

    def open(self, name: str):
        if name not in self.names:
            raise LookupError(f"{name} is not on the network right now")
        c = FakeCast(name)
        c.holder = self.held.get(name, "")
        self.opened.append(c)
        return c
