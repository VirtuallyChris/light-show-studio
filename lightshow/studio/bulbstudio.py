"""Bulb Studio (bulbstudio/, an app of its own on this PC) from the studio: its shows, which are running and on
which speaker, and switching one on or off from the Party Hub's Lights tab (the user).

The two talk over Bulb Studio's own API on 127.0.0.1. When it is not open, its shows are read from its folder (all
off), and switching one on starts it without a window, the way the studio starts with no console: it keeps the
show going until it is switched off (opening Bulb Studio later finds it running and shows its window).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

from ..appdirs import FROZEN, bulb_studio_dir, code_dir, self_command

SHOWS_DIR = os.path.join(bulb_studio_dir(), "shows")


def _http(method: str, url: str, body: dict | None = None, timeout: float = 2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


class BulbStudio:
    START_WAIT_S = 20.0                 # Bulb Studio takes a few seconds to come up (Python, its speakers' discovery)

    def __init__(self, port: int = 8770, shows_dir: str = SHOWS_DIR, launch: bool = True, http=_http,
                 clock=time.monotonic, sleep=time.sleep):
        self.base = f"http://127.0.0.1:{port}"
        self.port, self.shows_dir, self.launch = port, shows_dir, launch
        self.http, self.clock, self.sleep = http, clock, sleep

    # -- what there is ------------------------------------------------------------------
    def _from_app(self) -> list[dict] | None:
        try:
            shows = self.http("GET", self.base + "/api/shows")
        except Exception:  # noqa: BLE001 - not open
            return None
        try:
            running = (self.http("GET", self.base + "/api/running") or {}).get("shows") or {}
        except Exception:  # noqa: BLE001
            running = {}
        out = []
        for s in shows or []:
            r = running.get(s.get("id")) or {}
            out.append({"id": s.get("id"), "name": s.get("name") or s.get("id"), "speaker": s.get("speaker") or "",
                        "bulb": (s.get("bulb") or {}).get("name") or "", "ready": bool(s.get("ready")),
                        "on": bool(s.get("running")), "error": r.get("error") or ""})
        return out

    def _from_disk(self) -> list[dict]:
        out = []
        try:
            names = sorted(os.listdir(self.shows_dir))
        except OSError:
            return out
        for sid in names:
            try:
                with open(os.path.join(self.shows_dir, sid, "show.json"), encoding="utf-8") as fh:
                    s = json.load(fh)
            except (OSError, ValueError):
                continue
            bulb = s.get("bulb") or {}
            out.append({"id": sid, "name": s.get("name") or sid, "speaker": s.get("speaker") or "", "bulb": bulb.get("name") or "",
                        "ready": bool(s.get("duration") and s.get("speaker") and bulb), "on": False, "error": ""})
        return out

    def list(self) -> dict:
        """{open: is Bulb Studio running, shows: [{id, name, speaker, bulb, ready, on, error}]}; only the shows
        that can run (a sound, a speaker and a bulb) are any use from the phone."""
        live = self._from_app()
        shows = live if live is not None else self._from_disk()
        return {"open": live is not None, "shows": [s for s in shows if s["ready"]]}

    def speakers_in_use(self) -> dict[str, str]:
        """{speaker: the show playing on it} for every running show (none when Bulb Studio is not open)."""
        return {s["speaker"]: s["name"] for s in (self._from_app() or []) if s["on"] and s["speaker"]}

    def quit(self, why: str = "Light Show Studio closed") -> bool:
        """Close Bulb Studio if it is open: its shows stop and their bulbs are put back (the user: closing the
        studio closes Bulb Studio too). True if one was open."""
        try:
            self.http("POST", self.base + "/api/quit", {"why": why}, timeout=2.0)
            return True
        except Exception:  # noqa: BLE001 - not open
            return False

    def open_window(self) -> dict:
        """Bulb Studio running, for its window to open in the studio (started with no window of its own if it is
        not: its window keeps it running, and it quits once no show runs and no window is open). {ok, url, error}."""
        try:
            self.http("GET", self.base + "/api/settings", timeout=1.0)
        except Exception:  # noqa: BLE001 - not open yet
            if not self.launch:
                return {"ok": False, "url": "", "error": "Bulb Studio is not open (a test copy never starts it)"}
            self._start_app()
            if not self._wait_open():
                return {"ok": False, "url": "", "error": "Bulb Studio did not start"}
        return {"ok": True, "url": f"http://127.0.0.1:{self.port}/", "error": ""}

    # -- switching one ------------------------------------------------------------------
    def _start_app(self) -> None:
        exe = sys.executable
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        flags = 0
        if os.name == "nt":
            exe = w if os.path.exists(w) and not FROZEN else exe
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(self_command("bulbstudio", "--no-browser", "--port", str(self.port), exe=exe), cwd=code_dir(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)

    def _wait_open(self) -> bool:
        end = self.clock() + self.START_WAIT_S
        while self.clock() < end:
            try:
                self.http("GET", self.base + "/api/settings", timeout=1.0)
                return True
            except Exception:  # noqa: BLE001
                self.sleep(0.5)
        return False

    def set(self, sid: str, on: bool, busy: set[str] = frozenset()) -> dict:
        """Switch a show on or off. A show on a speaker in `busy` (the jam's) is not started: it would take the
        speaker from the party. Returns {ok, error}."""
        shows = self._from_app()
        if not on:
            if shows is not None:                        # (not open: nothing is running)
                self.http("POST", f"{self.base}/api/stop/{quote(sid)}", {})
            return {"ok": True, "error": ""}
        show = next((s for s in (shows if shows is not None else self._from_disk()) if s["id"] == sid), None)
        if show is None:
            return {"ok": False, "error": "no such bulb show"}
        if not show["ready"]:
            return {"ok": False, "error": f"{show['name']} needs a sound, a speaker and a bulb (set them in Bulb Studio)"}
        if show["speaker"] in busy:
            return {"ok": False, "error": f"{show['speaker']} is playing the party"}
        if shows is None:
            if not self.launch:
                return {"ok": False, "error": "Bulb Studio is not open"}
            self._start_app()
            if not self._wait_open():
                return {"ok": False, "error": "Bulb Studio did not start"}
        r = self.http("POST", f"{self.base}/api/run/{quote(sid)}", {}, timeout=30.0) or {}
        return {"ok": bool(r.get("ok")), "error": r.get("error") or ""}
