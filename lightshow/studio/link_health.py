"""How healthy the lights' Wi-Fi is right now, and how many frames a second they can take.

The lights never say whether a frame arrived (real-time frames are one-way UDP), but the Wi-Fi they
share shows its state in a ping: replies come back slow, or not at all, when the air is busy or a
light is struggling, and that is exactly when frames arrive late and the lights stutter. So while a
show plays, the studio pings the lights it drives in turn (a small ping every quarter second, so each
of five lights about every 1.25 s), and each light has a governor of its own that sets that light's
frame rate from its replies: down at once when a reply is lost or slow, back up step by step after
a stretch of clean, quick replies. Between 15 and 40 frames a second, light by light: a pole at the
edge of the Wi-Fi never holds back a strip right next to the router.

A light that chokes at a rate is not tried at that rate again for a while (CEILING_S): the governor
used to climb straight back to it after every recovery, and each time the light fell behind (frames
piling up in it, replies at 100-270 ms) until it stepped down again: the lights lagged, over and over,
all night. Now each light settles where it can keep up.

A light still far behind at its lowest rate (half its pings lost, or replies past 250 ms, for 10 s) is
dropped and rejoined: black, nothing sent while its Wi-Fi clears, then real-time mode again (rejoin). A
pole held at 15 on a link losing 70% of its pings fell so far behind it showed a frame every 2 s.

Pings use Windows' own ICMP API (no extra process, no admin rights); elsewhere, the ping command.
"""

from __future__ import annotations

import collections
import re
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
    CEILING_S = 600.0     # a rate a light choked at is not climbed back to for this long

    def __init__(self, lo: int = 15, hi: int = 60, start: int = 24, clock=time.monotonic):
        self.levels = [f for f in LADDER if lo <= f <= hi] or [lo]
        self.i = min(range(len(self.levels)), key=lambda k: abs(self.levels[k] - start))
        self.clock = clock
        self.samples: collections.deque = collections.deque()
        t = clock()
        self.changed_at, self.hold_until, self.reason = t, t, "starting"
        self.ceiling, self.ceiling_until = len(self.levels) - 1, t     # the highest step it may climb to, for now

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

    STUCK_LOSS = 0.5      # way behind at its lowest rate: this share of the last 10 s of pings lost...
    STUCK_MS = 250        # ...or replies this slow (90th percentile)

    def stuck(self) -> str:
        """Why this light is far behind even at its lowest rate ('' when it is not): time to drop and rejoin it."""
        if self.i > 0:
            return ""
        got = self._window(10.0)
        if len(got) < 5:
            return ""
        loss = sum(r is None for r in got) / len(got)
        p90 = self._p90(got)
        if loss >= self.STUCK_LOSS:
            return f"{loss:.0%} of pings lost"
        return f"replies at {p90:.0f} ms" if p90 is not None and p90 > self.STUCK_MS else ""

    def fresh(self) -> None:
        """Judged anew from here (a light that rejoined): its pings from before say nothing about now."""
        self.samples.clear()
        self.changed_at = self.hold_until = self.clock()

    def decide(self) -> int:
        now = self.clock()
        last10 = self._window(10.0)
        # only replies since the last change judge the rate it is at now: the slow ones from the rate it just
        # left kept stepping it down again and again (and its ceiling with it) on stale evidence
        last5 = [r for t, r in self.samples if now - t <= 5.0 and t > self.changed_at]
        lost3 = sum(r is None for t, r in self.samples if now - t <= 3.0 and t > self.changed_at)
        loss5 = sum(r is None for r in last5) / len(last5) if last5 else 0.0
        p90 = self._p90(last5) if len(last5) >= 2 else None
        lag = lost3 > 0 or (p90 is not None and p90 > self.SLOW_MS)
        severe = loss5 >= 0.33 or (p90 is not None and p90 > self.SEVERE_MS)
        if lag and now - self.changed_at >= self.DOWN_GAP_S and self.i > 0:
            steps = 2 if severe else 1
            # it choked here: this rate is out of reach for a while, the one below it is the most it gets
            self.ceiling, self.ceiling_until = max(0, self.i - 1), now + self.CEILING_S
            self.i = max(0, self.i - steps)
            self.changed_at, self.hold_until = now, now + self.HOLD_S
            self.reason = "lost pings" if lost3 else f"slow replies ({p90:.0f} ms)"
            return self.fps
        clean = (len(last10) >= 5 and all(r is not None for r in last10)
                 and (self._p90(last10) or 0) < self.QUICK_MS)
        top = self.ceiling if now < self.ceiling_until else len(self.levels) - 1
        if (clean and now >= self.hold_until and now - self.changed_at >= self.UP_AFTER_S
                and self.i < top):
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


def ping_args(ip: str, plat: str = sys.platform) -> list[str]:
    """One ping, waiting a second at most. -W means milliseconds on a Mac and seconds on Linux."""
    return ["ping", "-c", "1", "-W", "1000" if plat == "darwin" else "1", ip]


def ping_ms(output: str) -> float | None:
    """The round trip the ping itself reports: time spent starting the command is not the light's."""
    m = re.search(r"time[=<]\s*([\d.]+)\s*ms", output)
    return float(m.group(1)) if m else None


