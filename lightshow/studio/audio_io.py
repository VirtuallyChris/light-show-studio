"""Recording and playback in processes of their own.

Python runs one thread at a time. While the studio analyses music (and in Jam
mode it always is), a thread that has to hand audio to the sound card every
few milliseconds can be kept waiting far too long. Measured: with three busy
threads beside it, the in-process player stopped altogether. When recording
starves, Windows drops audio and the gap is heard as a skip; when playback
starves, the device runs dry and the lights, which follow its clock, stall.

So the sound card is talked to from two small processes that do nothing
else. They share a ring of samples with the studio through shared memory:

    recorder process  --writes-->  ring  --read by-->  studio (into the relay)
    studio (from the relay stream)  --writes-->  ring  --read by-->  player process

Each ring holds seconds of audio, so the studio can be busy for a long while
without anything being lost or heard.

Ring header (int64): 0 frames written, 1 frames read, 2 skip reading to here,
3 paused, 4 stop, 5 child ready, 6 child failed. Then 256 bytes of error text,
then the samples.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from multiprocessing import shared_memory

import numpy as np

HEADER = 8 * 8
ERR = 256
W, R, SKIP, PAUSED, STOP, READY, FAILED = range(7)
FAKE_SOURCE = "__fake_source__"       # tests: a ramp at real speed, no sound card involved
FAKE_SINK = "__fake_sink__"           # tests: swallows audio at real speed


class Ring:
    def __init__(self, capacity: int, channels: int, name: str | None = None):
        size = HEADER + ERR + capacity * channels * 2
        self.shm = shared_memory.SharedMemory(name=name, create=name is None, size=size)
        self.capacity, self.channels, self.owner = capacity, channels, name is None
        self.h = np.ndarray((8,), dtype=np.int64, buffer=self.shm.buf, offset=0)
        self.data = np.ndarray((capacity, channels), dtype=np.int16, buffer=self.shm.buf, offset=HEADER + ERR)
        if self.owner:
            self.h[:] = 0

    @property
    def name(self) -> str:
        return self.shm.name

    def put(self, frames: np.ndarray) -> None:
        """Append frames (writer side), wrapping round the ring."""
        n = len(frames)
        if n == 0:
            return
        i = int(self.h[W]) % self.capacity
        first = min(n, self.capacity - i)
        self.data[i:i + first] = frames[:first]
        if first < n:
            self.data[:n - first] = frames[first:]
        self.h[W] += n                                  # published only once the samples are in place

    def get(self, start: int, n: int) -> np.ndarray:
        i = start % self.capacity
        if i + n <= self.capacity:
            return self.data[i:i + n].copy()
        return np.concatenate([self.data[i:], self.data[:n - (self.capacity - i)]])

    def set_error(self, text: str) -> None:
        raw = text.encode("utf-8", "replace")[:ERR - 1]
        self.shm.buf[HEADER:HEADER + len(raw)] = raw
        self.shm.buf[HEADER + len(raw)] = 0
        self.h[FAILED] = 1

    def error(self) -> str:
        if not self.h[FAILED]:
            return ""
        raw = bytes(self.shm.buf[HEADER:HEADER + ERR])
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.shm.close()
            if self.owner:
                self.shm.unlink()
        except Exception:  # noqa: BLE001
            pass


# ========================================================================= the child processes
def _open_player(device: str | None, sr: int, ch: int, buffer_s: float):
    if device == FAKE_SINK:
        class Sink:
            def __enter__(self):
                self.t0, self.n = time.monotonic(), 0
                return self

            def __exit__(self, *a):
                pass

            def play(self, frames):
                self.n += len(frames)
                time.sleep(max(0.0, self.t0 + self.n / sr - buffer_s - time.monotonic()))
        return Sink()
    import soundcard as sc
    speaker = sc.get_speaker(device) if device else sc.default_speaker()
    return speaker.player(samplerate=sr, channels=ch, blocksize=int(sr * buffer_s))


class _Orphan:
    """Tells a child when the studio that started it is gone.

    A studio that is closed or killed does not get to stop its audio processes,
    and they would keep the sound device open, playing and recording for nobody.
    """

    def __init__(self):
        self.parent = mp.parent_process()
        self.checked = 0.0

    def __call__(self) -> bool:
        now = time.monotonic()
        if self.parent is None or now - self.checked < 0.5:
            return False
        self.checked = now
        return not self.parent.is_alive()


def _player_main(name: str, capacity: int, ch: int, sr: int, device: str | None, piece: int, buffer_s: float) -> None:
    """Play whatever the studio puts in the ring, in whole 10 ms periods, at the device's own pace."""
    from . import priority
    priority.process(priority.HIGH)                     # the sound card is served before any analysis
    priority.audio_thread()
    ring = Ring(capacity, ch, name)
    orphan = _Orphan()
    try:
        with _open_player(device, sr, ch, buffer_s) as player:
            ring.h[READY] = 1
            while not ring.h[STOP] and not orphan():
                if ring.h[SKIP] > ring.h[R]:
                    ring.h[R] = ring.h[SKIP]            # a new stream: forget what was queued
                if ring.h[PAUSED] or ring.h[W] - ring.h[R] < piece:
                    time.sleep(0.005)
                    continue
                start = int(ring.h[R])
                frames = ring.get(start, piece).astype(np.float32) / 32768.0
                player.play(frames)                     # blocks while the device buffer is full: that paces it
                if ring.h[R] == start:                  # unless a skip landed meanwhile
                    ring.h[R] = start + piece
    except Exception as e:  # noqa: BLE001
        ring.set_error(f"{type(e).__name__}: {e}")
    finally:
        ring.close()


