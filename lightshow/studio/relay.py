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


def _fade_gain(fade: tuple, f: int) -> float:
    """A fade's (from frame, frames, gain from, gain to) gain at frame f."""
    s, n, g0, g1 = fade
    k = min(1.0, max(0.0, (f - s) / n))
    return g0 + (g1 - g0) * k * k * (3 - 2 * k)


def _fade_pcm(data: bytes, f0: int, n: int, fade: tuple) -> bytes:
    """n frames of int16 audio, the first at frame f0, through a fade."""
    s, length, g0, g1 = fade
    k = np.clip((f0 + np.arange(n) - s) / length, 0.0, 1.0)
    g = (g0 + (g1 - g0) * k * k * (3 - 2 * k)).astype(np.float32)
    x = np.frombuffer(data, dtype=np.int16).reshape(n, -1).astype(np.float32) * g[:, None]
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


class Relay:
    def __init__(self, sr: int = 44100, channels: int = 2, capacity_s: float = 600.0, clock=time.monotonic):
        self.sr, self.channels, self.clock = int(sr), int(channels), clock
        self.capacity = int(capacity_s * sr)
        self.buf = np.zeros((self.capacity, channels), dtype=np.int16)
        self.written = 0                       # frames captured so far (absolute index of the next frame)
        self.cond = threading.Condition()
        self.last_write_at = clock()
        self.closed = False
        # Each song's volume (loudness.py): [capture frame, gain] from where each song begins, every stream
        # applying it as it reads, so a song is evened out on Cast, on this PC, and after any restart alike.
        self.levels: list[list] = []
        # The music turned down for a while (a party mode): (from capture frame, frames, gain from, gain to),
        # a fade on the capture timeline, so every stream (a skip, a restart) plays it the same; the speakers' own
        # volume is left alone (they chime on every change)
        self.duck: tuple = (0, 1, 1.0, 1.0)
        # Spotify paused and played again (a doorbell): what it played after the pause was asked for (its last
        # moment, its fade) is kept from the streams until the seam is mended (see hold)
        self.held_from: int | None = None
        self.seam: int | None = None
        self.on_seam = None                    # (what was done, the audio either side of the seam) -> None

    # -- song levels ------------------------------------------------------------------
    def set_level(self, at_capture_time: float, gain: float) -> None:
        """From this capture time on, the music plays at `gain` (a song's start). A level set for the same
        change a moment before (a song's end foreseen, then heard) is replaced."""
        f = int(at_capture_time * self.sr)
        with self.cond:
            keep = [e for e in self.levels if e[0] < f - 2 * self.sr]
            old = self.written - self.capacity                   # what fell out of the buffer: its last level is all that matters
            while len(keep) > 1 and keep[1][0] <= old:
                keep.pop(0)
            self.levels = keep + [[f, float(gain)]]

    def level_at(self, frame: int) -> float:
        g = 1.0
        for f, v in self.levels:
            if f > frame:
                break
            g = v
        return g

    def set_duck(self, at_capture_time: float, seconds: float, to: float) -> None:
        """From this capture time the music fades to `to` (1.0: as it was) over `seconds`, from the gain it
        has there (turned around mid-fade, from where it got to)."""
        f = int(at_capture_time * self.sr)
        with self.cond:
            self.duck = (f, max(1, int(seconds * self.sr)), self.duck_at(f), float(to))

    def duck_at(self, frame: int) -> float:
        return _fade_gain(self.duck, frame)

    # -- capture side -----------------------------------------------------------------
    def write(self, frames: np.ndarray) -> None:
        """Append captured frames, shape (n, channels), int16."""
        n = len(frames)
        if n == 0:
            return
        if n > self.capacity:
            frames, n = frames[-self.capacity:], self.capacity
        with self.cond:
            # Held, before Spotify is heard again, silence is not kept: Spotify says it plays a moment before it does,
            # and the capture let through what it heard meanwhile, so the seam was made on silence and the room heard
            # the music stop for a split second, a few seconds after a doorbell (the user)
            if self.held_from is not None and self.seam is None and int(np.abs(frames).max()) <= self.SEAM_QUIET:
                return
            if self.held_from is not None:
                if self.seam is None and self.clock() - self.last_write_at > self.SEAM_GAP_S:
                    self.seam = self.written                  # Spotify plays again: the first of it
            self._put(frames)
            self.last_write_at = self.clock()
            if self.held_from is not None:
                if self.seam is not None and self.written - self.seam >= int(self.SEAM_POST_S * self.sr):
                    self._mend()
                elif self.seam is None and self.written - self.held_from > int(self.HOLD_MAX_S * self.sr):
                    self.held_from = None                     # it never paused (or never went quiet): let it all go
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

    # -- a pause's seam ------------------------------------------------------------------
    # Pausing Spotify and playing it again leaves a seam in the capture. Spotify goes on for a moment after the
    # pause is asked for, then starts again from where it says it stopped, which can be a little before what it
    # last played: heard twice, a split-second jitter (the user, a few seconds after a doorbell, as the room
    # reached the seam). The speakers read right up to the newest audio, so the mending is done before they get
    # it: from hold() on, the streams are kept short of what Spotify plays last; once SEAM_POST_S of it playing
    # again is in, the start of that is looked for in what came before the seam, and what would be heard twice is
    # left out.
    SEAM_GAP_S = 0.3          # no capture for this long: Spotify is paused (silence never reaches the relay)
    SEAM_QUIET = 60           # (as loopback.Gate: below this a block is silence)
    SEAM_POST_S = 0.6         # how much of the music back is looked through
    HOLD_MAX_S = 3.0          # captured past the hold without a pause: it is let go
    SEAM_MAX_S = 1.0          # the most a mend leaves out
    MATCH_W_S = 0.2           # the start of the music back, looked for in what came before the seam...
    MATCH_SKIP_S = 0.05       # ...clear of any fade as it starts again
    MATCH_BEFORE_S = 0.15     # what Spotify played this close to the seam may be fading as it paused
    MATCH_MIN = 0.97          # the same audio again; found in two places (a loop, the same bar) it is left alone

    def hold(self) -> None:
        """Spotify is being paused: what is captured from here on is kept from the streams until the seam is
        mended (or let go, if Spotify never paused)."""
        with self.cond:
            if self.held_from is None:
                self.held_from, self.seam = self.written, None

    def readable(self) -> int:
        """How far the streams may read (caller holds the lock)."""
        return self.written if self.held_from is None else min(self.written, self.held_from)

    def _mono(self, f0: int, f1: int) -> np.ndarray:
        return self._slice(f0, f1).astype(np.float64).mean(axis=1)

    def _mend(self) -> None:
        """Leave out what Spotify played twice either side of the seam; nothing found, the seam is joined with
        a few milliseconds' fade each way (no click). Caller holds the lock."""
        from scipy.signal import correlate

        sr, s, h = self.sr, self.seam, self.held_from
        self.held_from = self.seam = None
        w, a, before = int(self.MATCH_W_S * sr), int(self.MATCH_SKIP_S * sr), int(self.MATCH_BEFORE_S * sr)
        p0 = s - int(self.SEAM_MAX_S * sr) - w
        info = {"cut_s": 0.0, "match": 0.0, "gap_ms": 0.0}
        oldest = max(0, self.written - self.capacity)
        around = self._slice(p0, self.written).copy() if p0 >= oldest else None     # (as captured, to listen to)
        # The silence either side: the end of Spotify's last block as it stopped, the start of its first as it
        # played again (17 to 36 ms at three doorbells: the music dropped out for that long, the jitter the user still
        # heard). Left out, so the music before meets the music back; before the seam only what no stream has had.
        lo = max(h, s - int(0.1 * sr), oldest)
        quiet_end = s
        if lo < s:
            loud = np.nonzero(np.abs(self._slice(lo, s)).max(axis=1) > self.SEAM_QUIET)[0]
            quiet_end = lo + (int(loud[-1]) + 1 if len(loud) else 0)
        head = np.abs(self._slice(s, min(self.written, s + int(0.1 * sr)))).max(axis=1)
        loud = np.nonzero(head > self.SEAM_QUIET)[0]
        quiet_start = s + (int(loud[0]) if len(loud) else 0)
        if quiet_start > quiet_end:
            tail = self._slice(quiet_start, self.written).copy()
            self.written = quiet_end
            self._put(tail)
            info["gap_ms"] = round((quiet_start - quiet_end) / sr * 1000, 1)
            s = quiet_end
        end = self.written
        if p0 >= oldest and end - s >= a + w:
            tmpl, pre = self._mono(s + a, s + a + w), self._mono(p0, s)  # the music back, looked for before the seam
            tmpl = tmpl - tmpl.mean()
            tn = float(np.sqrt((tmpl ** 2).sum()))
            if tn > 1e-3 * 32768 * np.sqrt(w) and len(pre) > w:
                c = correlate(pre, tmpl, mode="valid", method="fft")
                cs, cs2 = np.concatenate([[0.0], np.cumsum(pre)]), np.concatenate([[0.0], np.cumsum(pre ** 2)])
                seg_sum, seg_sq = cs[w:] - cs[:-w], cs2[w:] - cs2[:-w]
                ncc = c / (tn * np.sqrt(np.maximum(seg_sq - seg_sum ** 2 / w, 1e-9)))
                m = int(np.argmax(ncc))
                info["match"] = round(float(ncc[m]), 3)
                others = ncc.copy()
                others[max(0, m - int(0.02 * sr)):m + int(0.02 * sr)] = -1.0
                k = p0 + m                                   # post frame s + a is pre frame k: d frames heard twice
                d = s + a - k
                # kept from before the seam: up to just short of Spotify's fade as it paused, and no further than
                # what it played twice (from there the music back carries on)
                cut = max(h, min(s - before, k), k - a)
                if ncc[m] >= self.MATCH_MIN and others.max() < self.MATCH_MIN and cut + d < end:
                    tail = self._slice(cut + d, end).copy()
                    self.written = cut
                    self._put(tail)
                    info["cut_s"] = round(d / sr, 3)
        if not info["cut_s"]:
            f = max(1, int(0.002 * sr))                      # (no click, and no dip to hear)
            for f0, ramp in ((s - f, np.linspace(1.0, 0.0, f)), (s, np.linspace(0.0, 1.0, f))):
                if f0 >= max(h, self.written - self.capacity) and f0 + f <= self.written:
                    for k in range(f):
                        i = (f0 + k) % self.capacity
                        self.buf[i] = (self.buf[i] * ramp[k]).astype(np.int16)
        if self.on_seam is not None:
            try:
                self.on_seam(info, around)
            except Exception:  # noqa: BLE001 - a report only
                pass

    def _put(self, frames: np.ndarray) -> None:
        """Write frames at the head (caller holds the lock)."""
        n = len(frames)
        i = self.written % self.capacity
        first = min(n, self.capacity - i)
        self.buf[i:i + first] = frames[:first]
        if first < n:
            self.buf[:n - first] = frames[first:]
        self.written += n

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

    # How far ahead of real time a speaker may pull. Its buffer is what carries it through a Wi-Fi
    # dropout: at 4 s a longer hiccup emptied it and the music stopped until the speaker came back.
    BURST_S = 20.0

    def __init__(self, relay: Relay, start_time: float, pace: bool = True):
        self.relay, self.pace = relay, pace
        self.cursor = max(int(start_time * relay.sr), relay.written - relay.capacity, 0)
        self.sent = 0                                   # stream frames handed out
        self.segments: list[list[int]] = []             # [stream_frame, capture_frame or -1 for silence, length]
        self.gaps: list[list[int]] = []                 # [at_capture_frame, silence_frames_left]
        self.gain: tuple | None = None                  # (from stream frame, frames, gain from, gain to): a fade
        self.cut: dict | None = None                    # a cue's song played into the stream first (see insert)
        self.lev = relay.level_at(self.cursor)          # the song level being applied (glides to a new song's)
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

    def fade(self, seconds: float, to: float, start: float | None = None) -> None:
        """Fade what the stream hands out from here (a cue's fade): to 0 for out, to 1 for in, from the
        level it is at now (or start). The speakers hear it once they reach this point of the stream."""
        with self.relay.cond:
            g0 = self._gain_at(self.sent) if start is None else float(start)
            self.gain = (self.sent, max(1, int(seconds * self.relay.sr)), g0, float(to))

    def insert(self, pcm: np.ndarray, after: int) -> None:
        """Play other audio (a cue's song, int16 frames) before the stream's own: the song alone, to its very
        end, then the music from where it held, fading in from silence over `after` frames (the user: let it
        finish fully, then fade back in; the songs overlapping each other's ends was a mess)."""
        with self.relay.cond:
            self.cut = {"pcm": pcm, "pos": 0, "after": max(0, int(after))}

    def _read_cut(self, max_frames: int) -> bytes:
        c = self.cut
        pcm, pos = c["pcm"], c["pos"]
        n = max(1, min(max_frames, len(pcm) - pos))
        self._note(-1, n)                                # (none of the music: capture_time_at says so)
        c["pos"] = pos + n
        if c["pos"] >= len(pcm):
            self.cut = None
            if c["after"]:
                self.gain = (self.sent, c["after"], 0.0, 1.0)     # the music after it, from silence
        return pcm[pos:pos + n].tobytes()

    def _gain_at(self, f: int) -> float:
        return 1.0 if self.gain is None else _fade_gain(self.gain, f)

    def _faded(self, data: bytes, n: int) -> bytes:
        if self.gain is None:
            return data
        s, length, _, g1 = self.gain
        if self.sent >= s + length and g1 >= 1.0:
            self.gain = None                            # faded all the way in: nothing to do any more
            return data
        return _fade_pcm(data, self.sent, n, self.gain)

    def _ducked(self, data: bytes, f0: int, n: int) -> bytes:
        """The music from capture frame f0 at the relay's duck (Relay.duck): untouched while it is at 1."""
        s, length, g0, g1 = self.relay.duck
        if g0 == g1 == 1.0 or (g1 == 1.0 and f0 >= s + length):
            return data
        return _fade_pcm(data, f0, n, self.relay.duck)

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
            if self.cut is not None:                        # a cue's song plays first
                return self._read_cut(max_frames)
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
            avail = r.readable() - self.cursor
            if avail <= 0:
                r.cond.wait(timeout)
                avail = r.readable() - self.cursor
                if avail <= 0 and self.pace and self.sent > (r.clock() - self.started_at) * r.sr:
                    return None                                 # ahead of real time: the speaker has plenty, wait
                if avail <= 0:                                  # nothing new and the speaker is due: keep it fed
                    n = min(max_frames, int(0.1 * r.sr))
                    self._note(-1, n)
                    return self._silence(n)
            n = min(max_frames, avail)
            if self.gaps:
                n = min(n, max(1, self.gaps[0][0] - self.cursor))
            data = self._faded(self._ducked(self._leveled(r._slice(self.cursor, self.cursor + n), self.cursor), self.cursor, n), n)
            self._note(self.cursor, n)
            self.cursor += n
            return data

    LEVEL_GLIDE_S = 0.25                                # a new song's level is reached in about a second
    CEILING = 32767 * 10 ** (-0.3 / 20)                 # the boosted music's peaks stay under -0.3 dBFS

    def _leveled(self, x: np.ndarray, f0: int) -> bytes:
        """Frames from capture frame f0 at their songs' levels (Relay.levels), gliding from the level the
        stream was at, and a boosted song's rare peaks held under the ceiling by a limiter that looks a
        moment ahead and lets go over 40 ms (it touches at most 1 in 100 of a song's 10 ms stretches)."""
        r, n = self.relay, len(x)
        if not r.levels and abs(self.lev - 1.0) < 1e-4:
            return x.tobytes()
        fr = np.array([e[0] for e in r.levels], dtype=np.int64)
        gv = np.array([1.0] + [e[1] for e in r.levels])
        tgt = gv[np.searchsorted(fr, np.arange(f0, f0 + n), side="right")]
        if np.all(np.abs(tgt - self.lev) < 1e-4):
            g = np.full(n, self.lev)
        else:
            from scipy.signal import lfilter
            a = float(np.exp(-1.0 / (self.LEVEL_GLIDE_S * r.sr)))
            g, _ = lfilter([1 - a], [1, -a], tgt, zi=[a * self.lev])
        self.lev = float(g[-1])
        if np.all(np.abs(g - 1.0) < 1e-4):
            return x.tobytes()
        y = x.astype(np.float32) * g[:, None].astype(np.float32)
        if g.max() > 1.0:
            peak = np.abs(y).max(axis=1)
            need = np.minimum(1.0, self.CEILING / np.maximum(peak, 1.0))
            if need.min() < 1.0:
                from scipy.ndimage import minimum_filter1d, uniform_filter1d
                ahead = minimum_filter1d(need, size=max(3, int(0.004 * r.sr)) | 1)
                env = uniform_filter1d(minimum_filter1d(need, size=max(3, int(0.03 * r.sr)) | 1), size=max(3, int(0.01 * r.sr)))
                y *= np.minimum(env, ahead)[:, None].astype(np.float32)
        return np.clip(y, -32768, 32767).astype(np.int16).tobytes()

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