def _icmp_command():
    def ping(ip: str, timeout_ms: int = 300) -> float | None:
        t0 = time.monotonic()
        try:
            r = subprocess.run(ping_args(ip), capture_output=True, text=True, timeout=2)
        except Exception:  # noqa: BLE001
            return None
        if r.returncode != 0:
            return None
        ms = ping_ms(r.stdout)
        return ms if ms is not None else (time.monotonic() - t0) * 1000
    return ping


def make_pinger():
    if sys.platform == "win32":
        try:
            return _icmp_windows()
        except Exception:  # noqa: BLE001 - fall back to the command
            pass
    return _icmp_command()


class LinkMonitor:
    """While the lights are being driven, pings them in turn and keeps each one's frame rate where its
    Wi-Fi can carry it (the output's fps_by_ip; max_fps is the fastest of them, for show)."""

    def __init__(self, output, lo: int = 15, hi: int = 60, every_s: float = 0.25, log=print, clock=time.monotonic, range_for=None):
        self.output, self.every_s, self.log = output, every_s, log
        self.lo, self.hi, self.clock = lo, hi, clock
        # range_for(ip) -> (lo, hi, start) for one light, or None for the defaults: the light poles never held
        # more than 15 frames a second, the strips 30 or so (a night of the log)
        self.range_for = range_for
        self.governors: dict[str, FpsGovernor] = {}
        self.governor = FpsGovernor(lo, hi, clock=clock)      # the starting rate, before a light is heard from
        self.ping = make_pinger()
        self.stop = threading.Event()
        self.k = 0
        self.sleep = time.sleep
        self.rejoined_at: dict[str, float] = {}          # when each light was last dropped and rejoined
        self.rejoining: set[str] = set()

    def _governor(self, ip: str) -> FpsGovernor:
        g = self.governors.get(ip)
        if g is None:
            r = self.range_for(ip) if self.range_for else None
            lo, hi, start = r if r else (self.lo, self.hi, 24)
            g = self.governors[ip] = FpsGovernor(lo, hi, start=start, clock=self.clock)
        return g

    def tick(self) -> None:
        """Ping the next light and set its frame rate from its own replies."""
        out = self.output
        devices = list(out.devices or [])                # once: another thread may change the list meanwhile
        if not (out.enabled and out.active and devices):
            return
        ip = devices[self.k % len(devices)].ip
        self.k += 1
        if ip in self.rejoining:                         # (its own pings, meanwhile: _link_back)
            return
        g = self._governor(ip)
        g.report(self.ping(ip, 300))
        before = out.fps_by_ip.get(ip, g.fps)
        fps = g.decide()
        out.fps_by_ip[ip] = fps
        out.max_fps = max(out.fps_by_ip.get(d.ip, g.fps) for d in devices)
        if fps != before:
            self.log(f"[lights] {ip}: {before:.0f} -> {fps} fps: {g.reason}")
        why = g.stuck()
        now = self.clock()
        if why and hasattr(out, "rejoin") and now - self.rejoined_at.get(ip, -1e9) >= self.REJOIN_GAP_S:
            self.rejoined_at[ip] = now
            self.rejoining.add(ip)
            self.log(f"[lights] {ip}: far behind at {fps} fps ({why}): dropped, black until its Wi-Fi is back")
            threading.Thread(target=self._rejoin, args=(ip, g), daemon=True, name=f"rejoin-{ip}").start()

    REJOIN_GAP_S = 120.0  # a light is dropped and rejoined at most this often
    DRAIN_S = 3.0         # nothing sent for at least this long: what its Wi-Fi had piled up clears
    REJOIN_MAX_S = 20.0   # and at most this long: back in the show, its link well or not

    def _rejoin(self, ip: str, g: FpsGovernor) -> None:
        try:
            t0 = time.monotonic()
            back = self.output.rejoin(ip, self._link_back)
            self.log(f"[lights] {ip}: rejoined after {time.monotonic() - t0:.1f} s"
                     f"{'' if back else ' (its Wi-Fi still weak)'}")
        except Exception as e:  # noqa: BLE001 - the show goes on
            self.log(f"[lights] {ip}: rejoin failed: {type(e).__name__}: {e}")
        finally:
            g.fresh()
            self.rejoining.discard(ip)

    def _link_back(self, ip: str) -> bool:
        """Wait, sending it nothing, until the light answers quickly three times in a row (at least DRAIN_S,
        at most REJOIN_MAX_S). True when its link came back."""
        waited, good = 0.0, 0
        while waited < self.REJOIN_MAX_S:
            rtt = self.ping(ip, 300)
            good = good + 1 if rtt is not None and rtt < FpsGovernor.SLOW_MS else 0
            if good >= 3 and waited >= self.DRAIN_S:
                return True
            self.sleep(0.5)
            waited += 0.5
        return False

    def start(self) -> None:
        threading.Thread(target=self._run, name="link-monitor", daemon=True).start()

    def _run(self) -> None:
        while not self.stop.wait(self.every_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - one bad tick must not end the monitor (the lights' rates)
                self.log(f"[lights] link monitor: {type(e).__name__}: {e}")

    def to_json(self) -> dict:
        lights = {ip: {"fps": g.fps, "reason": "rejoining" if ip in self.rejoining else g.reason} for ip, g in self.governors.items()}
        return {"fps": max([v["fps"] for v in lights.values()] or [self.governor.fps]), "lights": lights}