def _recorder_main(name: str, capacity: int, ch: int, sr: int, device: str | None, block: int) -> None:
    """Record an output, keeping the timeline in step with the clock whatever the device does."""
    from . import priority
    priority.process(priority.HIGH)
    priority.high_thread()
    ring = Ring(capacity, ch, name)
    q: list = []
    lock = threading.Lock()

    def record():
        priority.audio_thread()
        try:
            if device == FAKE_SOURCE:
                k, t0 = 0, time.monotonic()
                while not ring.h[STOP]:
                    time.sleep(max(0.0, t0 + (k + block) / sr - time.monotonic()))
                    v = ((np.arange(k, k + block) % 30000) + 1).astype(np.int16)
                    with lock:
                        q.append(np.stack([v] * ch, axis=1))
                    k += block
                return
            from . import mac_capture
            if device == mac_capture.DEVICE:
                for frames in mac_capture.frames(sr, ch, lambda: ring.h[STOP]):
                    with lock:
                        q.append(frames)
                return
            import soundcard as sc
            mic = sc.get_microphone(device or sc.default_speaker().name, include_loopback=True)
            with mic.recorder(samplerate=sr, channels=ch, blocksize=block) as rec:
                while not ring.h[STOP]:
                    x = rec.record(numframes=block)
                    frames = (np.clip(x[:, :ch] if x.ndim == 2 else x[:, None].repeat(ch, 1), -1, 1) * 32767).astype(np.int16)
                    with lock:
                        q.append(frames)
        except Exception as e:  # noqa: BLE001
            ring.set_error(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=record, daemon=True)
    t.start()
    ring.h[READY] = 1
    t0, margin = time.monotonic(), int(0.25 * sr)
    orphan = _Orphan()
    try:
        while not ring.h[STOP] and not ring.h[FAILED]:
            if orphan():
                ring.h[STOP] = 1                        # and the recording thread with it
                break
            time.sleep(0.01)
            with lock:
                got, q[:] = list(q), []
            due = int((time.monotonic() - t0) * sr)
            for frames in got:                          # late audio covering time already filled is dropped
                extra = int(ring.h[W]) + len(frames) - (due + margin)
                if extra > 0:
                    frames = frames[min(extra, len(frames)):]
                ring.put(frames)
            behind = due - margin - int(ring.h[W])      # the device gave nothing (silence): keep time
            if behind > 0:
                ring.put(np.zeros((behind, ch), dtype=np.int16))
    finally:
        ring.close()


def _spawn(target, args) -> mp.Process:
    p = mp.get_context("spawn").Process(target=target, args=args, daemon=True)
    p.start()
    return p


def _wait_ready(ring: Ring, proc: mp.Process, timeout: float = 20.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if ring.h[FAILED]:
            return ring.error()
        if ring.h[READY]:
            return ""
        if not proc.is_alive():
            return "the audio process stopped"
        time.sleep(0.02)
    return "the audio process did not start"


# ============================================================================ studio side
class ProcessCapture:
    """Records one output device into the relay: the recording happens in its own process."""

    def __init__(self, relay, device_name: str | None = None, gate=None, block: int = 1024, seconds: float = 20.0):
        from .loopback import Gate
        self.relay, self.device_name, self.gate = relay, device_name, gate or Gate()
        self.block, self.capacity = block, int(seconds * relay.sr)
        self.ring: Ring | None = None
        self.proc: mp.Process | None = None
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.error: str | None = None
        self.level = 0.0
        self.lost = 0.0                                 # seconds the studio was too busy to collect (should stay 0)

    def start(self) -> None:
        self.ring = Ring(self.capacity, self.relay.channels)
        self.proc = _spawn(_recorder_main, (self.ring.name, self.capacity, self.relay.channels, self.relay.sr, self.device_name, self.block))
        self.error = _wait_ready(self.ring, self.proc) or None
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._collect, daemon=True)
        self.thread.start()

    def _collect(self) -> None:
        from . import priority
        priority.high_thread()
        read = 0
        while not self.stop_flag.is_set():
            time.sleep(0.02)
            ring = self.ring
            if ring is None:
                return
            if ring.h[FAILED]:
                self.error = ring.error()
            n = int(ring.h[W]) - read
            if n <= 0:
                continue
            if n > ring.capacity:                       # fell a whole ring behind: skip what was overwritten
                self.lost += (n - ring.capacity) / self.relay.sr
                read += n - ring.capacity
                n = ring.capacity
            frames = ring.get(read, n)
            read += n
            self.level = 0.9 * self.level + 0.1 * float(np.abs(frames).max()) / 32768.0
            if self.gate.admit(frames):
                self.relay.write(frames)

    def stop(self) -> None:
        self.stop_flag.set()
        if self.ring is not None:
            self.ring.h[STOP] = 1
        if self.proc is not None:
            self.proc.join(timeout=3)
            if self.proc.is_alive():
                self.proc.kill()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.ring is not None:
            self.ring.close()
        self.ring = self.proc = self.thread = None


