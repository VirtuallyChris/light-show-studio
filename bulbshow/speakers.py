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


class Speakers:
    def __init__(self):
        self.ip = govee_lan.local_ip()
        self.zc = None
        self.lock = threading.Lock()
        self.cached: list[dict] = []
        self.cached_at = 0.0

    def _zeroconf(self):
        import zeroconf
        if self.zc is None:
            self.zc = zeroconf.Zeroconf(interfaces=[self.ip]) if self.ip else zeroconf.Zeroconf()
        return self.zc

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
        import pychromecast
        with self.lock:
            casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[name], discovery_timeout=8,
                                                                  zeroconf_instance=self._zeroconf())
            browser.stop_discovery()
        if not casts:
            raise LookupError(f"{name} is not on the network right now")
        mgr = CastManager(self.ip)
        mgr.casts = list(casts)
        mgr.connect(name)
        return mgr


class FakeCast:
    """Stands in for a speaker (tests, and a copy run with --no-cast): plays nothing, and reports a
    position that runs with the clock from the moment a stream is handed to it."""

    def __init__(self, name: str, clock=time.monotonic):
        self.name, self.clock = name, clock
        self.t0 = None
        self.url = ""
        self.state = "IDLE"

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

    def list(self, fresh: bool = False):
        return [{"name": n, "model": "virtual", "group": False} for n in self.names]

    def open(self, name: str):
        if name not in self.names:
            raise LookupError(f"{name} is not on the network right now")
        c = FakeCast(name)
        self.opened.append(c)
        return c
