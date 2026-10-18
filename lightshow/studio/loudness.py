"""How loud a song is, and how much to turn it up or down so every song plays at about the same level.

Loudness is measured as the streaming services do it (ITU-R BS.1770: K-weighted, gated, in LUFS), from
the YouTube copy a song is learned from and again from the relay once it has played whole (what the
speakers really get). The correction is one gain for the whole song, never compression: a quiet song is
turned up as a whole, its loud and soft parts kept as they were. It is turned up only as far as its own
peaks allow: at least 99 in 100 of its 10 ms stretches stay under the ceiling untouched, and the rare
peaks above it lose 3 dB at most to the relay's limiter (relay.RelayStream). Loud songs are brought down
only gently (half of what they are over, 3 dB at most): the party's volume should not drop.
"""

from __future__ import annotations

import math

import numpy as np

TARGET_LUFS = -9.0          # where songs are brought to (modern masters are -6 to -9; older and gentler ones -12 to -18)
MAX_BOOST_DB = 8.0
MAX_TRIM_DB = 3.0
CEILING_DB = -0.3           # dBFS: what a boosted song's peaks are kept under
LIMIT_DB = 3.0              # the most the limiter may take off the rarest peaks
DEADBAND_DB = 0.5           # smaller corrections are not worth making


def _biquad(b, a, x):
    from scipy.signal import lfilter
    return lfilter(np.asarray(b, dtype=np.float64) / a[0], np.asarray(a, dtype=np.float64) / a[0], x, axis=0)


def k_weighted(x: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770's K-weighting (a high shelf at 1.5 kHz, a high pass at 38 Hz) for any sample rate."""
    w0 = 2 * math.pi * 1500.0 / sr
    A, alpha = 10 ** (4.0 / 40), math.sin(w0) / (2 * (1 / math.sqrt(2)))
    c, sa = math.cos(w0), 2 * math.sqrt(A) * alpha
    shelf_b = [A * ((A + 1) + (A - 1) * c + sa), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sa)]
    shelf_a = [(A + 1) - (A - 1) * c + sa, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sa]
    w0 = 2 * math.pi * 38.0 / sr
    c, alpha = math.cos(w0), math.sin(w0) / (2 * 0.5)
    hp_b, hp_a = [(1 + c) / 2, -(1 + c), (1 + c) / 2], [1 + alpha, -2 * c, 1 - alpha]
    return _biquad(hp_b, hp_a, _biquad(shelf_b, shelf_a, x))


def measure(x: np.ndarray, sr: int) -> dict | None:
    """{"lufs": integrated loudness, "p99": the 99th percentile of 10 ms peaks (dBFS), "peak": dBFS} of
    audio shaped (n,) or (n, channels): int16, or float in -1..1. None for silence or under 3 s."""
    x = np.asarray(x)
    x = x.astype(np.float64) / 32768.0 if x.dtype == np.int16 else x.astype(np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if len(x) < 3 * sr:
        return None
    # integrated loudness: 400 ms blocks every 100 ms, gated at -70 LUFS and 10 LU under the ungated mean
    z = k_weighted(x, sr) ** 2
    hop, blk = int(0.1 * sr), int(0.4 * sr)
    cs = np.concatenate([np.zeros((1, z.shape[1])), np.cumsum(z, axis=0)])
    starts = np.arange(0, len(z) - blk + 1, hop)
    ms = ((cs[starts + blk] - cs[starts]) / blk).sum(axis=1)       # each channel weighted 1 (left, right)
    with np.errstate(divide="ignore"):
        lk = -0.691 + 10 * np.log10(ms)
    keep = ms[lk > -70.0]
    if not len(keep):
        return None
    rel = -0.691 + 10 * math.log10(keep.mean()) - 10.0
    gated = ms[(lk > -70.0) & (lk > rel)]
    lufs = -0.691 + 10 * math.log10(gated.mean()) if len(gated) else rel + 10.0
    # peaks, 10 ms at a time
    w = max(1, int(0.01 * sr))
    peaks = np.abs(x[:len(x) // w * w]).max(axis=1).reshape(-1, w).max(axis=1)
    db = lambda v: 20 * math.log10(max(float(v), 1e-9))
    return {"lufs": round(lufs, 2), "p99": round(db(np.percentile(peaks, 99)), 2), "peak": round(db(peaks.max()), 2)}


def gain_db(level: dict, target: float = TARGET_LUFS) -> float:
    """The correction for a song measured as `level` (see measure): up as far as its peaks allow, down gently."""
    d = target - float(level["lufs"])
    if d > 0:
        g = min(d, MAX_BOOST_DB, CEILING_DB - float(level["p99"]), CEILING_DB - float(level["peak"]) + LIMIT_DB)
        g = max(0.0, g)
    else:
        g = max(0.5 * d, -MAX_TRIM_DB)
    return 0.0 if abs(g) < DEADBAND_DB else round(g, 2)
