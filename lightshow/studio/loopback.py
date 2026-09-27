"""Where the relay's audio comes from.

LoopbackCapture hears whatever this PC is playing on one of its outputs, the way
a cable from the headphone jack would, using Windows' own loopback recording.
Point Spotify at an output nobody is listening to (a muted device, a monitor
with no speakers, or a virtual cable) and the only place the music is heard is
the house speakers, late, by way of the relay.

FileCapture feeds a file instead. It exists so the whole of Jam mode can be run
and tested with no sound, no speakers and no Spotify.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

BLOCK = 2048


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
        self.dropped = 0

    def admit(self, frames: np.ndarray) -> bool:
        if not self.paused:
            return True
        if len(frames) and int(np.abs(frames).max()) > self.threshold:
            return True
        self.dropped += len(frames)
        return False


class LoopbackCapture:
    """Records one output device into the relay on background threads."""

    def __init__(self, relay, device_name: str | None = None, gate: Gate | None = None):
        self.relay, self.device_name, self.gate = relay, device_name, gate or Gate()
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self.stop_flag = threading.Event()
        self.threads: list[threading.Thread] = []
        self.error: str | None = None
        self.level = 0.0

    def start(self) -> None:
        self.stop_flag.clear()
        for fn in (self._record, self._pump):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stop_flag.set()

    def _record(self) -> None:
        try:
            import soundcard as sc

            name = self.device_name or sc.default_speaker().name
            mic = sc.get_microphone(name, include_loopback=True)
            with mic.recorder(samplerate=self.relay.sr, channels=self.relay.channels, blocksize=BLOCK) as rec:
                while not self.stop_flag.is_set():
                    data = rec.record(numframes=BLOCK)
                    try:
                        self.q.put_nowait(to_int16(data, self.relay.channels))
                    except queue.Full:
                        pass
        except Exception as e:  # noqa: BLE001 - reported to the UI, never fatal
            self.error = f"{type(e).__name__}: {e}"

    def _pump(self) -> None:
        """Keep capture time moving with the wall clock.

        Windows hands a loopback recorder nothing at all while the output is
        silent, so the recorder thread can sit blocked. The timeline must not:
        when no block has arrived for a while, silence of the right length is
        written in its place.
        """
        sr, block_s = self.relay.sr, BLOCK / self.relay.sr
        t0 = time.monotonic()
        written = 0                                      # frames put on the timeline, silence included
        margin = int(0.25 * sr)                          # how late a block may be before silence stands in for it
        while not self.stop_flag.is_set():
            try:
                frames = self.q.get(timeout=block_s * 3)
                # Audio that arrives after silence was already written for its time would push the
                # timeline ahead of the clock: keep only what is still in the future.
                due = int((time.monotonic() - t0) * sr) + margin
                extra = written + len(frames) - due
                if extra > 0:
                    frames = frames[min(extra, len(frames)):]
            except queue.Empty:
                behind = int((time.monotonic() - t0) * sr) - margin - written
                frames = np.zeros((max(0, behind), self.relay.channels), dtype=np.int16)
            written += len(frames)
            if len(frames):
                self.level = 0.9 * self.level + 0.1 * float(np.abs(frames).max()) / 32768.0
                if self.gate.admit(frames):
                    self.relay.write(frames)


class FileCapture:
    """Feeds audio from an array or file, in real time or as fast as the caller steps it."""

    def __init__(self, relay, audio: np.ndarray, gate: Gate | None = None):
        # audio: float (n,) or (n, ch) already at the relay's sample rate
        self.relay, self.gate = relay, gate or Gate()
        self.data = to_int16(np.asarray(audio, dtype=np.float32), relay.channels)
        self.pos = 0

    @classmethod
    def from_file(cls, relay, path: str, gate: Gate | None = None) -> "FileCapture":
        import librosa

        y, _ = librosa.load(path, sr=relay.sr, mono=False)
        return cls(relay, y.T if y.ndim == 2 else y, gate)

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