class ProcessOutput:
    """Drop-in for local_output.LocalOutput: the playing happens in its own process.

    The studio keeps the ring filled a second ahead of what is playing. Position
    is what the player process has actually handed the device.
    """

    PIECE_S, BUFFER_S, LEAD_S = 0.05, 0.3, 1.0

    def __init__(self, device_name: str | None, offset_s: float = 0.3, sr: int = 44100, channels: int = 2, seconds: float = 8.0):
        self.device_name, self._offset = device_name, float(offset_s)
        self.sr, self.channels, self.capacity = sr, channels, int(seconds * sr)
        self.ring: Ring | None = None
        self.proc: mp.Process | None = None
        self.stream = None
        self.base = 0
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self._error = ""

    @property
    def name(self) -> str:
        return "This PC" + (f": {self.device_name}" if self.device_name else " (default output)")

    @property
    def offset(self) -> float:
        return self._offset

    @property
    def error(self) -> str:
        return self._error or (self.ring.error() if self.ring is not None else "")

    def _ensure_player(self, sr: int, channels: int) -> None:
        if self.proc is not None and self.proc.is_alive() and (sr, channels) == (self.sr, self.channels):
            return
        self._close_player()
        self.sr, self.channels, self.capacity = sr, channels, int(8.0 * sr)
        self.ring = Ring(self.capacity, channels)
        piece = max(1, int(round(sr * self.PIECE_S / (sr // 100)))) * (sr // 100)     # whole 10 ms periods
        self.proc = _spawn(_player_main, (self.ring.name, self.capacity, channels, sr, self.device_name, piece, self.BUFFER_S))
        self._error = _wait_ready(self.ring, self.proc)

    def start_stream(self, stream) -> None:
        self._halt_feeder()
        self._ensure_player(stream.relay.sr, stream.relay.channels)
        self.stream = stream
        self.base = int(self.ring.h[W])
        self.ring.h[SKIP] = self.base                   # anything still queued from the old stream is dropped
        self.ring.h[PAUSED] = 0
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._feed, args=(stream,), daemon=True)
        self.thread.start()

    def _feed(self, stream) -> None:
        from . import priority
        priority.high_thread()                          # keeps the player's ring topped up: ahead of analysis
        ring, ch = self.ring, stream.relay.channels
        lead = int(self.LEAD_S * self.sr)
        while not self.stop_flag.is_set():
            if ring.h[PAUSED] or int(ring.h[W]) - int(ring.h[R]) >= lead:
                time.sleep(0.02)
                continue
            data = stream.read(int(0.1 * self.sr), timeout=0.2)
            if data is None:
                continue
            if not data:
                break
            ring.put(np.frombuffer(data, dtype=np.int16).reshape(-1, ch))

    def position(self) -> float:
        if self.ring is None:
            return 0.0
        return max(0, int(self.ring.h[R]) - self.base) / self.sr

    def pump(self) -> None:
        pass

    def pause(self) -> None:
        if self.ring is not None:
            self.ring.h[PAUSED] = 1

    def resume(self) -> None:
        if self.ring is not None:
            self.ring.h[PAUSED] = 0

    def _halt_feeder(self) -> None:
        self.stop_flag.set()
        if self.stream is not None:
            self.stream.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.thread = None

    def _close_player(self) -> None:
        if self.ring is not None:
            self.ring.h[STOP] = 1
        if self.proc is not None:
            self.proc.join(timeout=3)
            if self.proc.is_alive():
                self.proc.kill()
        if self.ring is not None:
            self.ring.close()
        self.ring = self.proc = None

    def stop(self) -> None:
        self._halt_feeder()
        self._close_player()
        self.stream = None
