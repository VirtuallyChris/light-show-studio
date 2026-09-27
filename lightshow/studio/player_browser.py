"""The hidden player: Spotify plays in a windowless browser whose sound only the studio hears.

Spotify's player has to run in a real browser (it plays protected audio, which no
page can read), and the studio can only record it from an output device. So the
player runs in Microsoft Edge with no window at all, and the studio sets Edge's
output (Windows' per-app output setting) to an output nobody listens to, S/PDIF by
default. The studio records that output and plays it to the room, late, on the
default output. Nothing to route by hand, nothing in the taskbar.

Why Edge: Windows keeps the per-app output per program file, so the player cannot
be the Chrome you browse with (and a private copy of Chrome will not start outside
its install folder). Edge comes with Windows. While the studio runs, Edge's sound
goes to the hidden output, so an Edge window opened meanwhile is silent too; when
the studio closes it hands Edge back to the default output.

The player page opens a silent audio stream as soon as it loads, so Edge has an
audio stream to route before Spotify plays a note, and no music leaks to the room
live.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from . import audio_route

HOME = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "lanlightshow")
PROFILE = os.path.join(HOME, "player-browser")
HIDDEN_OUTPUT = "S/PDIF"                               # part of the output's name; config can change it


def edge() -> str | None:
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return None


def _version(exe: str) -> str:
    folder = os.path.dirname(exe)
    vs = [d for d in os.listdir(folder) if d[:1].isdigit() and os.path.isdir(os.path.join(folder, d))]
    vs.sort(key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])
    return vs[-1] if vs else "140.0.0.0"


def user_agent(exe: str) -> str:
    # headless Edge calls itself "HeadlessEdg"; Spotify's player may turn that away. It is the same Edge.
    v = _version(exe)
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36 Edg/{v}"


def audio_pids(profile: str = PROFILE) -> list[int]:
    """Edge's audio processes for the player's profile (normally one)."""
    if sys.platform != "win32":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_audio_pids(out, profile)


def parse_audio_pids(listing: str, profile: str) -> list[int]:
    pids = []
    for line in listing.splitlines():
        pid, _, cmd = line.partition("\t")
        if "audio.mojom.AudioService" in cmd and profile.lower() in cmd.lower() and pid.strip().isdigit():
            pids.append(int(pid))
    return pids


def close_leftovers(profile: str = PROFILE) -> None:
    """End Edge processes still running the player's profile (the profile can only be open once)."""
    if sys.platform != "win32":
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -like '*"
          + profile.replace("'", "''") + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


class HiddenPlayer:
    """Runs the windowless player, keeps its sound on the hidden output, and puts it back on exit."""

    def __init__(self, url: str, hidden_output: str = HIDDEN_OUTPUT):
        self.url, self.hidden_output = url, hidden_output
        self.proc: subprocess.Popen | None = None
        self.routed_pid: int | None = None
        self.error = ""
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_launch = 0.0

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def routed(self) -> bool:
        return self.running and self.routed_pid is not None

    def start(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._keep, daemon=True)
            self.thread.start()

    def _launch(self) -> None:
        close_leftovers()                                 # a player left behind by a studio that crashed
        exe = edge()
        if exe is None:
            self.error = "Microsoft Edge is not installed; the studio's Spotify player needs it"
            return
        if audio_route.device_id(self.hidden_output) is None:
            self.error = f"no output named like {self.hidden_output!r} to send Spotify to"
            return
        os.makedirs(PROFILE, exist_ok=True)
        self.last_launch = time.monotonic()
        self.routed_pid = None
        args = [exe, "--headless=new", f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required", "--disable-features=Translate,TranslateUI",
                f"--user-agent={user_agent(exe)}", self.url]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
            self.error = ""
        except OSError as e:
            self.error = f"could not start Edge: {e}"

    def _keep(self) -> None:
        """Start the player, route its sound as soon as it has any, and restart it if it dies."""
        while not self.stop_flag.is_set():
            if not self.running and time.monotonic() - self.last_launch > 10.0:
                self._launch()
            if self.running:
                pids = audio_pids()
                new = [p for p in pids if p != self.routed_pid]
                if new:
                    if audio_route.route(new[0], self.hidden_output):
                        self.routed_pid = new[0]
                        self.error = ""
                    else:
                        self.error = f"Windows would not send Edge's sound to {self.hidden_output}"
                elif not pids:
                    self.routed_pid = None
            self.stop_flag.wait(1.0 if self.routed_pid is None else 5.0)

    def stop(self) -> None:
        """Hand Edge's sound back to the default output, then close the player."""
        self.stop_flag.set()
        pid = self.routed_pid
        if pid is not None and self.running:
            audio_route.route(pid, None)                  # needs the player still playing its silent stream
        self.routed_pid = None
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        self.proc = None

    def to_json(self) -> dict:
        return {"hidden": True, "running": self.running, "routed": self.routed, "hidden_output": self.hidden_output,
                "route_error": self.error}
