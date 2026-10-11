"""How healthy the lights' Wi-Fi is right now, and how many frames a second they can take.

The lights never say whether a frame arrived (real-time frames are one-way UDP), but the Wi-Fi they
share shows its state in a ping: replies come back slow, or not at all, when the air is busy or a
light is struggling, and that is exactly when frames arrive late and the lights stutter. So while a
show plays, the studio pings the lights it drives, one small ping every half second in turn, and a
governor sets the frame rate from the replies: down at once when a reply is lost or slow, back up
step by step after a stretch of clean, quick replies. Between 15 and 60 frames a second.

Pings use Windows' own ICMP API (no extra process, no admin rights); elsewhere, the ping command.
"""

from __future__ import annotations

import collections
import subprocess
import sys
import threading
import time

LADDER = [15, 20, 24, 30, 40, 50, 60]


class FpsGovernor:
    """Frames a second for the lights, from a stream of ping results (milliseconds, or None if lost)."""

    SLOW_MS = 90          # replies slower than this (90th percentile, last 5 s) mean the air is struggling
    SEVERE_MS = 200       # this slow, or a third of the pings lost: two steps down
    QUICK_MS = 45         # clean: every reply in the last 10 s back, the 90th percentile under this
    UP_AFTER_S = 15.0     # how long things must stay clean, and since the last change, before a step up
    HOLD_S = 30.0         # after a step down, no step up for this long
    DOWN_GAP_S = 2.0      # steps down at most this often (one bad moment is one step)

    def __init__(self, lo: int = 15, hi: int = 60, start: int = 24, clock=time.monotonic):
        self.levels = [f for f in LADDER if lo <= f <= hi] or [lo]
        self.i = min(range(len(self.levels)), key=lambda k: abs(self.levels[k] - start))
        self.clock = clock
        self.samples: collections.deque = collections.deque()
        t = clock()
        self.changed_at, self.hold_until, self.reason = t, t, "starting"

    @property
    def fps(self) -> int:
        return self.levels[self.i]

    def report(self, rtt_ms: float | None) -> None:
        now = self.clock()
        self.samples.append((now, rtt_ms))
        while self.samples and now - self.samples[0][0] > 10.0:
            self.samples.popleft()

    def _window(self, seconds: float):
        now = self.clock()
        return [r for t, r in self.samples if now - t <= seconds]

    @staticmethod
    def _p90(rtts):
        got = sorted(r for r in rtts if r is not None)
        return got[min(len(got) - 1, int(0.9 * len(got)))] if got else None

    def decide(self) -> int:
        now = self.clock()
        last5, last10 = self._window(5.0), self._window(10.0)
        # a lost ping counts once: only losses since the last change can step down again
        lost3 = sum(r is None for t, r in self.samples if now - t <= 3.0 and t > self.changed_at)
        loss5 = sum(r is None for r in last5) / len(last5) if last5 else 0.0
        p90 = self._p90(last5)
        lag = lost3 > 0 or (p90 is not None and p90 > self.SLOW_MS)
        severe = loss5 >= 0.33 or (p90 is not None and p90 > self.SEVERE_MS)
        if lag and now - self.changed_at >= self.DOWN_GAP_S and self.i > 0:
            steps = 2 if severe else 1
            self.i = max(0, self.i - steps)
            self.changed_at, self.hold_until = now, now + self.HOLD_S
            self.reason = f"{'lost pings' if lost3 else f'slow replies ({p90:.0f} ms)'}"
            return self.fps
        clean = (len(last10) >= 5 and all(r is not None for r in last10)
                 and (self._p90(last10) or 0) < self.QUICK_MS)
        if (clean and now >= self.hold_until and now - self.changed_at >= self.UP_AFTER_S
                and self.i < len(self.levels) - 1):
            self.i += 1
            self.changed_at = now
            self.reason = f"clean replies ({self._p90(last10):.0f} ms)"
        return self.fps


# ------------------------------------------------------------------------------------- pinging
def _icmp_windows():
    import ctypes
    from ctypes import wintypes

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                    ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                    ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                    ("Options", IP_OPTION_INFORMATION)]

    lib = ctypes.windll.Iphlpapi
    lib.IcmpCreateFile.restype = wintypes.HANDLE
    lib.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    lib.IcmpSendEcho.restype = wintypes.DWORD
    lib.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    handle = lib.IcmpCreateFile()
    payload = b"lights"
    size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8
    lock = threading.Lock()

    def ping(ip: str, timeout_ms: int = 300) -> float | None:
        import socket
        addr = int.from_bytes(socket.inet_aton(ip), "little")
        buf = ctypes.create_string_buffer(size)
        with lock:
            n = lib.IcmpSendEcho(handle, addr, payload, len(payload), None, buf, size, timeout_ms)
        if not n:
            return None
        rep = ICMP_ECHO_REPLY.from_buffer(buf)
        return float(rep.RoundTripTime) if rep.Status == 0 else None
    return ping


def _icmp_command():
    def ping(ip: str, timeout_ms: int = 300) -> float | None:
        t0 = time.monotonic()
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=2)
        except Exception:  # noqa: BLE001
            return None
        return (time.monotonic() - t0) * 1000 if r.returncode == 0 else None
    return ping


def make_pinger():
    if sys.platform == "win32":
        try:
            return _icmp_windows()
        except Exception:  # noqa: BLE001 - fall back to the command
            pass
    return _icmp_command()


class LinkMonitor:
    """While the lights are being driven, pings them in turn and keeps their frame rate where the
    Wi-Fi can carry it (the output's max_fps)."""

    def __init__(self, output, lo: int = 15, hi: int = 60, every_s: float = 0.5, log=print):
        self.output, self.every_s, self.log = output, every_s, log
        self.governor = FpsGovernor(lo, hi)
        self.ping = make_pinger()
        self.stop = threading.Event()
        self.k = 0

    def start(self) -> None:
        threading.Thread(target=self._run, name="link-monitor", daemon=True).start()

    def _run(self) -> None:
        while not self.stop.wait(self.every_s):
            out = self.output
            if not (out.enabled and out.active and out.devices):
                continue
            ip = out.devices[self.k % len(out.devices)].ip
            self.k += 1
            self.governor.report(self.ping(ip, 300))
            before = out.max_fps
            fps = self.governor.decide()
            if fps != before:
                out.max_fps = fps
                self.log(f"[lights] {before:.0f} -> {fps} fps: {self.governor.reason}")

    def to_json(self) -> dict:
        return {"fps": self.governor.fps, "reason": self.governor.reason}
