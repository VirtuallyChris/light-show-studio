"""Where the relay's audio comes from.

The real recording is audio_io.ProcessCapture. FileCapture feeds audio from
memory instead. It exists so the whole of the party can be run and tested with no
sound, no speakers and no Spotify.
"""

from __future__ import annotations

import numpy as np


def to_int16(x: np.ndarray, channels: int) -> np.ndarray:
    """Float frames (n, ch) in -1..1 to int16 frames with exactly `channels` channels."""
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] < channels:
        x = np.repeat(x[:, :1], channels, axis=1)
    return (np.clip(x[:, :channels], -1.0, 1.0) * 32767.0).astype(np.int16)


def list_outputs() -> list[dict]:
    """Outputs of this PC that can be listened in on."""
    try:
        import soundcard as sc
    except Exception as e:  # noqa: BLE001 - the library is optional
        return [{"error": f"soundcard is not installed ({e}); run: pip install soundcard"}]
    default = sc.default_speaker().name
    return [{"name": s.name, "default": s.name == default} for s in sc.all_speakers()]


class Gate:
    """Decides which captured blocks reach the relay.

    While the source is paused the capture is nothing but silence. Letting it
    through would push the music further from the room for no reason, and the
    pause would be heard twice: once now, once when that silence came round. So
    while `paused` is set, silent blocks are dropped; anything audible passes,
    which means a resume is caught by ear even before the API reports it.
    """

    def __init__(self, threshold: int = 60):
        self.paused = False
        self.threshold = threshold

    def admit(self, frames: np.ndarray) -> bool:
        if not self.paused:
            return True
        return bool(len(frames)) and int(np.abs(frames).max()) > self.threshold


class FileCapture:
    """Feeds audio from an array, in real time or as fast as the caller steps it."""

    def __init__(self, relay, audio: np.ndarray, gate: Gate | None = None):
        # audio: float (n,) or (n, ch) already at the relay's sample rate
        self.relay, self.gate = relay, gate or Gate()
        self.data = to_int16(np.asarray(audio, dtype=np.float32), relay.channels)
        self.pos = 0

    @property
    def finished(self) -> bool:
        return self.pos >= len(self.data)

    def step(self, seconds: float, silent: bool = False) -> None:
        """Deliver the next stretch of audio (or that much silence, as when the source is paused)."""
        n = int(seconds * self.relay.sr)
        if silent:
            frames = np.zeros((n, self.relay.channels), dtype=np.int16)
        else:
            frames = self.data[self.pos:self.pos + n]
            self.pos += len(frames)
            if len(frames) < n:
                frames = np.concatenate([frames, np.zeros((n - len(frames), self.relay.channels), dtype=np.int16)])
        if self.gate.admit(frames):
            self.relay.write(frames)

    def load(self, audio: np.ndarray) -> None:
        """Queue a different piece of audio to follow (the next song, or a skip)."""
        self.data = to_int16(np.asarray(audio, dtype=np.float32), self.relay.channels)
        self.pos = 0
