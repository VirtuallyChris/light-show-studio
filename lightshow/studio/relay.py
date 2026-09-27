"""A delayed relay: audio goes in now and comes out for the speakers later.

Jam mode plays somebody else's stream (Spotify, running on this PC) through the
house speakers, but a little late. Everything the PC plays is captured into a
rolling buffer in memory, and the Cast speakers pull their audio from a point
some tens of seconds behind the newest sample. That gap is the whole trick: the
studio can study music the room has not heard yet, so the lights can know a
drop is coming instead of noticing it after the fact.

Nothing here touches the disk. The buffer is a fixed block of memory that
overwrites itself, and the only thing that leaves it is the delayed stream.

Two clocks matter and the code keeps them apart:

  capture time   seconds of audio captured since the relay started. Sample k was
                 captured at k / sr. This is the timeline tracks, beats and shows
                 are placed on.
  stream time    seconds of audio handed to the speakers on the current stream.
                 Usually capture time minus a constant, but inserted silence (to
                 grow the delay back) and restarts (after a skip) bend it, so a
                 stream carries a map back to capture time.
"""

from __future__ import annotations

import struct
import threading
import time

import numpy as np


def wav_stream_header(sr: int, channels: int) -> bytes:
    """A WAV header for a stream with no known end (sizes set to the maximum)."""
    byte_rate = sr * channels * 2
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sr, byte_rate, channels * 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF - 36))


