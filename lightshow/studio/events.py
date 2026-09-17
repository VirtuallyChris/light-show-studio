"""Per-song musical events: the things that make a track sound like itself.

The studio has the whole file up front, so it can pull apart what a live
microphone never could. The aim is not to react to everything, it is to find
the *signature* parts of a track and let the lights cling to those:

  lead    a wide synth, saw lead or horn line. Taken from the stereo side
          channel (left minus right), which cancels anything panned dead
          centre, so lead vocals and kick largely drop out and the wide
          instruments remain. Each hit carries a pitch class.
  hook    the motif that repeats most: found by comparing every bar of the
          song against every other and keeping the one with the most twins.
          Its return is the moment a show should mark.
  fill    stretches where drum hits suddenly get much denser than the song's
          norm: fills, breaks and drum solos.
  ghost   percussion landing between the beats rather than on them.
  bass    bass note starts, with their pitch class.

Each stream is [time, strength 0..1, pitch class]; `fills` and `hook` are
[start, end] ranges. Continuous 0..1 curves come too, so an effect can stay
lit only while an instrument is actually sounding.
"""

from __future__ import annotations

import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _peaks(env, sr, hop, wait_s, sensitivity):
    """Peak pick against a local baseline.

    A loud chorus and a quiet verse have wildly different onset levels, so a
    single threshold for the whole song either floods one or misses the other.
    Subtracting a running median first puts every part of the track on equal
    terms.
    """
    import librosa
    from scipy.ndimage import median_filter

    if env.size < 8 or not np.isfinite(env).any():
        return np.array([], dtype=int)
    env = np.nan_to_num(env)
    span = max(3, int(2.0 * sr / hop) | 1)                 # ~2 seconds, odd length
    local = median_filter(env, size=span, mode="nearest")
    resid = np.maximum(0, env - local)
    wait = max(1, int(wait_s * sr / hop))
    delta = sensitivity * float(np.percentile(resid[resid > 0], 60)) if (resid > 0).any() else 0.0
    return librosa.util.peak_pick(resid, pre_max=wait, post_max=wait, pre_avg=wait * 4,
                                  post_avg=wait * 4, delta=delta, wait=wait)


def _pitch_classes(S, freqs, frames, lo, hi):
    band = (freqs >= lo) & (freqs <= hi)
    if not band.any() or len(frames) == 0:
        return np.zeros(len(frames), dtype=int)
    sub, fsub = S[band], freqs[band]
    out = []
    for f in frames:
        col = sub[:, int(np.clip(f, 0, sub.shape[1] - 1))]
        peak = fsub[int(np.argmax(col))]
        out.append(int(round(12 * np.log2(max(peak, 1e-6) / 440.0))) % 12)
    return np.array(out, dtype=int)


def _curve(env, sr, hop, out_times):
    import librosa
    t = librosa.frames_to_time(np.arange(len(env)), sr=sr, hop_length=hop)
    v = np.interp(out_times, t, env)
    return np.clip(v / (np.percentile(v, 97) + 1e-9), 0, 1)


def _level(S, freqs, lo, hi, sr, hop, out_times, smooth_s=0.25):
    """How loudly a band is sounding, smoothed.

    An onset envelope spikes and collapses every frame, so a light driven by one
    flickers. A glow needs the sustained level of the band instead.
    """
    m = (freqs >= lo) & (freqs <= hi)
    if not m.any():
        return np.zeros(len(out_times))
    e = S[m].mean(axis=0)
    span = max(1, int(smooth_s * sr / hop))
    e = np.convolve(e, np.ones(span) / span, mode="same")
    return _curve(e, sr, hop, out_times)


def _band_onsets(S, freqs, lo, hi, sr, hop, wait_s, sens):
    import librosa
    m = (freqs >= lo) & (freqs <= hi)
    if not m.any():
        return np.array([]), np.array([]), np.array([], dtype=int)
    env = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S[m], ref=np.max), sr=sr, hop_length=hop)
    idx = _peaks(env, sr, hop, wait_s, sens)
    return env, idx, idx


