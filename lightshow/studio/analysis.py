"""Offline song analysis for the studio.

Given an audio file, produce everything the sequencer needs:

  bpm, beat grid (regular, precisely fitted), downbeats (bar starts)
  waveform peaks for drawing
  energy (RMS) and 12 log-spaced band energies at ~30 fps for reactive effects
  sections: intro / verse / buildup / drop / breakdown / outro, snapped to bars
  drops: the moments where the energy slams in

Everything is plain lists so it serialises to JSON.
"""

from __future__ import annotations

import json
import math
import os

import numpy as np

from .matching import match_features

SR = 22050
HOP = 512
FEATURE_FPS = 30.0
N_BANDS = 12
WAVE_PPS = 50  # waveform peaks per second


def analyze(path: str, cache_dir: str | None = None, use_model: bool = True) -> dict:
    import librosa

    y, sr = librosa.load(path, sr=SR, mono=True)
    return analyze_audio(y, sr, path=path, cache_dir=cache_dir, use_model=use_model)


def analyze_audio(y, sr: int = SR, stereo=None, path: str | None = None, cache_dir: str | None = None,
                  use_model: bool = False, events: bool = True, bpm_hint: float | None = None) -> dict:
    """The whole analysis, from audio already in memory.

    `analyze` is this plus loading a file. Jam mode calls it directly on audio it
    is relaying, which never touches the disk: `y` is mono at `sr`, `stereo` an
    optional (2, n) array at the same rate for the instrument detectors, and
    `bpm_hint` keeps a growing live analysis from flipping between half and
    double time from one pass to the next.
    """
    import librosa

    duration = float(len(y) / sr)
    if duration < 5:
        raise ValueError("song is too short to analyse")

    # --- tempo and beat grid --------------------------------------------------------
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    onset_times = librosa.frames_to_time(np.arange(len(onset)), sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=HOP, trim=False)
    tracked = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    bpm, offset = fit_grid(onset, onset_times, bpm_hint)
    period = 60.0 / bpm
    first = offset - period * math.floor(offset / period)  # first beat at or after 0
    beats = np.arange(first, duration, period)
    if bpm_hint is None:
        bpm, beats = fix_half_tempo(y, sr, bpm, beats)
    first = float(beats[0]) if len(beats) else first

    # --- frame features ---------------------------------------------------------------
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    mel = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=N_BANDS, fmin=40, fmax=11000)
    logmel = librosa.power_to_db(mel, ref=np.max)
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=HOP)
    out_times = np.arange(0, duration, 1.0 / FEATURE_FPS)
    energy = np.interp(out_times, frame_times, rms)
    energy = energy / (np.percentile(energy, 99.5) + 1e-9)
    bands = np.stack([np.interp(out_times, frame_times, logmel[b]) for b in range(N_BANDS)])
    lo = np.percentile(bands, 5, axis=1, keepdims=True)
    hi = np.percentile(bands, 99.5, axis=1, keepdims=True)
    bands = np.clip((bands - lo) / (hi - lo + 1e-9), 0, 1)

    # --- waveform peaks ---------------------------------------------------------------
    n_buckets = int(duration * WAVE_PPS)
    edges = np.linspace(0, len(y), n_buckets + 1).astype(int)
    peaks = np.array([np.abs(y[a:b]).max() if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])])
    peaks = peaks / (peaks.max() + 1e-9)

    # --- structure and downbeats -------------------------------------------------------
    phase, sections, drops = find_structure(y, sr, beats, rms, duration)
    downbeats = beats[phase::4]
    structure_source = "builtin"
    builtin_sections, builtin_drops = sections, drops
    if use_model and path:
        # SongFormer, when its environment is installed: better labels, mapped onto our grid
        from . import structure_model
        if structure_model.available():
            base = {"beats": [float(b) for b in beats], "downbeat_phase": phase, "duration": duration,
                    "energy": list(np.interp(np.arange(0, duration, 1.0 / FEATURE_FPS), frame_times, rms) / (np.percentile(rms, 99.5) + 1e-9)),
                    "feature_fps": FEATURE_FPS}
            segs = structure_model.run_model(path, cache_dir)
            mapped = structure_model.sections_from_model(segs, base, builtin_drops, builtin_sections) if segs else None
            if mapped:
                sections, drops = mapped
                structure_source = "songformer"

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    mood = song_mood(y, sr, energy, centroid)
    ev = {"events": {}, "fills": [], "hook": None, "curves": {}, "stereo": False}
    if events:
        try:
            from .events import detect_events
            ev = detect_events(path, [float(b) for b in beats], duration, phase, SR, HOP, FEATURE_FPS,
                               audio=(stereo if stereo is not None else y, sr) if path is None else None)
        except Exception as e:  # noqa: BLE001 - a show without flair is better than no show
            print(f"[events] skipped: {type(e).__name__}: {e}")

    return {
        "duration": duration,
        # the opening 45 seconds as band onsets: enough to find this song's 0:00 by ear in a jam
        "align_bands": [[round(float(v), 2) for v in row] for row in match_features(y[: int(45 * sr)], sr, HOP)],
        "align_fps": round(sr / HOP, 4),
        "mood": mood,
        "events": ev["events"],
        "fills": ev["fills"],
        "hook": ev["hook"],
        "curves": ev["curves"],
        "bpm": round(bpm, 3),
        "beat_offset": round(float(first), 4),
        "downbeat_phase": phase,
        "beats": [round(float(b), 4) for b in beats],
        "downbeats": [round(float(b), 4) for b in downbeats],
        "tracked_beats": [round(float(b), 4) for b in tracked],
        "feature_fps": FEATURE_FPS,
        "energy": [round(float(e), 4) for e in energy],
        "bands": [[round(float(v), 3) for v in row] for row in bands.T],
        "wave_pps": WAVE_PPS,
        "peaks": [round(float(p), 3) for p in peaks],
        "sections": sections,
        "drops": drops,
        "structure_source": structure_source,
        "builtin_sections": builtin_sections,
        "builtin_drops": builtin_drops,
    }