class Relay:
    def __init__(self, sr: int = 44100, channels: int = 2, capacity_s: float = 600.0, clock=time.monotonic):
        self.sr, self.channels, self.clock = int(sr), int(channels), clock
        self.capacity = int(capacity_s * sr)
        self.buf = np.zeros((self.capacity, channels), dtype=np.int16)
        self.written = 0                       # frames captured so far (absolute index of the next frame)
        self.cond = threading.Condition()
        self.last_write_at = clock()
        self.closed = False

    # -- capture side -----------------------------------------------------------------
    def write(self, frames: np.ndarray) -> None:
        """Append captured frames, shape (n, channels), int16."""
        n = len(frames)
        if n == 0:
            return
        if n > self.capacity:
            frames, n = frames[-self.capacity:], self.capacity
        with self.cond:
            i = self.written % self.capacity
            first = min(n, self.capacity - i)
            self.buf[i:i + first] = frames[:first]
            if first < n:
                self.buf[:n - first] = frames[first:]
            self.written += n
            self.last_write_at = self.clock()
            self.cond.notify_all()

    @property
    def capture_time(self) -> float:
        return self.written / self.sr

    @property
    def oldest_time(self) -> float:
        return max(0, self.written - self.capacity) / self.sr

    def trim_tail_silence(self, max_seconds: float = 3.0, threshold: int = 40, keep_after: int | None = None) -> float:
        """Drop silence from the newest end of the buffer; returns seconds removed.

        Used when the source pauses: the second or so of nothing captured before
        the pause was noticed would otherwise be heard later as a hole. `keep_after`
        is a frame index that must not be crossed (frames a reader may already hold).
        """
        with self.cond:
            limit = max(self.written - int(max_seconds * self.sr), keep_after or 0, self.written - self.capacity)
            k = self.written
            block = max(16, self.sr // 100)               # 10 ms steps
            while k - block >= limit:
                seg = self._slice(k - block, k)
                if int(np.abs(seg).max()) > threshold:
                    break
                k -= block
            removed = self.written - k
            self.written = k
            return removed / self.sr

    def _slice(self, f0: int, f1: int) -> np.ndarray:
        """Frames [f0, f1) as a contiguous array. Caller holds the lock and has checked the range."""
        i0, n = f0 % self.capacity, f1 - f0
        if i0 + n <= self.capacity:
            return self.buf[i0:i0 + n]
        return np.concatenate([self.buf[i0:], self.buf[:n - (self.capacity - i0)]])

    def frames(self, t0: float, t1: float, pad: bool = False) -> np.ndarray:
        """int16 frames between two capture times, clipped to what is still held.

        With `pad`, time before the oldest frame held (before the recording began,
        say) comes back as silence, so the result still starts at t0.
        """
        with self.cond:
            want0 = int(np.floor(t0 * self.sr))
            f0 = max(want0, self.written - self.capacity, 0)
            f1 = min(int(t1 * self.sr), self.written)
            x = self._slice(f0, f1).copy() if f1 > f0 else np.zeros((0, self.channels), dtype=np.int16)
        if pad and f0 > want0 and f1 > want0:
            x = np.concatenate([np.zeros((min(f0, f1) - want0, self.channels), dtype=np.int16), x])
        return x

    def audio(self, t0: float, t1: float, sr_out: int = 22050, mono: bool = True, pad: bool = False) -> np.ndarray:
        """Float audio for analysis: mono (n,) or stereo (2, n), resampled to sr_out."""
        from math import gcd

        from scipy.signal import resample_poly

        x = self.frames(t0, t1, pad=pad).astype(np.float32) / 32768.0
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32) if mono else np.zeros((2, 0), dtype=np.float32)
        x = x.mean(axis=1) if mono else x[:, :2].T
        if sr_out != self.sr:
            g = gcd(sr_out, self.sr)
            x = resample_poly(x, sr_out // g, self.sr // g, axis=-1).astype(np.float32)
        return np.ascontiguousarray(x)

    def open_stream(self, start_time: float, pace: bool = True) -> "RelayStream":
        return RelayStream(self, start_time, pace)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class RelayStream:
    """One continuous stream to the speakers, starting at a capture time.

    Reads never pass the newest captured frame. If the reader catches the writer
    the stream carries on with silence rather than stalling the speaker, and the
    delay simply grows back by however long that lasted.
    """

    BURST_S = 4.0     # how far ahead of real time a speaker may pull, so its own buffer cannot drain ours

    def __init__(self, relay: Relay, start_time: float, pace: bool = True):
        self.relay, self.pace = relay, pace
        self.cursor = max(int(start_time * relay.sr), relay.written - relay.capacity, 0)
        self.sent = 0                                   # stream frames handed out
        self.segments: list[list[int]] = []             # [stream_frame, capture_frame or -1 for silence, length]
        self.gaps: list[list[int]] = []                 # [at_capture_frame, silence_frames_left]
        self.started_at = relay.clock()
        self.closed = False

    def header(self) -> bytes:
        return wav_stream_header(self.relay.sr, self.relay.channels)

    def add_gap(self, at_capture_time: float, seconds: float) -> None:
        """Insert silence when the stream reaches a capture time (a song boundary).

        This is how the delay is grown back after a skip has emptied it: a few
        seconds of quiet between songs, each of which pushes the room further
        behind the capture head again.
        """
        self.gaps.append([int(at_capture_time * self.relay.sr), int(seconds * self.relay.sr)])
        self.gaps.sort()

    def _note(self, capture_frame: int, n: int) -> None:
        last = self.segments[-1] if self.segments else None
        if last and ((capture_frame < 0 and last[1] < 0) or (capture_frame >= 0 and last[1] >= 0 and last[1] + last[2] == capture_frame)):
            last[2] += n
        else:
            self.segments.append([self.sent, capture_frame, n])
        self.sent += n

    def read(self, max_frames: int, timeout: float = 0.5) -> bytes | None:
        """Up to max_frames of int16 audio, waiting briefly for new capture if needed.

        b"" means the stream is over; None means nothing is due yet, ask again.
        """
        r = self.relay
        if self.closed or r.closed:
            return b""
        if self.pace:   # a speaker fills its buffer as fast as the network allows; hold it near real time
            allowed = int((r.clock() - self.started_at + self.BURST_S) * r.sr) - self.sent
            if allowed <= 0:
                time.sleep(min(timeout, 0.05))
                return None
            max_frames = min(max_frames, allowed)
        with r.cond:
            # a gap that falls at the cursor goes out first
            if self.gaps and self.gaps[0][0] <= self.cursor:
                n = min(max_frames, self.gaps[0][1])
                self.gaps[0][1] -= n
                if self.gaps[0][1] <= 0:
                    self.gaps.pop(0)
                self._note(-1, n)
                return self._silence(n)
            if self.cursor < r.written - r.capacity:          # fell out of the buffer: jump to what is left
                self.cursor = r.written - r.capacity
            avail = r.written - self.cursor
            if avail <= 0:
                r.cond.wait(timeout)
                avail = r.written - self.cursor
                if avail <= 0:                                  # nothing new: keep the speaker fed
                    n = min(max_frames, int(0.1 * r.sr))
                    self._note(-1, n)
                    return self._silence(n)
            n = min(max_frames, avail)
            if self.gaps:
                n = min(n, max(1, self.gaps[0][0] - self.cursor))
            data = r._slice(self.cursor, self.cursor + n).tobytes()
            self._note(self.cursor, n)
            self.cursor += n
            return data

    def _silence(self, n: int) -> bytes:
        return bytes(n * self.relay.channels * 2)

    def capture_time_at(self, stream_seconds: float) -> float | None:
        """Which capture time the speaker is on when it has played this much of the stream.

        None while the stream is inside inserted silence.
        """
        f = int(stream_seconds * self.relay.sr)
        for s0, c0, n in reversed(self.segments):
            if f >= s0:
                if c0 < 0:
                    return None
                return (c0 + min(f - s0, n)) / self.relay.sr
        return self.segments[0][1] / self.relay.sr if self.segments and self.segments[0][1] >= 0 else None

    def close(self) -> None:
        self.closed = True