def find_hook(y_side, sr, beats, phase, hop):
    """The bar-long motif that comes back most often, and every time it returns.

    Bars are compared on timbre and harmony together, so the same riff played
    with the same sound matches even if the mix around it changes.
    """
    import librosa

    bars = [beats[i] for i in range(phase, len(beats) - 4, 4)]
    if len(bars) < 8:
        return None
    frames = librosa.time_to_frames(np.array(bars), sr=sr, hop_length=hop)
    chroma = librosa.feature.chroma_cqt(y=y_side, sr=sr, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=y_side, sr=sr, hop_length=hop, n_mfcc=12)
    feats = []
    for f in frames:
        a, b = int(f), int(f + (frames[1] - frames[0] if len(frames) > 1 else 8))
        a, b = max(0, a), min(chroma.shape[1], b)
        if b - a < 2:
            return None
        v = np.concatenate([chroma[:, a:b].mean(axis=1), mfcc[1:, a:b].mean(axis=1) / 50.0])
        feats.append(v / (np.linalg.norm(v) + 1e-9))
    F = np.array(feats)
    sim = F @ F.T
    np.fill_diagonal(sim, 0)
    # A motif only counts as a hook if it comes back often but is not simply what
    # every bar sounds like: matching more than half the song means it is the texture.
    thresh = max(0.93, float(np.percentile(sim, 99)))
    counts = (sim > thresh).sum(axis=1)
    order = np.argsort(-counts)
    best = None
    for cand in order:
        c = counts[cand]
        if c < 3:
            break
        if c <= 0.45 * len(bars):
            best = int(cand)
            break
    if best is None:
        return None
    hits = [i for i in range(len(bars)) if sim[best, i] > thresh or i == best]
    span = (bars[1] - bars[0]) if len(bars) > 1 else 2.0
    return {"length": round(float(span), 3),
            "times": [round(float(bars[i]), 3) for i in sorted(hits)]}


def find_fills(drum_times, duration, beats):
    """Stretches where the drums get much busier than usual: fills and solos."""
    if len(drum_times) < 12 or len(beats) < 8:
        return []
    period = float(np.median(np.diff(beats)))
    win = period * 4
    edges = np.arange(0, duration, win)
    counts = np.array([((drum_times >= a) & (drum_times < a + win)).sum() for a in edges])
    if counts.max() < 4:
        return []
    nz = counts[counts > 0]
    typical = float(np.median(nz)) if nz.size else 0.0
    busy = counts >= max(float(np.percentile(nz, 88)) if nz.size else 0.0, typical + 2)
    out, run = [], None
    for i, b in enumerate(busy):
        if b and run is None:
            run = i
        elif not b and run is not None:
            out.append([round(float(edges[run]), 3), round(float(edges[i]), 3)])
            run = None
    if run is not None:
        out.append([round(float(edges[run]), 3), round(float(min(duration, edges[-1] + win)), 3)])
    return [r for r in out if r[1] - r[0] >= win * 0.9]