# Krumhansl-Schmuckler key profiles, used only to tell major from minor.
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def song_mood(y, sr, energy, centroid_hz) -> dict:
    """A one-line character sketch of the track, and the palette that suits it.

    One palette for the whole song reads as a deliberate look; switching family
    every section reads as a demo reel. Sections vary within the palette instead.
    """
    import librosa

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP).mean(axis=1)
    chroma = chroma / (chroma.sum() + 1e-9)
    best = (-2.0, "major", 0)
    for shift in range(12):
        rolled = np.roll(chroma, -shift)
        for name, prof in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            r = float(np.corrcoef(rolled, prof)[0, 1])
            if r > best[0]:
                best = (r, name, shift)
    _, mode, tonic = best
    bright = float(np.clip((np.median(centroid_hz) - 800) / 2600, 0, 1))
    drive = float(np.clip(np.mean(energy) * 1.6, 0, 1))
    warm = mode == "major"
    # pick a family from brightness, drive and mode; each is a distinct look
    if drive > 0.62 and bright > 0.55:
        family = ["Miami", "Neon", "Candy"] if warm else ["Cyan/Magenta", "Toxic", "Purple/Gold"]
    elif drive > 0.62:
        family = ["Fire", "Sunset"] if warm else ["Blood", "Purple/Gold"]
    elif bright > 0.6:
        family = ["Candy", "Ice", "Miami"] if warm else ["Ice", "Cyan/Magenta"]
    elif bright < 0.3:
        family = ["Sunset", "Fire"] if warm else ["Ocean", "Blood"]
    else:
        family = ["Neon", "Candy"] if warm else ["Purple/Gold", "Ocean"]
    # two songs of the same character in different keys should not look identical
    # rotate the family by the song's own fingerprint (key, brightness, drive) so two
    # tracks of similar character do not open on the same colour
    spin = (tonic + int(round(bright * 7)) + int(round(drive * 5))) % len(family)
    family = family[spin:] + family[:spin]
    return {"key": f"{NOTE_NAMES[tonic]} {mode}", "mode": mode, "brightness": round(bright, 3),
            "drive": round(drive, 3), "palette": family[0], "palettes": family}


