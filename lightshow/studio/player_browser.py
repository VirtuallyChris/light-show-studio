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

Nothing has to be set up on Windows either: unless an output is chosen in Settings, the studio picks one this computer isn't
using (VB-Cable if it is there, otherwise S/PDIF, a monitor's HDMI, and so on), and mutes that output while the player runs.
A muted output is still recorded in full, as the sound is recorded before the mute. Only a computer with one output needs
VB-Cable. Muting the player's own sound wouldn't do: Windows silences an app's muted session in the recording too.

Mac and Linux need no output nobody listens to, and no setup at all, and use Google Chrome (it has the protected-audio
module Spotify needs, which Linux's own Chromium builds lack). On Linux the player is started on an output the studio
makes for it (linux_audio); on a Mac a tap records it and silences it (mac_capture).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from . import audio_route, linux_audio, mac_capture
from .winpaths import local_appdata
from .updates import clean_env
from ..appdirs import popen_outside

HOME = os.path.join(local_appdata(), "lanlightshow")
PROFILE = os.path.join(HOME, "player-browser")
# Part of the output's name (VB-Cable's, by default); config.json jam_hidden_output. Mac and Linux make their own.
AUTO = "auto"
HIDDEN_OUTPUT = {"darwin": mac_capture.DEVICE, "linux": linux_audio.DESCRIPTION}.get(sys.platform, AUTO)
MUTED_NOTE = os.path.join(HOME, "muted-output.txt")     # the output this studio muted, should it crash before unmuting
BROWSER_NAME = {"darwin": "Google Chrome, hidden", "linux": "Google Chrome, hidden"}.get(sys.platform, "Microsoft Edge, hidden")


def own_hidden_output() -> bool:
    """Does the studio make the hidden output itself (so there is nothing to pick)?"""
    return sys.platform != "win32"


def hidden_problem() -> str:
    """Why this system cannot record Spotify's sound, or "". (Windows: said when the output is looked for.)"""
    if sys.platform == "darwin":
        return mac_capture.problem()
    if sys.platform == "win32":
        return ""
    return "" if linux_audio.available() else "Jam mode needs PulseAudio or PipeWire (the pactl command) to record Spotify's sound"


def pick_hidden_output(outputs: list[str], default: str = "", avoid: str = "") -> tuple[str, bool]:
    """The output to play Spotify into unheard, and whether it needs muting: ("", False) if this computer has no spare one.
    Never the default output or the one the room hears; VB-Cable if it is there, else the least likely to be in use."""
    spare = [o for o in outputs if o != default and not (avoid and avoid.lower() in o.lower())]
    cable = next((o for o in spare if "cable input" in o.lower()), "")
    if cable:
        return cable, False

    def rank(name: str) -> int:
        low = name.lower()
        if "s/pdif" in low or "spdif" in low or "digital" in low:
            return 0
        if "headset" in low or "headphone" in low or "hands-free" in low or "bluetooth" in low:
            return 2
        return 1
    spare.sort(key=rank)
    return (spare[0], True) if spare else ("", False)


def edge() -> str | None:
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return None


def player_browser_exe(plat: str = sys.platform, exists=os.path.exists, which=shutil.which) -> str | None:
    if plat == "win32":
        return edge()
    if plat == "darwin":
        return next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge") if exists(p)), None)
    return next((p for p in map(which, ("google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable")) if p), None)


def _version(exe: str) -> str:
    if sys.platform != "win32":
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, env=clean_env()).stdout
            return next((w for w in out.split() if w[:1].isdigit() and "." in w), "140.0.0.0")
        except (OSError, subprocess.SubprocessError):
            return "140.0.0.0"
    folder = os.path.dirname(exe)
    vs = [d for d in os.listdir(folder) if d[:1].isdigit() and os.path.isdir(os.path.join(folder, d))]
    vs.sort(key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])
    return vs[-1] if vs else "140.0.0.0"


