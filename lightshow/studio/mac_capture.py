"""macOS: recording the hidden player with a Core Audio process tap. No driver, nothing for the user to set up; macOS 14.2
or later, and it asks once for permission to record audio. The tap also silences the player, so the room only ever hears
the studio's delayed copy.

The helper (packaging/mac/audiotap.swift, built with the app) does the tapping, as those calls are hard to reach from
Python. It sends raw frames down a pipe, and they stay in memory: nothing is written to disk.
"""

from __future__ import annotations

import json
import os
import platform
import select
import shutil
import subprocess
import sys
import time

import numpy as np

from ..appdirs import FROZEN, SOURCE_ROOT, code_dir, data_dir

DEVICE = "Light Show Studio tap"                 # what the recorder is told to record, in place of an output's name
MIN_VERSION = (14, 2)


def version_ok(version: str | None = None) -> bool:
    version = platform.mac_ver()[0] if version is None else version
    try:
        return tuple(int(p) for p in version.split(".")[:2]) >= MIN_VERSION
    except ValueError:
        return False


def helper_path() -> str | None:
    """The helper: inside a packaged app; from source, built the first time from its Swift file (Xcode's tools)."""
    if FROZEN:
        path = os.path.join(code_dir(), "lightshow", "studio", "bin", "audiotap")
        return path if os.path.exists(path) else None
    path = os.path.join(data_dir(), "packaging", "mac", "audiotap")
    source = os.path.join(SOURCE_ROOT, "packaging", "mac", "audiotap.swift")
    if os.path.exists(path) and os.path.getmtime(path) >= os.path.getmtime(source):
        return path
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None
    try:
        subprocess.run([swiftc, "-O", "-target", f"{platform.machine()}-apple-macos14.2", "-framework", "CoreAudio", "-framework", "AudioToolbox",
                        "-framework", "Foundation", "-o", path, source], capture_output=True, timeout=180, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return path


def problem() -> str:
    """Why Spotify cannot be recorded on this Mac, or ""."""
    if not version_ok():
        return "the party needs macOS 14.2 or later to record Spotify's sound"
    if not helper_path():
        return "the audio helper is missing: from source, the party needs Xcode's command line tools (xcode-select --install)"
    return ""


def available() -> bool:
    return sys.platform == "darwin" and not problem()


class Resampler:
    """Float frames at one rate to another, continuously across blocks (the tap's rate is the device's, usually 48000)."""

    def __init__(self, rate_in: int, rate_out: int):
        self.step = rate_in / rate_out
        self.pos = 0.0                           # where the next output frame falls, counted from self.last
        self.last: np.ndarray | None = None

    def feed(self, x: np.ndarray) -> np.ndarray:
        if self.step == 1.0 or not len(x):
            return x
        if self.last is None:
            self.last = x[:1]
        joined = np.concatenate([self.last, x])
        at = np.arange(self.pos, len(joined) - 1, self.step)
        out = np.stack([np.interp(at, np.arange(len(joined)), joined[:, c]) for c in range(joined.shape[1])], axis=1)
        self.pos = (at[-1] + self.step - (len(joined) - 1)) if len(at) else self.pos - (len(joined) - 1)
        self.last = joined[-1:]
        return out.astype(np.float32)


def to_frames(raw: bytes, channels_in: int, channels: int) -> np.ndarray:
    """Whole frames of the helper's 32-bit floats, as (n, channels)."""
    x = np.frombuffer(raw, dtype=np.float32).reshape(-1, channels_in)
    if channels_in >= channels:
        return x[:, :channels]
    return np.repeat(x[:, :1], channels, axis=1)


def read_tap(command: list[str], sr: int, channels: int, stop, restart=lambda: False):
    """Runs the helper until it ends, `stop()` or `restart()`, giving int16 frames at `sr` as they come."""
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    fd = proc.stdout.fileno()
    try:
        spare = b""                              # (the first line, and any frames that came with it)
        end = time.monotonic() + 15
        while b"\n" not in spare:
            if stop() or time.monotonic() > end:
                return
            if select.select([fd], [], [], 0.25)[0]:
                chunk = os.read(fd, 4096)
                if not chunk:
                    return
                spare += chunk
        hello, spare = spare.split(b"\n", 1)
        info = json.loads(hello)
        rate, ch = int(info["rate"]), int(info["channels"])
        size = ch * 4
        resampler = Resampler(rate, sr)
        ended = False
        while not ended and not stop() and not restart():
            usable = len(spare) // size * size           # (first, whatever came with the first line)
            if usable:
                frames = resampler.feed(to_frames(spare[:usable], ch, channels))
                spare = spare[usable:]
                if len(frames):
                    yield (np.clip(frames, -1, 1) * 32767).astype(np.int16)
            if select.select([fd], [], [], 0.25)[0]:
                chunk = os.read(fd, 65536)
                spare += chunk
                ended = not chunk
        usable = len(spare) // size * size               # (what it sent just before it ended)
        if ended and usable:
            frames = resampler.feed(to_frames(spare[:usable], ch, channels))
            if len(frames):
                yield (np.clip(frames, -1, 1) * 32767).astype(np.int16)
    finally:
        try:
            proc.stdin.close()
            proc.wait(4)
        except (OSError, subprocess.SubprocessError):
            proc.kill()


def frames(sr: int, channels: int, stop, pids=None, helper=None):
    """The hidden player's sound, for as long as `stop()` is false. Its audio process can come and go (Chrome restarts it),
    so the tap follows it; meanwhile nothing comes, and the recorder keeps time with silence."""
    if pids is None:
        from .player_browser import audio_pids as pids
    helper = helper or helper_path()
    while not stop():
        now = pids()
        if not now:
            time.sleep(0.5)
            continue
        checked = [time.monotonic()]

        def moved() -> bool:
            if time.monotonic() - checked[0] < 3.0:
                return False
            checked[0] = time.monotonic()
            return pids() != now
        yield from read_tap([helper, *map(str, now)], sr, channels, stop, moved)
        if not stop():
            time.sleep(0.5)
