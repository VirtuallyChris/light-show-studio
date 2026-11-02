"""Synthetic dance tracks for testing, so no real music is needed.

Each has a kick on every beat, hats, a bassline, a stereo-wide lead, and a clear
shape: quiet intro, verse, buildup, drop, breakdown, second drop, outro. The
exact times of the drops are returned so a test can ask whether the analysis
found them, and how far ahead of the room it did.
"""

import numpy as np


def make_song(bpm=128.0, sr=44100, seed=1, bars=(4, 8, 4, 8, 4, 8, 4)):
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    names = ["intro", "verse", "buildup", "drop", "breakdown", "drop", "outro"]
    total_beats = sum(bars) * 4
    n = int(total_beats * beat * sr)
    L, R = np.zeros(n, dtype=np.float32), np.zeros(n, dtype=np.float32)
    t_kick = np.arange(int(0.18 * sr)) / sr
    kick = (np.sin(2 * np.pi * (55 + 90 * np.exp(-t_kick * 28)) * t_kick) * np.exp(-t_kick * 16)).astype(np.float32)
    hat = (rng.standard_normal(int(0.04 * sr)) * np.exp(-np.arange(int(0.04 * sr)) / (0.008 * sr))).astype(np.float32)
    root = [55.0, 61.74, 65.41, 73.42][seed % 4]
    scale = [1, 9 / 8, 6 / 5, 4 / 3, 3 / 2, 8 / 5, 9 / 5]

    def put(buf, sig, at, gain):
        i = int(at * sr)
        j = min(len(buf), i + len(sig))
        if i < len(buf):
            buf[i:j] += sig[: j - i] * gain

    sections, drops, b = [], [], 0
    for name, nb in zip(names, bars):
        b0, b1 = b, b + nb * 4
        sections.append((name, b0 * beat, b1 * beat))
        if name == "drop":
            drops.append(b0 * beat)
        for k in range(b0, b1):
            t = k * beat
            prog = (k - b0) / max(1, b1 - b0)
            level = {"intro": 0.25, "verse": 0.6, "buildup": 0.5 + 0.4 * prog, "drop": 1.0, "breakdown": 0.3, "outro": 0.3}[name]
            if name not in ("breakdown", "intro"):
                put(L, kick, t, 0.9 * level)
                put(R, kick, t, 0.9 * level)
            if name in ("verse", "drop", "buildup"):
                offs = (0.5,) if name != "buildup" else tuple(np.arange(0, 1, 0.5 / (1 + int(prog * 3))))
                for h in offs:
                    put(L, hat, t + h * beat, 0.25 * level)
                    put(R, hat, t + h * beat, 0.25 * level)
            if name in ("intro", "verse", "drop", "breakdown"):     # a bass note per beat
                f = root * scale[(k // 2 + seed) % 7]
                tt = np.arange(int(beat * 0.9 * sr)) / sr
                bass = ((np.sign(np.sin(2 * np.pi * f * tt)) * 0.5 + np.sin(2 * np.pi * f * tt)) * np.exp(-tt * 3)).astype(np.float32)
                g = 0.35 * level if name not in ("breakdown", "intro") else 0.12
                put(L, bass, t, g)
                put(R, bass, t, g)
            if name == "drop" and (k - b0) % 4 in (0, 1, 3):       # the lead riff, out of phase so it lives in the sides
                f = root * 8 * scale[((k - b0) * 3 + seed) % 7]
                tt = np.arange(int(beat * 0.45 * sr)) / sr
                saw = ((2 * ((f * tt) % 1) - 1) * np.exp(-tt * 6)).astype(np.float32)
                put(L, saw, t, 0.4)
                put(R, saw, t, -0.4)
        b = b1
    stereo = np.stack([L, R], axis=1)
    stereo = stereo / (np.abs(stereo).max() + 1e-9) * 0.9
    return {"audio": stereo.astype(np.float32), "sr": sr, "bpm": bpm, "duration": n / sr, "sections": sections, "drops": drops}
