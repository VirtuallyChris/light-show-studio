"""Party output on this PC: the delayed music plays on one of the computer's own outputs.

Handy for trying the party on headphones, or with a single pair of speakers
plugged into the PC. Spotify must play on a *different* output that nobody
hears (the studio records that one); if both were the same device, the studio
would record its own delayed copy and feed it back in forever, so it refuses.

The player runs on its own thread and pulls the relay stream in small blocks.
Its position is simply how much audio has been handed to the device, minus
the device's own buffering (the offset), which is what the lights follow.

Windows feeds a shared-mode device in 10 ms steps, and a write that is not a
whole number of steps wastes the rest of one. Handing over 4096 frames at a
time played at 93% speed (stuttering, and lights drifting ahead); whole 10 ms
multiples play at exactly 100%, measured over 8 s. So audio goes out in 50 ms
pieces into a 300 ms device buffer. The device buffer is kept full, so what is
heard is about that far behind what was handed over: the default offset.
"""

from __future__ import annotations

import threading
import time

import numpy as np

PIECE_S = 0.05          # a whole number of Windows' 10 ms device periods
BUFFER_S = 0.3          # the device's own buffer: headroom if the analysis briefly holds the processor
DEFAULT_OFFSET_S = 0.3  # handed over to heard: about one full device buffer


def soundcard_player(device_name: str | None, sr: int, channels: int):
    """Open a player on a named output (or the default one). Split out so tests can stand in for it."""
    import soundcard as sc

    speaker = sc.get_speaker(device_name) if device_name else sc.default_speaker()
    return speaker.player(samplerate=sr, channels=channels, blocksize=int(sr * BUFFER_S))


class LocalOutput:
    def __init__(self, device_name: str | None, offset_s: float = DEFAULT_OFFSET_S, player_factory=soundcard_player):
        self.device_name, self._offset, self.player_factory = device_name, float(offset_s), player_factory
        self.stream = None
        self.played = 0                                  # frames handed to the device on the current stream
        self.sr = 44100
        self.paused = threading.Event()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.error = ""

    @property
    def name(self) -> str:
        return "This PC" + (f": {self.device_name}" if self.device_name else " (default output)")

    @property
    def offset(self) -> float:
        return self._offset

    def start_stream(self, stream) -> None:
        self._halt()
        self.stream, self.played, self.sr = stream, 0, stream.relay.sr
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._play, args=(stream,), daemon=True)
        self.thread.start()

    def _play(self, stream) -> None:
        ch, sr = stream.relay.channels, stream.relay.sr
        piece = max(1, int(round(sr * PIECE_S / (sr // 100)))) * (sr // 100)     # whole 10 ms periods
        have = bytearray()
        need = piece * ch * 2
        try:
            with self.player_factory(self.device_name, sr, ch) as player:
                while not self.stop_flag.is_set():
                    if self.paused.is_set():
                        time.sleep(0.02)                 # hold the stream where it is
                        continue
                    data = stream.read((need - len(have)) // (ch * 2), timeout=0.2)
                    if data is None:
                        continue
                    if not data:
                        break
                    have += data
                    if len(have) < need:
                        continue                         # only whole pieces go to the device
                    frames = np.frombuffer(bytes(have[:need]), dtype=np.int16).reshape(-1, ch).astype(np.float32) / 32768.0
                    del have[:need]
                    player.play(frames)                  # blocks while the device buffer is full: that paces it
                    self.played += len(frames)
        except Exception as e:  # noqa: BLE001 - shown in Settings; the party keeps going silently
            self.error = f"{type(e).__name__}: {e}"

    def position(self) -> float:
        return self.played / self.sr

    def pump(self) -> None:
        pass                                              # the player thread pulls for itself

    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    def _halt(self) -> None:
        self.stop_flag.set()
        if self.stream:
            self.stream.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.thread = None

    def stop(self) -> None:
        self._halt()
        self.stream = None