def fit_grid(onset: np.ndarray, times: np.ndarray, hint: float | None = None) -> tuple[float, float]:
    """Precise constant tempo and beat phase from the onset envelope.

    Scores every candidate tempo by the magnitude of the onset envelope's
    Fourier component at that beat frequency (plus its 2nd and 4th harmonics,
    which sharpen the peak for kick/snare patterns), with a gentle prior
    around 125 bpm to settle half/double-time ambiguity. A coarse sweep over
    the whole range is refined to 0.005 bpm around the winner; the phase of
    the winning component gives the beat offset.
    """
    env = onset - onset.mean()

    def score(cands):
        freqs = cands / 60.0
        c1 = np.exp(-2j * np.pi * np.outer(freqs, times)) @ env
        c2 = np.exp(-2j * np.pi * np.outer(2 * freqs, times)) @ env
        c4 = np.exp(-2j * np.pi * np.outer(4 * freqs, times)) @ env
        prior = np.exp(-0.5 * ((np.log2(cands / 125.0)) / 0.5) ** 2)
        return (np.abs(c1) + 0.5 * np.abs(c2) + 0.25 * np.abs(c4)) * (0.6 + 0.4 * prior), c1

    # with a hint, only look near it: a live analysis must not jump an octave mid-song
    coarse = np.arange(65.0, 190.0, 0.1) if not hint else np.arange(hint * 0.96, hint * 1.04, 0.05)
    sc, _ = score(coarse)
    centre = float(coarse[int(np.argmax(sc))])
    fine = np.arange(centre - 1.0, centre + 1.0, 0.005)
    sf, c1 = score(fine)
    i = int(np.argmax(sf))
    bpm = float(fine[i])
    phase = float(-np.angle(c1[i]) / (2 * np.pi * fine[i] / 60.0))  # time of a beat
    period = 60.0 / bpm
    return round(bpm, 3), phase % period


def fix_half_tempo(y, sr, bpm, beats):
    """Halve the grid when the kick lands on only every other beat.

    Tempo trackers often lock onto eighth notes in tracks with a busy top end.
    The giveaway is that alternate beats carry no low end at all. Halving keeps
    the half of the grid the kicks are on, so a beat means what the song means
    and the bar lines have half as many places to go wrong.
    """
    import librosa

    if len(beats) < 16 or bpm < 120:
        return bpm, beats
    low = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=8, fmin=30, fmax=200).mean(axis=0)
    at_beat = low[np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, len(low) - 1)]
    even, odd = float(at_beat[0::2].mean()), float(at_beat[1::2].mean())
    strong, weak = max(even, odd), min(even, odd)
    if weak >= 0.3 * strong:
        return bpm, beats
    return bpm / 2, beats[(0 if even > odd else 1)::2]