def detect_events(path, beats, duration, phase=0, sr_target=22050, hop=512, feature_fps=30.0, audio=None) -> dict:
    """`audio` is (array, sr) already in memory, mono or (2, n); otherwise `path` is loaded."""
    import librosa

    out_times = np.arange(0, duration, 1.0 / feature_fps)
    beats = np.asarray(beats, dtype=float)
    period = float(np.median(np.diff(beats))) if len(beats) > 2 else 0.5

    if audio is not None:
        y, sr = np.asarray(audio[0], dtype=np.float32), int(audio[1])
    else:
        y, sr = librosa.load(path, sr=sr_target, mono=False)
    if y.ndim == 1:
        mid, side = y, y
        wide = False
    else:
        mid, side = (y[0] + y[1]) / 2, (y[0] - y[1]) / 2
        wide = float(np.sqrt(np.mean(side ** 2))) > 0.01 * float(np.sqrt(np.mean(mid ** 2)) + 1e-9)
    if not wide:                      # a mono-ish master: fall back to the harmonic part
        side = librosa.effects.harmonic(mid, margin=3.0)

    S_side = np.abs(librosa.stft(side, hop_length=hop))
    mono_h, mono_p = librosa.effects.hpss(mid)
    S_h, S_p = np.abs(librosa.stft(mono_h, hop_length=hop)), np.abs(librosa.stft(mono_p, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr)

    # wide instruments (synth lead, horns, stabs) - vocals and kick are centre and mostly gone
    # Deliberately sparse: a light that marks every note is a strobe, one that marks
    # the notable notes reads as following the instrument.
    env_lead, idx_lead, _ = _band_onsets(S_side, freqs, 300, 5000, sr, hop, 0.35, 3.2)
    env_bass, idx_bass, _ = _band_onsets(S_h, freqs, 35, 250, sr, hop, 0.30, 2.8)
    env_drum, idx_drum, _ = _band_onsets(S_p, freqs, 1200, 9000, sr, hop, 0.08, 1.8)

    streams = {}
    for name, env, idx, S, lo, hi in (("lead", env_lead, idx_lead, S_side, 300, 5000),
                                      ("bass", env_bass, idx_bass, S_h, 35, 250),
                                      ("drum", env_drum, idx_drum, S_p, 1200, 9000)):
        times = librosa.frames_to_time(idx, sr=sr, hop_length=hop)
        keep = (times > 0.05) & (times < duration)
        idx, times = idx[keep], times[keep]
        if len(idx):
            st = env[idx]
            st = np.clip(st / (np.percentile(st, 95) + 1e-9), 0, 1)
            pc = _pitch_classes(S, freqs, idx, lo, hi)
        else:
            st, pc = np.array([]), np.array([], dtype=int)
        streams[name] = (times, st, pc)

    # ghosts are the drum hits that land between the beats, not on them
    t, s, p = streams["drum"]
    if len(t):
        off = np.abs(((t - beats[0]) / period + 0.5) % 1.0 - 0.5)
        streams["ghost"] = (t[off > 0.17], s[off > 0.17], p[off > 0.17])
    else:
        streams["ghost"] = (np.array([]), np.array([]), np.array([], dtype=int))

    hook = find_hook(side, sr, beats, phase, hop)
    fills = find_fills(streams["drum"][0], duration, beats)

    def pack(name):
        t, s, p = streams[name]
        return [[round(float(a), 3), round(float(b), 2), int(c)] for a, b, c in zip(t, s, p)]

    return {
        "events": {k: pack(k) for k in ("lead", "bass", "ghost")},
        "fills": fills,
        "hook": hook,
        "curves": {
            "lead": [round(float(v), 3) for v in _level(S_side, freqs, 300, 5000, sr, hop, out_times)],
            "bass": [round(float(v), 3) for v in _level(S_h, freqs, 35, 250, sr, hop, out_times)],
            "drum": [round(float(v), 3) for v in _level(S_p, freqs, 1200, 9000, sr, hop, out_times, 0.15)],
        },
        "stereo": bool(wide),
    }


def summarise(ev: dict, duration: float) -> str:
    bits = []
    for name in ("lead", "bass", "ghost"):
        items = ev["events"].get(name) or []
        if items:
            notes: dict[int, int] = {}
            for _, _, pc in items:
                notes[pc] = notes.get(pc, 0) + 1
            bits.append(f"{name} {len(items)} ({len(items) / max(1, duration) * 60:.0f}/min, {NOTE_NAMES[max(notes, key=notes.get)]})")
    if ev.get("hook"):
        bits.append(f"hook x{len(ev['hook']['times'])}")
    if ev.get("fills"):
        bits.append(f"{len(ev['fills'])} fills")
    return "; ".join(bits)
