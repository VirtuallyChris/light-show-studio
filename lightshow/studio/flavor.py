"""The things that make one kind of song different from another, for effects that only belong there.

- 808s: a bass hit that holds, low and long (hip-hop, trap), for a deep glow that dies away slowly.
- Hat rolls: the hi-hat (the drum track's highest band) at five or more hits a beat for at least
  half a beat, the trap roll, for ticks racing along the strip.
- Wobbles: the bass track's level pulsing steadily at a fixed fraction of the beat (dubstep, bass
  music), so the lights can pulse at the wobble's own speed rather than the beat's.
- A key change: the music moves up a semitone or two near the end and stays there (a pop song's
  last chorus), for the room's colors to lift with it.

All of them work on the separated tracks (drums, bass) or the full mix, never on anything written
to disk.
"""

from __future__ import annotations

import numpy as np


def _env(x: np.ndarray, hop: int) -> np.ndarray:
    m = len(x) // hop
    if m < 4:
        return np.zeros(0)
    return np.sqrt((x[: m * hop].reshape(m, hop) ** 2).mean(axis=1))


def find_808s(bass: np.ndarray, sr: int, bass_hits: list) -> list:
    """The bass hits that hold: low (under about 110 Hz) and still at half their peak 0.35 s later.
    Returns (time, strength 0..1, seconds held)."""
    import librosa
    if bass is None or not len(bass) or not bass_hits:
        return []
    hop = 256
    env = _env(bass, hop)
    if not len(env):
        return []
    fps = sr / hop
    S = np.abs(librosa.stft(bass.astype(np.float32), n_fft=2048, hop_length=hop))
    f = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = S[(f >= 25) & (f < 110)].sum(axis=0)
    high = S[(f >= 110) & (f < 400)].sum(axis=0)
    ref = float(np.percentile(env, 95)) + 1e-9
    out = []
    for t, strength in bass_hits:
        i = int(t * fps)
        if i >= len(env) - 2:
            continue
        seg = env[i: i + int(1.5 * fps)]
        peak_i = int(np.argmax(seg[: int(0.12 * fps) + 1]))
        peak = float(seg[peak_i])
        if peak < 0.35 * ref:
            continue
        held = 0
        while peak_i + held < len(seg) and seg[peak_i + held] >= 0.5 * peak:
            held += 1
        secs = held / fps
        j0, j1 = i, min(len(low), i + max(2, held))
        lowness = float(low[j0:j1].sum() / (low[j0:j1].sum() + high[j0:j1].sum() + 1e-9))
        if secs >= 0.35 and lowness >= 0.6:
            out.append((float(t), float(min(1.0, peak / ref)), round(secs, 2)))
    return out


def find_hat_rolls(drums: np.ndarray, sr: int, beats) -> list:
    """Stretches where the drum track's top band (5 kHz up) hits five or more times a beat for at least
    half a beat. Returns [start, end, hits per beat]."""
    import librosa
    beats = np.asarray(beats, dtype=float)
    if drums is None or not len(drums) or len(beats) < 8:
        return []
    hop = 128
    S = np.abs(librosa.stft(drums.astype(np.float32), n_fft=1024, hop_length=hop))
    f = librosa.fft_frequencies(sr=sr, n_fft=1024)
    band = S[f >= 5000].sum(axis=0)
    if not band.any():
        return []
    e = np.log1p(band / (np.percentile(band, 99) + 1e-9) * 100)
    flux = np.maximum(0.0, np.diff(e, prepend=e[:1]))
    fps = sr / hop
    pk = librosa.util.peak_pick(flux, pre_max=2, post_max=2, pre_avg=6, post_avg=6, delta=0.25 * float(np.percentile(flux, 99)), wait=int(0.035 * fps))
    times = pk / fps
    if len(times) < 8:
        return []
    period = float(np.median(np.diff(beats)))
    out, i = [], 0
    gap = period / 4.5                                   # closer than a sixteenth note: a roll
    while i < len(times) - 1:
        j = i
        while j + 1 < len(times) and times[j + 1] - times[j] <= gap:
            j += 1
        span = times[j] - times[i]
        if j - i >= 3 and span >= 0.5 * period:
            rate = (j - i) / span * period
            if rate >= 5:
                out.append([round(float(times[i]), 3), round(float(times[j]) + 0.05, 3), round(float(rate), 1)])
        i = j + 1
    return out


WOBBLE_STEPS = (0.5, 1, 1.5, 2, 3, 4, 6, 8)             # wobbles a beat: half notes to 32nds, and triplets


