"""A jam with no Spotify, no speakers and no sound.

Demo mode runs the real engine on synthetic songs, with a scripted stand-in for
Spotify that plays them one after another and can be paused and skipped like the
real thing. It is how Jam mode is tested end to end, and it lets you watch the
whole thing work in the studio before setting anything up.
"""

from __future__ import annotations

import threading
import time

from .jam_synth import make_song
from .loopback import FileCapture
from .spotify import NowPlaying, Track

SONGS = [(128.0, 1, "Neon Avenue"), (120.0, 2, "Low Tide"), (124.0, 3, "Glass Arcade"), (132.0, 4, "Overdrive")]


class DemoSpotify:
    """Plays the synthetic songs in order and answers like the Spotify client."""

    signed_in = True

    def __init__(self, relay, gate, clock=time.monotonic):
        self.relay, self.clock = relay, clock
        self.songs = []
        for bpm, seed, name in SONGS:
            s = make_song(bpm=bpm, sr=relay.sr, seed=seed)
            self.songs.append((s, Track(id=f"demo{seed:02d}", name=name, artists=["The Synthetics"], duration=s["duration"])))
        self.capture = FileCapture(relay, self.songs[0][0]["audio"], gate)
        self.index, self.playing, self.position = 0, False, 0.0
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    # -- the transport, as a phone would drive it -----------------------------------------
    def start(self) -> None:
        self.playing = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()

    def skip(self) -> None:
        with self.lock:
            self._load((self.index + 1) % len(self.songs))

    def pause(self) -> bool:
        self.playing = False
        return True

    def resume(self) -> bool:
        self.playing = True
        return True

    def _load(self, i: int) -> None:
        self.index, self.position = i, 0.0
        self.capture.load(self.songs[i][0]["audio"])

    def _run(self) -> None:
        step, last = 0.1, time.monotonic()
        while not self.stop_flag.is_set():
            time.sleep(max(0.0, last + step - time.monotonic()))
            last += step
            with self.lock:
                self.capture.step(step, silent=not self.playing)
                if self.playing:
                    self.position += step
                    if self.capture.finished:
                        self._load((self.index + 1) % len(self.songs))

    # -- what the engine asks ----------------------------------------------------------------
    def now_playing(self) -> NowPlaying:
        with self.lock:
            return NowPlaying(self.songs[self.index][1], self.position, self.playing, self.clock(), "Demo")

    def queue(self) -> list[Track]:
        with self.lock:
            return [self.songs[(self.index + k) % len(self.songs)][1] for k in range(1, len(self.songs))]