def find_structure(y, sr, beats, rms, duration):
    """Downbeat phase, sections and drops from per-bar features.

    Cues, learned against hand-labelled dance/pop tracks:
      drop       loud AND full-spectrum: mids and highs light up together
                 (a verse can be just as loud, but its mids are sparse)
      breakdown  the sub bass disappears and the level falls
      buildup    the 4-16 bars right before a drop that are not a breakdown,
                 bounded by the nearest texture change (or 8 bars by default)
      verse      everything else with a groove; intro/outro by position
    """
    import librosa

    n_beats = len(beats)
    if n_beats < 32:
        return 0, [{"start": 0.0, "end": duration, "type": "verse", "energy": 1.0, "bars": max(1, n_beats // 4)}], []

    # --- per-beat features --------------------------------------------------------------
    mel_p = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=48, fmin=30, fmax=11000)
    mel = librosa.power_to_db(mel_p, ref=np.max)
    freqs = librosa.mel_frequencies(n_mels=48, fmin=30, fmax=11000)
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    n_frames = mel.shape[1]
    bf = np.unique(np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, n_frames - 1))
    # librosa.util.sync pads a leading column for the audio before the first beat
    # boundary; drop it so column k is beat k (otherwise everything lands one beat late)
    pad = 1 if bf[0] > 0 else 0
    sync = lambda x: librosa.util.sync(np.atleast_2d(x), bf, aggregate=np.mean)[:, pad:]
    mel_b = sync(mel)
    chroma_b = sync(chroma)
    n = min(mel_b.shape[1], chroma_b.shape[1], n_beats)
    mel_b, chroma_b = mel_b[:, :n], chroma_b[:, :n]
    lin = lambda rows: np.mean(np.power(10, mel_b[rows] / 10), axis=0)
    norm = lambda v: np.clip(v / (np.percentile(v, 98) + 1e-12), 0, 1.2)
    loud = norm(sync(rms)[0][:n])
    sub = norm(lin(freqs < 100))
    bassmid = norm(lin((freqs >= 100) & (freqs < 250)))
    full = (norm(lin((freqs >= 250) & (freqs < 1000))) + norm(lin((freqs >= 1000) & (freqs < 4000))) + norm(lin(freqs >= 5000))) / 3
    cent_b = sync(cent)[0][:n]
    cent_n = np.clip((cent_b - np.percentile(cent_b, 5)) / (np.percentile(cent_b, 95) - np.percentile(cent_b, 5) + 1e-9), 0, 1)
    peaks = librosa.util.peak_pick(onset, pre_max=3, post_max=3, pre_avg=3, post_avg=5, delta=0.1, wait=2)
    ptimes = librosa.frames_to_time(peaks, sr=sr, hop_length=HOP)
    hits = np.zeros(n)
    for i in range(n):
        t0 = beats[i]
        t1 = beats[i + 1] if i + 1 < n_beats else t0 + (beats[i] - beats[i - 1])
        hits[i] = ((ptimes >= t0) & (ptimes < t1)).sum()
    hits = norm(hits)

    # --- candidate boundaries (beats): timbre/harmony novelty + jumps in level, sub, fullness --
    feat = np.vstack([_z(mel_b), 2.0 * _z(chroma_b), 3.0 * _z(loud[None, :]), 3.0 * _z(sub[None, :]), 3.0 * _z(full[None, :])])
    k = int(np.clip(round(duration / 12.0), 3, 48))
    cand = {int(c) for c in librosa.segment.agglomerative(feat, min(k, n - 1))}
    jumps = {}
    for name, v, thr in (("loud", loud, 0.2), ("sub", sub, 0.3), ("full", full, 0.15)):
        j = np.zeros(n)
        for i in range(8, n - 4):
            j[i] = v[i:i + 4].mean() - v[i - 8:i].mean()
        for i in range(8, n - 4):
            if abs(j[i]) > thr and abs(j[i]) >= np.abs(j[max(0, i - 3):i + 4]).max():
                cand.add(i)
        jumps[name] = j
    cand.discard(0)

    # --- downbeat phase: where changes land, minus the snare on 2 and 4 ------------------
    low = mel_b[freqs < 250].mean(axis=0)
    mid = mel_b[(freqs > 150) & (freqs < 1200)].mean(axis=0)
    kick = norm(lin(freqs < 150))
    flux_low = np.maximum(0, np.diff(low, prepend=low[0]))
    onset_mid = np.maximum(0, np.diff(mid, prepend=mid[0]))
    bound_strength = np.zeros(n)
    for c in cand:
        bound_strength[c] = sum(abs(jumps[nm][c]) for nm in jumps) + 0.15 * float(np.linalg.norm(feat[:, c] - feat[:, c - 1])) / feat.shape[0]
    # Score the four candidate bar phases. The terms are on different scales per song, so each
    # is normalised across the four candidates first. Sections get snapped to bar lines, so the
    # phase should agree with where the section changes are; the kick marks downbeats; a rise in
    # the low end helps; the snare (mid) onset marks the backbeat, so it counts against.
    def phase_term(v, agg):
        t = np.array([agg(v[p::4]) for p in range(4)])
        return (t - t.min()) / (t.max() - t.min()) if t.max() > t.min() else np.zeros(4)
    score = (2.0 * phase_term(bound_strength, np.sum) + 2.0 * phase_term(kick, np.mean)
             + 0.5 * phase_term(flux_low, np.sum) - 0.5 * phase_term(onset_mid, np.sum))
    if os.environ.get("SF_PHASE_DEBUG"):
        import sys as _s
        print(f"[phase] bound {np.round(phase_term(bound_strength, np.sum), 2)}  kick {np.round(phase_term(kick, np.mean), 2)}"
              f"  flux {np.round(phase_term(flux_low, np.sum), 2)}  mid {np.round(phase_term(onset_mid, np.sum), 2)}"
              f"  -> score {np.round(score, 2)}", file=_s.stderr)
    phase = int(np.argmax(score))

    # --- per-bar features ------------------------------------------------------------------
    bar_starts = np.arange(phase, n, 4)
    n_bars = len(bar_starts)
    if n_bars < 6:
        return phase, [{"start": 0.0, "end": duration, "type": "verse", "energy": 1.0, "bars": n_bars}], []

    def per_bar(v):
        return np.array([v[bar_starts[i]:bar_starts[i + 1] if i + 1 < n_bars else n].mean() for i in range(n_bars)])
    B = {nm: per_bar(v) for nm, v in (("loud", loud), ("sub", sub), ("bassmid", bassmid), ("full", full), ("cent", cent_n), ("hits", hits))}
    full_ref = float(np.percentile(B["full"], 90)) + 1e-9
    act = 0.5 * B["cent"] + 0.3 * B["loud"] + 0.2 * B["bassmid"]   # what climbs through a buildup

    def to_bar(beat):
        return int(round((beat - phase) / 4.0))

    def strength(bar):
        beat = bar_starts[bar]
        return bound_strength[max(0, beat - 2):beat + 3].max() if bar > 0 else 0.0

    snapped = sorted({0} | {to_bar(c) for c in cand if 0 < to_bar(c) < n_bars})
    bounds = [0]
    for bb in snapped[1:]:
        if bb - bounds[-1] >= 4:
            bounds.append(bb)
        elif len(bounds) > 1 and strength(bb) > strength(bounds[-1]) and bb - bounds[-2] >= 4:
            bounds[-1] = bb
    weak = {to_bar(c) for c in cand if 0 < to_bar(c) < n_bars}      # every candidate, 4-bar snapped
    bar_time = [float(beats[bar_starts[i]]) for i in range(n_bars)] + [duration]

    def seg_stats(b0, b1):
        return {nm: float(v[b0:b1].mean()) for nm, v in B.items()}

    def is_drop(st):
        return st["loud"] >= 0.75 and st["full"] >= 0.55 * full_ref and (st["sub"] >= 0.4 or st["full"] >= 0.8 * full_ref)

    def quiet(st):
        return st["loud"] < 0.5 or (st["sub"] < 0.15 and st["loud"] < 0.65)

    segs = []
    for i, b0 in enumerate(bounds):
        b1 = bounds[i + 1] if i + 1 < len(bounds) else n_bars
        segs.append({"b0": b0, "b1": b1, **seg_stats(b0, b1)})
    for sg in segs:
        sg["type"] = "drop" if is_drop(sg) else None
    for i, sg in enumerate(segs):   # a short loud stretch right before a louder drop is buildup, not drop
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        if sg["type"] == "drop" and nxt is not None and nxt["type"] == "drop" and sg["b1"] - sg["b0"] <= 4 \
                and nxt["full"] - sg["full"] > 0.2 and sg["full"] < 0.7 * full_ref:
            sg["type"] = None
    for i, sg in enumerate(segs):
        if sg["type"]:
            continue
        sg["type"] = "breakdown" if (quiet(sg) and 0 < i < len(segs) - 1) else "verse"
    if segs[0]["type"] == "verse" and segs[0]["loud"] < 0.7:
        segs[0]["type"] = "intro"
    if len(segs) > 1 and segs[-1]["type"] in ("verse", "breakdown") and segs[-1]["loud"] < 0.7:
        segs[-1]["type"] = "outro"

    # --- buildups: the run into each drop, bounded by the nearest texture change -------------
    labels = np.empty(n_bars, dtype=object)
    segid = np.zeros(n_bars, dtype=int)
    for i, sg in enumerate(segs):
        labels[sg["b0"]:sg["b1"]] = sg["type"]
        segid[sg["b0"]:sg["b1"]] = i
    lead = 0
    while lead < min(4, n_bars - 4) and B["loud"][lead] < 0.45:
        lead += 1
    if lead > 0 and labels[0] != "intro":
        labels[:lead] = "intro"
    for d in range(1, n_bars):
        if labels[d] != "drop" or labels[d - 1] == "drop":
            continue
        # run of non-drop bars before the drop (at most 16)
        run0 = d
        while run0 > 0 and d - run0 < 16 and labels[run0 - 1] != "drop":
            run0 -= 1
        if d - run0 < 4:
            continue
        # nearest candidate boundary 4..16 bars back, else 8 bars
        L = next((d - c for c in sorted(weak | set(bounds), reverse=True) if 4 <= d - c <= 16 and c >= run0), min(8, d - run0))
        floor = float(act[run0:d].min())
        # a buildup often starts gently before the texture change: extend back while the
        # bars are lively, sit below the buildup's tail, and keep climbing toward the drop
        tail = float(act[d - L:d].mean())
        j = d - L - 1
        while (j >= max(run0, d - 16) and (B["loud"][j] >= 0.45 or act[j] > floor + 0.15)
               and act[j] < tail - 0.1 and act[j] < act[j + 1] - 0.02):   # genuinely climbing, bar over bar
            j -= 1
        L = int(np.clip(round((d - 1 - j) / 4.0) * 4, L, min(16, d - run0)))
        for j in range(d - L, d):
            if labels[j] == "drop":
                continue
            lively = B["loud"][j] >= 0.45 or act[j] > floor + 0.15
            if labels[j] != "breakdown" or lively:
                labels[j] = "buildup"

    # --- compress bars into sections ------------------------------------------------------
    def similar(i, j):
        return abs(segs[i]["loud"] - segs[j]["loud"]) < 0.2 and abs(segs[i]["full"] - segs[j]["full"]) < 0.2
    runs = [[0, 1]]
    for k in range(1, n_bars):
        if labels[k] != labels[k - 1] or (segid[k] != segid[k - 1] and labels[k] not in ("buildup", "drop") and not similar(segid[k - 1], segid[k])):
            runs.append([k, k + 1])
        else:
            runs[-1][1] = k + 1
    final = []
    for b0, b1 in runs:
        st = seg_stats(b0, b1)
        st.update({"b0": b0, "b1": b1, "type": labels[b0]})
        if final and b1 - b0 < 2 and labels[b0] != "intro":
            final[-1]["b1"] = b1
            final[-1].update(seg_stats(final[-1]["b0"], b1))
        else:
            final.append(st)
    segs = final
    if segs[0]["type"] == "verse" and segs[0]["loud"] < 0.7:
        segs[0]["type"] = "intro"
    if len(segs) > 1 and segs[-1]["type"] in ("verse", "breakdown") and segs[-1]["loud"] < 0.7:
        segs[-1]["type"] = "outro"

    # --- drops: every bar line where a drop section begins, or sub bass slams back inside one ----
    drops = []
    for i, sg in enumerate(segs):
        if sg["type"] != "drop":
            continue
        if i == 0 or segs[i - 1]["type"] != "drop":
            drops.append(sg["b0"])
        for bar in range(sg["b0"] + 4, sg["b1"] - 1):
            if B["sub"][bar - 2:bar].mean() < 0.3 and B["sub"][bar:bar + 2].mean() > 0.6 and bar - drops[-1] >= 8:
                drops.append(bar)
    drops = sorted(set(drops))

    out = []
    for i, sg in enumerate(segs):
        out.append({"start": round(bar_time[sg["b0"]], 3) if i else 0.0, "end": round(bar_time[sg["b1"]], 3),
                    "type": sg["type"], "energy": round(float(np.clip(sg["loud"], 0, 1.2)), 3), "bars": sg["b1"] - sg["b0"],
                    "bass": round(sg["sub"], 2), "hats": round(sg["full"], 2)})
    return phase, out, [round(bar_time[d], 3) for d in drops]


def _z(a: np.ndarray) -> np.ndarray:
    return (a - a.mean(axis=1, keepdims=True)) / (a.std(axis=1, keepdims=True) + 1e-9)


def analyze_cached(path: str, cache_dir: str) -> dict:
    """Analyse once per (path, size, mtime); cache the JSON next to the project."""
    os.makedirs(cache_dir, exist_ok=True)
    st = os.stat(path)
    key = f"{os.path.basename(path)}.{st.st_size}.{int(st.st_mtime)}.v15.json"
    cache = os.path.join(cache_dir, key)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)
    data = analyze(path, cache_dir)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data
