"""An endless MP3 stream from PCM blocks, for the Cast speakers.

Raw audio (WAV) is 1.4 Mbit/s; a Cast group's leader receives it and sends it on
over Wi-Fi to every other speaker, so on a busy network the stream stalls. MP3 at
320 kbit/s is under a quarter of that and sounds the same on these speakers.

One encoder runs for the whole stream (starting a new one per block would put a
click and a gap at every seam). libsndfile writes into an in-memory file, and
whatever it has appended since the last call is handed out.
"""

from __future__ import annotations

import io

import numpy as np

BITRATE_MODE = "CONSTANT"


class _Sink(io.RawIOBase):
    """A file libsndfile can write into, that remembers only what has not been handed out yet."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0                                     # absolute position libsndfile thinks it is at
        self.base = 0                                    # absolute position of buf[0]

    def writable(self):
        return True

    def seekable(self):
        return True

    def readable(self):
        return False

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.base + len(self.buf) + offset
        return self.pos

    def write(self, b):
        b = bytes(b)
        i = self.pos - self.base
        if i < 0:                                        # a rewrite of what was already sent (only at close): drop it
            skip = min(len(b), -i)
            b, i = b[skip:], 0
            self.pos += skip
        if i > len(self.buf):
            self.buf.extend(b"\0" * (i - len(self.buf)))
        self.buf[i:i + len(b)] = b
        self.pos += len(b)
        return len(b) + (0 if i >= 0 else 0)

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.base += len(self.buf)
        self.buf.clear()
        return out


class Mp3Encoder:
    """Feed int16 frames (n, channels); get MP3 bytes back as they are produced."""

    def __init__(self, sr: int = 44100, channels: int = 2, kbps: int = 320):
        import soundfile as sf
        self.sink = _Sink()
        self.sf = sf.SoundFile(self.sink, mode="w", samplerate=sr, channels=channels, format="MP3",
                               subtype="MPEG_LAYER_III", compression_level=1.0 - min(320, kbps) / 320.0,
                               bitrate_mode=BITRATE_MODE)

    def encode(self, frames: np.ndarray) -> bytes:
        if len(frames):
            self.sf.write(frames)
        return self.sink.take()

    def close(self) -> bytes:
        try:
            self.sf.close()
        except Exception:  # noqa: BLE001
            pass
        return self.sink.take()
