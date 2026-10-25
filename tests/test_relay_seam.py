"""A Spotify pause's seam in the relay (Relay.hold / _mend): what it plays twice either side is left out before the
speakers get it. Synthetic audio only: nothing plays."""

import unittest

import numpy as np

from lightshow.studio.relay import Relay

SR = 8000


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def music(seconds, seed=1):
    """Noise-like 'music' (no repeating beat), shaped so every stretch is its own."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 6000, int(seconds * SR)).clip(-30000, 30000)
    return np.stack([x, x * 0.8], axis=1).astype(np.int16)


def feed(relay, clock, frames, block=0.02):
    n = int(block * SR)
    for i in range(0, len(frames), n):
        relay.write(frames[i:i + n])
        clock.t += block


def read_all(stream):
    """Everything the speaker may have now (past it, a stream would keep it fed with silence)."""
    out = []
    while stream.relay.readable() > stream.cursor:
        b = stream.read(10 ** 6, timeout=0)
        out.append(np.frombuffer(b, dtype=np.int16).reshape(-1, 2))
    return np.concatenate(out) if out else np.zeros((0, 2), dtype=np.int16)


class TestSeam(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.relay = Relay(sr=SR, channels=2, capacity_s=60, clock=self.clock)
        self.said = []
        self.relay.on_seam = lambda info, around: self.said.append(info)
        self.song = music(10)

    def test_what_spotify_plays_twice_is_left_out(self):
        r, song = self.relay, self.song
        feed(r, self.clock, song[:3 * SR])                         # the music
        s = r.open_stream(0.0, pace=False)
        self.assertEqual(len(read_all(s)), 3 * SR)                 # the speaker is right at the newest audio
        r.hold()                                                   # a doorbell: Spotify asked to pause
        feed(r, self.clock, song[3 * SR:int(3.5 * SR)])            # it plays on for half a second...
        self.assertEqual(len(read_all(s)), 0)                      # ...kept from the speaker
        self.clock.t += 8.0                                        # paused (silence never reaches the relay)
        feed(r, self.clock, song[int(3.2 * SR):5 * SR])            # again, from 0.3 s before where it stopped
        got = read_all(s)
        self.assertEqual(self.said[0]["cut_s"], 0.3)
        self.assertGreaterEqual(self.said[0]["match"], 0.99)
        heard = np.concatenate([song[:3 * SR], got])
        np.testing.assert_array_equal(heard, song[:len(heard)])    # every moment once, in order
        self.assertGreater(len(heard), int(4.5 * SR))

    def test_silence_before_spotify_is_heard_again_is_not_the_seam(self):
        """Spotify says it plays a moment before it does: the gate let the silence through and the seam was made on
        it (the room heard the music stop for a split second, a few seconds after a doorbell)."""
        r, song = self.relay, self.song
        feed(r, self.clock, song[:3 * SR])
        s = r.open_stream(0.0, pace=False)
        read_all(s)
        r.hold()
        feed(r, self.clock, song[3 * SR:int(3.5 * SR)])
        self.clock.t += 8.0                                        # paused
        feed(r, self.clock, np.zeros((int(0.4 * SR), 2), dtype=np.int16))   # "playing", not yet heard
        feed(r, self.clock, song[int(3.2 * SR):5 * SR])            # heard again, from 0.3 s back
        got = read_all(s)
        self.assertEqual(self.said[0]["cut_s"], 0.3)
        heard = np.concatenate([song[:3 * SR], got])
        np.testing.assert_array_equal(heard, song[:len(heard)])    # no silence, nothing twice

    def test_a_clean_restart_loses_nothing(self):
        r, song = self.relay, self.song
        feed(r, self.clock, song[:3 * SR])
        s = r.open_stream(0.0, pace=False)
        read_all(s)
        r.hold()
        feed(r, self.clock, song[3 * SR:int(3.5 * SR)])
        self.clock.t += 8.0
        feed(r, self.clock, song[int(3.5 * SR):5 * SR])            # it picks up exactly where it stopped
        got = read_all(s)
        self.assertEqual(self.said[0]["cut_s"], 0.0)
        self.assertEqual(len(got), 2 * SR)                         # nothing left out
        f = int(0.005 * SR)
        np.testing.assert_array_equal(got[:int(0.5 * SR) - f], song[3 * SR:int(3.5 * SR) - f])

    def test_the_same_beat_again_is_not_a_seam(self):
        """A loop: the same bar played again after the seam must not be taken for Spotify repeating itself."""
        r = self.relay
        bar = music(0.5, seed=3)
        loop = np.concatenate([bar] * 12)
        feed(r, self.clock, loop[:3 * SR])
        s = r.open_stream(0.0, pace=False)
        read_all(s)
        r.hold()
        feed(r, self.clock, loop[3 * SR:int(3.25 * SR)])
        self.clock.t += 8.0
        feed(r, self.clock, loop[int(3.25 * SR):5 * SR])
        got = read_all(s)
        self.assertEqual(len(got), 2 * SR)                         # nothing cut: it was never heard twice
        self.assertLessEqual(self.said[0]["cut_s"], 0.0)

    def test_a_pause_that_never_happens_is_let_go(self):
        r, song = self.relay, self.song
        feed(r, self.clock, song[:2 * SR])
        s = r.open_stream(0.0, pace=False)
        read_all(s)
        r.hold()                                                   # (not Premium: Spotify plays on)
        feed(r, self.clock, song[2 * SR:6 * SR])
        self.assertIsNone(r.held_from)
        self.assertEqual(len(read_all(s)), 4 * SR)
        self.assertEqual(self.said, [])


if __name__ == "__main__":
    unittest.main()