def find_wobbles(bass: np.ndarray, sr: int, beats) -> list:
    """Four-bar stretches where the bass track's level pulses steadily at one of WOBBLE_STEPS a beat,
    deeply (the quiet of each wobble well under its peak) and while the bass is loud. Neighbouring
    stretches at the same speed are joined. Returns [start, end, wobbles per beat]."""
    beats = np.asarray(beats, dtype=float)
    if bass is None or not len(bass) or len(beats) < 17:
        return []
    hop = max(1, sr // 200)                              # 200 frames a second: enough for 16 wobbles a second
    # the level over 25 ms (a whole cycle of the lowest bass note), every 5 ms
    sq = np.convolve(bass.astype(np.float64) ** 2, np.ones(int(0.025 * sr)) / int(0.025 * sr), mode="same")
    env = np.sqrt(sq[::hop])
    if len(env) < 64:
        return []
    fps = sr / hop
    loud = float(np.percentile(env, 90)) + 1e-9
    found = []
    for k in range(0, len(beats) - 17, 16):
        t0, t1 = beats[k], beats[k + 16]
        a, b = int(t0 * fps), int(t1 * fps)
        w = env[a:b]
        if len(w) < 64 or float(np.median(w)) < 0.25 * loud:
            continue
        p90, p10 = np.percentile(w, 90), np.percentile(w, 10)
        depth = (p90 - p10) / (p90 + 1e-9)
        if depth < 0.5:
            continue
        x = w - w.mean()
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
        freqs = np.fft.rfftfreq(len(x), 1 / fps)
        beat_hz = 16 / (t1 - t0)
        best, best_ratio = None, 0.0
        floor = float(np.median(spec[(freqs > 0.3) & (freqs < 20)]) + 1e-12)
        for m in WOBBLE_STEPS:
            hz = beat_hz * m
            if hz > 16:
                continue
            sel = np.abs(freqs - hz) <= max(0.06 * hz, freqs[1])
            if not sel.any():
                continue
            ratio = float(spec[sel].max()) / floor
            if ratio > best_ratio:
                best, best_ratio = m, ratio
        # a kick-like bass (one hit a beat) is a pulse, not a wobble: only faster than the beat counts
        if best is None or best < 1.5 or best_ratio < 25:
            continue
        # A wobble swells: its level rises smoothly over the wobble (a sine rises at about pi times
        # its swing per wobble). A bass playing separate notes at the same speed jumps up on every
        # note's attack, many times faster. So: how steep the steepest rises are, against the swing.
        per_wobble = (t1 - t0) / 16 / best * fps
        rise = np.percentile(np.maximum(0.0, np.diff(w)), 99) * per_wobble / (p90 - p10 + 1e-9)
        if rise <= 10.0:                                # a sine scores about 6 here; plucked notes 20 and up
            found.append([float(t0), float(t1), float(best)])
    out = []
    for t0, t1, m in found:
        if out and out[-1][2] == m and t0 - out[-1][1] < 0.5:
            out[-1][1] = t1
        else:
            out.append([t0, t1, m])
    return [[round(a, 3), round(b, 3), m] for a, b, m in out]


_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _key_of(chroma: np.ndarray) -> tuple[int, int, float]:
    """(tonic 0..11, 0 major / 1 minor, how clearly) by the Krumhansl key profiles."""
    v = chroma / (chroma.sum() + 1e-9)
    best = (0, 0, -2.0)
    for mode, prof in ((0, _MAJOR), (1, _MINOR)):
        for k in range(12):
            r = float(np.corrcoef(v, np.roll(prof, k))[0, 1])
            if r > best[2]:
                best = (k, mode, r)
    return best


def find_key_change(mid: np.ndarray, sr: int, duration: float) -> dict | None:
    """The song moving up a semitone or two in its last part and staying there: the key of every 8 s,
    then the point in the last 45% where the key after it (for at least 15 s, to the end) is the key
    before it (for at least 30 s) moved up 1 or 2 semitones, in the same mode. Returns {time, up} or None."""
    import librosa
    if mid is None or duration < 90:
        return None
    hop = 2048
    chroma = librosa.feature.chroma_cqt(y=mid.astype(np.float32), sr=sr, hop_length=hop)
    fps = sr / hop
    win = int(8 * fps)
    keys = []
    for i in range(0, chroma.shape[1] - win + 1, win):
        k, m, r = _key_of(chroma[:, i:i + win].sum(axis=1))
        keys.append((i / fps, k, m, r))
    if len(keys) < 8:
        return None
    for j in range(len(keys)):
        t = keys[j][0]
        if t < 0.55 * duration or duration - t < 15:
            continue
        before = [x for x in keys[:j] if x[3] > 0.5 and t - x[0] <= 60]
        after = [x for x in keys[j:] if x[3] > 0.5]
        if len(before) < 4 or len(after) < 2:
            continue
        kb = max(set((x[1], x[2]) for x in before), key=lambda km: sum(1 for x in before if (x[1], x[2]) == km))
        ka = max(set((x[1], x[2]) for x in after), key=lambda km: sum(1 for x in after if (x[1], x[2]) == km))
        share_b = sum(1 for x in before if (x[1], x[2]) == kb) / len(before)
        share_a = sum(1 for x in after if (x[1], x[2]) == ka) / len(after)
        up = (ka[0] - kb[0]) % 12
        if kb[1] == ka[1] and up in (1, 2) and share_b >= 0.6 and share_a >= 0.6 and keys[j][1:3] == ka:
            return {"time": round(float(t), 2), "up": int(up)}
    return None
