"""Serve a song from an exact point in time, so a Cast speaker never has to seek.

A Cast receiver seeks by guessing which byte holds a given second. In a variable
bitrate MP3 that guess is wrong by anything up to a second and a half, and it
stays wrong for the rest of playback, so the lights drift by a different amount
every time you start from a different place. Measuring on real hardware, the
same speaker reported +121 ms when told to start at 20 s and +1250 ms at 95 s,
repeatably.

The cure is to take the seeking away from the speaker. This module walks the
file's own frame table, finds the byte where a given second truly begins, and
the studio serves the stream from there. The speaker only ever plays a file
from its beginning, so what is left is the speaker's own fixed output delay,
which one calibration measures once and keeps.
"""

from __future__ import annotations

import os
import struct

BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
SAMPLE_RATES = [44100, 48000, 32000, 0]
_cache: dict[tuple[str, float, int], list[tuple[int, float]]] = {}


def _id3_end(data: bytes) -> int:
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    return 10 + size


def mp3_frames(path: str) -> list[tuple[int, float]]:
    """Every MPEG-1 Layer III frame in the file as (byte offset, start time)."""
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    if key in _cache:
        return _cache[key]
    data = open(path, "rb").read()
    i, t, frames = _id3_end(data), 0.0, []
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        version, layer = (data[i + 1] >> 3) & 3, (data[i + 1] >> 1) & 3
        bri, sri, pad = (data[i + 2] >> 4) & 0xF, (data[i + 2] >> 2) & 3, (data[i + 2] >> 1) & 1
        if version != 3 or layer != 1 or bri in (0, 15) or sri == 3:
            i += 1
            continue
        rate, sr = BITRATES[bri] * 1000, SAMPLE_RATES[sri]
        frames.append((i, t))
        t += 1152 / sr
        i += 144 * rate // sr + pad
    _cache.clear()
    _cache[key] = frames
    return frames


def _wav_data_chunk(path: str) -> tuple[int, int, int] | None:
    """(first audio byte, bytes per second, total audio bytes), or None if unreadable."""
    with open(path, "rb") as fh:
        if fh.read(4) != b"RIFF" or fh.read(8)[4:] != b"WAVE":
            return None
        rate = None
        while True:
            head = fh.read(8)
            if len(head) < 8:
                return None
            cid, size = head[:4], struct.unpack("<I", head[4:])[0]
            if cid == b"fmt ":
                fmt = fh.read(size)
                rate = struct.unpack("<I", fmt[8:12])[0]          # byte rate
                fh.seek(size - len(fmt), os.SEEK_CUR)
            elif cid == b"data":
                return (fh.tell(), rate or 0, size) if rate else None
            else:
                fh.seek(size + (size & 1), os.SEEK_CUR)


def slice_at(path: str, start: float) -> tuple[int, float]:
    """Byte offset to start streaming from, and the time that byte truly represents.

    Falls back to (0, 0.0) for formats this cannot index, which keeps the caller
    honest: it then knows it is streaming the whole file from the beginning.
    """
    start = max(0.0, float(start))
    ext = os.path.splitext(path)[1].lower()
    if start < 0.001:
        return 0, 0.0
    if ext == ".mp3":
        frames = mp3_frames(path)
        if not frames:
            return 0, 0.0
        lo, hi, best = 0, len(frames) - 1, 0
        while lo <= hi:                                   # last frame beginning at or before start
            mid = (lo + hi) // 2
            if frames[mid][1] <= start:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return frames[best]
    if ext == ".wav":
        info = _wav_data_chunk(path)
        if not info:
            return 0, 0.0
        first, byte_rate, size = info
        off = min(size, int(start * byte_rate))
        off -= off % 4                                     # keep sample frames intact
        return first + off, off / byte_rate
    return 0, 0.0


def exact_start(path: str, start: float) -> float:
    """The time the stream will really begin at when asked to start at `start`."""
    return slice_at(path, start)[1]


def header_for(path: str, offset: int) -> bytes:
    """Bytes to send before a sliced body so the stream is still a valid file.

    MP3 frames stand alone, so nothing is needed. A WAV needs its RIFF header
    repeated with the shortened length, or the receiver reads a broken file.
    """
    if offset <= 0 or os.path.splitext(path)[1].lower() != ".wav":
        return b""
    info = _wav_data_chunk(path)
    if not info:
        return b""
    first, _rate, size = info
    remaining = max(0, size - (offset - first))
    head = bytearray(open(path, "rb").read(first))
    head[first - 4:first] = struct.pack("<I", remaining)          # data chunk length
    head[4:8] = struct.pack("<I", (first - 8) + remaining)        # RIFF length
    return bytes(head)


def total_duration(path: str) -> float:
    """Length of the file in seconds, read from its own frame table."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        frames = mp3_frames(path)
        return frames[-1][1] + 1152 / 44100 if frames else 0.0
    if ext == ".wav":
        info = _wav_data_chunk(path)
        return info[2] / info[1] if info and info[1] else 0.0
    return 0.0