def user_agent(exe: str, plat: str = sys.platform, version: str | None = None) -> str:
    # a headless browser calls itself "Headless..."; Spotify's player may turn that away. It is the same browser.
    v = version or _version(exe)
    if plat == "darwin":
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"
    if plat != "win32":
        return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36 Edg/{v}"


def _processes() -> str:
    """Every process on a Mac or Linux, as "pid<tab>command" lines."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return "\n".join("\t".join(line.strip().split(None, 1)) for line in out.splitlines() if line.strip())


def audio_pids(profile: str = PROFILE) -> list[int]:
    """The browser's audio processes for the player's profile (normally one)."""
    if sys.platform != "win32":
        return parse_audio_pids(_processes(), profile)
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
    """End browser processes still running the player's profile (the profile can only be open once)."""
    if sys.platform != "win32":
        for line in _processes().splitlines():
            pid, _, cmd = line.partition("\t")
            if f"--user-data-dir={profile}" in cmd and pid.isdigit():
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass
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

    def __init__(self, url: str, hidden_output: str = HIDDEN_OUTPUT, avoid=lambda: ""):
        """`avoid`: the output the room hears, never to be picked (config.json jam_room_output)."""
        self.url, self.hidden_output, self.avoid = url, hidden_output, avoid
        self.muted = ""                                    # the output this player muted
        self.proc: subprocess.Popen | None = None
        self.routed_pid: int | None = None
        self.error = ""
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_launch = 0.0
        self.mute_tap: subprocess.Popen | None = None      # (a Mac) silences the player for as long as it runs

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def routed(self) -> bool:
        return self.running and self.routed_pid is not None

    @property
    def automatic(self) -> bool:
        return sys.platform == "win32" and self.hidden_output in ("", AUTO)

    def output_name(self) -> str | None:
        """The output Spotify plays into: the one chosen in Settings, or one picked now. None if there is none to pick."""
        if not self.automatic:
            return self.hidden_output
        if self.muted:
            return self.muted                              # (it stays put while it plays)
        try:
            import soundcard as sc
            return pick_hidden_output([s.name for s in sc.all_speakers()], sc.default_speaker().name, self.avoid())[0] or None
        except Exception:  # noqa: BLE001
            return None

    def no_output_note(self) -> str:
        if self.automatic:
            return ("This computer has only one sound output, so there is nowhere for Spotify to play unheard. "
                    "Install VB-Cable (free, vb-audio.com/Cable) and the studio will use it by itself.")
        return f"there is no output named like {self.hidden_output!r} for Spotify to play into unheard"

    def _mute_output(self, name: str) -> bool:
        """Silences a picked output for as long as the player runs, unless it needs no muting. Was it muted already? Then
        it is left that way afterwards, too."""
        if self.muted == name or pick_hidden_output([name])[1] is False:
            return True
        before = audio_route.device_mute(name)
        if before is None:
            return False
        if not before:
            os.makedirs(HOME, exist_ok=True)
            with open(MUTED_NOTE, "w", encoding="utf-8") as fh:
                fh.write(name)
            if audio_route.device_mute(name, True) is not True:
                return False
            self.muted = name
        return True

    def _unmute_output(self) -> None:
        if self.muted:
            audio_route.device_mute(self.muted, False)
            self.muted = ""
        try:
            os.remove(MUTED_NOTE)
        except OSError:
            pass

    @staticmethod
    def undo_leftover_mute() -> None:
        """A studio that crashed left its output muted: put it back."""
        try:
            with open(MUTED_NOTE, encoding="utf-8") as fh:
                name = fh.read().strip()
            os.remove(MUTED_NOTE)
        except OSError:
            return
        if name:
            audio_route.device_mute(name, False)

    def start(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._keep, daemon=True)
            self.thread.start()

    def _launch(self) -> None:
        close_leftovers()                                 # a player left behind by a studio that crashed
        exe = player_browser_exe()
        if exe is None:
            self.error = ("Microsoft Edge is not installed; the studio's Spotify player needs it" if sys.platform == "win32"
                          else "Google Chrome is not installed; the studio's Spotify player needs it")
            return
        env = clean_env()
        if sys.platform == "win32":
            self.undo_leftover_mute()
            output = self.output_name()
            if not output or audio_route.device_id(output) is None:
                self.error = self.no_output_note()
                return
            if self.automatic and not self._mute_output(output):
                self.error = f"could not silence {output} for Spotify to play into"
                return
        else:
            self.error = hidden_problem()
            if self.error:
                return
            if sys.platform != "darwin":
                if linux_audio.ensure_sink() is None:
                    self.error = "could not make an output for Spotify to play into"
                    return
                env["PULSE_SINK"] = linux_audio.SINK
        os.makedirs(PROFILE, exist_ok=True)
        self.last_launch = time.monotonic()
        self.routed_pid = None
        args = [exe, "--headless=new", f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required", "--disable-features=Translate,TranslateUI",
                f"--user-agent={user_agent(exe)}", self.url]
        try:
            self.proc = popen_outside(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.error = ""
        except OSError as e:
            self.error = f"could not start the player's browser: {e}"

    def _mute(self, pid: int) -> bool:
        """A Mac: a tap on the player that goes nowhere, so nothing reaches the room before a recording tap starts."""
        self._unmute()
        helper = mac_capture.helper_path()
        if not helper:
            return False
        try:
            self.mute_tap = subprocess.Popen([helper, str(pid)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False

    def _unmute(self) -> None:
        if self.mute_tap is not None:
            try:
                self.mute_tap.stdin.close()               # (how the helper is told to end)
                self.mute_tap.wait(3)
            except (OSError, subprocess.SubprocessError):
                self.mute_tap.kill()
            self.mute_tap = None

    def _keep(self) -> None:
        """Start the player, route its sound as soon as it has any, and restart it if it dies."""
        while not self.stop_flag.is_set():
            if not self.running and time.monotonic() - self.last_launch > 10.0:
                self._launch()
            if self.running and sys.platform.startswith("linux"):
                self.routed_pid = self.proc.pid           # (it started on the hidden output: nothing to move)
            elif self.running and sys.platform == "darwin":
                pids = audio_pids()
                if pids and pids[0] != self.routed_pid:
                    self.routed_pid = pids[0] if self._mute(pids[0]) else None
                elif not pids:
                    self.routed_pid = None
            elif self.running:
                pids = audio_pids()
                new = [p for p in pids if p != self.routed_pid]
                if new:
                    output = self.output_name()
                    if output and audio_route.route(new[0], output):
                        self.routed_pid = new[0]
                        self.error = ""
                    else:
                        self.error = f"Windows would not send Edge's sound to {output}"
                elif not pids:
                    self.routed_pid = None
            self.stop_flag.wait(1.0 if self.routed_pid is None else 5.0)

    def stop(self) -> None:
        """Hand Edge's sound back to the default output, then close the player."""
        self.stop_flag.set()
        pid = self.routed_pid
        if pid is not None and self.running and sys.platform == "win32":
            audio_route.route(pid, None)                  # needs the player still playing its silent stream
        self.routed_pid = None
        if self.proc is not None and self.proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                self.proc.kill()
                close_leftovers()
        self.proc = None
        if sys.platform == "win32":
            self._unmute_output()                         # (after the player: nothing plays once it is gone)
        elif sys.platform == "darwin":
            self._unmute()
        elif sys.platform != "win32":
            linux_audio.remove_sink()

    def to_json(self) -> dict:
        return {"hidden": True, "running": self.running, "routed": self.routed, "hidden_output": self.muted or self.hidden_output, "own_output": own_hidden_output(),
                "route_error": self.error}
