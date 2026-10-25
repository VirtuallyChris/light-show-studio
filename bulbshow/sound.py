"""A show's sound, looped forever for a Cast speaker.

The editor decodes whatever file is dropped on it (mp3, m4a, wav...) in the browser and uploads
plain 16-bit WAV, so this side only ever reads WAV. A speaker is handed one endless WAV stream that
plays the sound over and over with no gap; the stream's own clock, which the speaker reports,
then tells exactly where in the loop the room is.
"""

from __future__ import annotations

import wave

import numpy as np

from lightshow.studio.relay import wav_stream_header


class Sound:
    def __init__(self, pcm: np.ndarray, sr: int):
        self.pcm = pcm if pcm.ndim == 2 else pcm[:, None]      # (frames, channels) int16
        self.sr = int(sr)

    @property
    def channels(self) -> int:
        return self.pcm.shape[1]

    @property
    def duration(self) -> float:
        return len(self.pcm) / self.sr

    @classmethod
    def load(cls, path: str) -> "Sound":
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError("the sound must be 16-bit WAV")
            ch, sr = w.getnchannels(), w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return cls(data.reshape(-1, ch).copy(), sr)


class LoopReader:
    """Reads the sound round and round from `start` seconds in, at `gain` (the show's volume, applied
    in the stream so the speaker's own volume is never touched: Google speakers chime on every change)."""

    def __init__(self, sound: Sound, start: float = 0.0, gain: float = 1.0):
        self.sound = sound
        n = len(sound.pcm)
        self.pos = int(round(start * sound.sr)) % n if n else 0
        self.gain = gain
        self.sent = 0                                       # frames handed out so far

    def header(self) -> bytes:
        return wav_stream_header(self.sound.sr, self.sound.channels)

    def read(self, frames: int) -> bytes:
        pcm, n = self.sound.pcm, len(self.sound.pcm)
        if n == 0:
            return b""
        parts, want = [], frames
        while want > 0:
            take = min(want, n - self.pos)
            parts.append(pcm[self.pos:self.pos + take])
            self.pos = (self.pos + take) % n
            want -= take
        out = np.concatenate(parts) if len(parts) > 1 else parts[0]
        g = max(0.0, min(1.0, float(self.gain)))
        if g < 0.999:
            out = (out.astype(np.float32) * g).astype(np.int16)
        self.sent += frames
        return np.ascontiguousarray(out, dtype="<i2").tobytes()


def write_wav(path: str, pcm: np.ndarray, sr: int) -> None:
    pcm = pcm if pcm.ndim == 2 else pcm[:, None]
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(np.ascontiguousarray(pcm, dtype="<i2").tobytes())
