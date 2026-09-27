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
                  use_model: bool = False, events: bool = True, bpm_hint: float | None = None, segment_fn=None) -> dict:
    """The whole analysis, from audio already in memory.

    `analyze` is this plus loading a file. Jam mode calls it directly on audio it
    is relaying, which never touches the disk: `y` is mono at `sr`, `stereo` an
    optional (2, n) array at the same rate for the instrument detectors, and
    `bpm_hint` keeps a growing live analysis from flipping between half and
    double time from one pass to the next.

    Sections come from SongFormer alone: from the file at `path` when
    `use_model` is set, or from `segment_fn(y, sr)` for audio held in memory.
    Without it the song is one plain section and `structure_error` says why.
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

    # --- structure: SongFormer's sections on our bar lines -------------------------------
    from . import structure_model

    segs, structure_error = None, "no section model was asked"
    if use_model and path:
        segs, structure_error = structure_model.run_model(path, cache_dir)
    elif segment_fn is not None:
        try:
            segs = segment_fn(y, sr)
            structure_error = "" if segs else f"SongFormer failed: {structure_model.WORKER.last_error or 'no answer'}"
        except Exception as e:  # noqa: BLE001
            segs, structure_error = None, f"SongFormer failed: {type(e).__name__}: {e}"
    phase = find_downbeat_phase(y, sr, beats, structure_model.boundary_times(segs))
    downbeats = beats[phase::4]
    sections, drops = structure_model.sections_from_segments(segs or [], [float(b) for b in beats], phase, duration)
    structure_source = "songformer" if segs else "none"

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
        "structure_error": "" if segs else structure_error,
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
    # A missing kick alone is not enough: kick on 1 and 3 with the snare on 2 and 4 is the
    # ordinary pop and rock backbeat, and halving that turns a 125 bpm song into 62.8.
    # A grid that really counts double has almost nothing on the in-between beats; a
    # backbeat has a snare there at least as strong as on the kick beats.
    snare = librosa.onset.onset_strength_multi(y=y, sr=sr, hop_length=HOP, n_mels=64, channels=[12, 44])[0]
    fr = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, len(snare) - 1)
    # an onset peaks a frame or two after the hit, so look at a short window from each beat
    near = lambda frames: np.array([snare[max(0, f - 1):f + 4].max() for f in frames])
    weak_hits = near(fr[1::2] if even > odd else fr[0::2]).mean()
    strong_hits = near(fr[0::2] if even > odd else fr[1::2]).mean()
    # a backbeat is a real snare on the in-between beats: as strong as on the kick beats,
    # and clearly above the song's general level (so that silence cannot pass for one)
    if weak_hits >= 0.75 * strong_hits and weak_hits >= 1.2 * snare.mean():
        return bpm, beats
    return bpm / 2, beats[(0 if even > odd else 1)::2]


def find_downbeat_phase(y, sr, beats, boundaries: list[float] | None = None) -> int:
    """Which of every four beats starts a bar.

    Scored over the four possibilities: the kick drum marks downbeats, a rise in
    the low end helps, the snare on 2 and 4 counts against, and where SongFormer
    heard one part of the song give way to another counts for a lot, because
    sections change on bar lines. Each term is scaled across the four candidates
    first, since they sit on very different scales from song to song.
    """
    import librosa

    n_beats = len(beats)
    if n_beats < 8:
        return 0
    mel = librosa.power_to_db(librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=48, fmin=30, fmax=11000), ref=np.max)
    freqs = librosa.mel_frequencies(n_mels=48, fmin=30, fmax=11000)
    bf = np.unique(np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, mel.shape[1] - 1))
    # librosa.util.sync pads a leading column for the audio before the first beat
    # boundary; drop it so column k is beat k (otherwise everything lands one beat late)
    pad = 1 if bf[0] > 0 else 0
    mel_b = librosa.util.sync(mel, bf, aggregate=np.mean)[:, pad:]
    n = min(mel_b.shape[1], n_beats)
    mel_b = mel_b[:, :n]
    lin = lambda rows: np.mean(np.power(10, mel_b[rows] / 10), axis=0)
    kick = lin(freqs < 150)
    kick = kick / (np.percentile(kick, 98) + 1e-12)
    low = mel_b[freqs < 250].mean(axis=0)
    mid = mel_b[(freqs > 150) & (freqs < 1200)].mean(axis=0)
    flux_low = np.maximum(0, np.diff(low, prepend=low[0]))
    onset_mid = np.maximum(0, np.diff(mid, prepend=mid[0]))
    edges = np.zeros(n)
    for t in boundaries or []:
        i = int(np.argmin(np.abs(np.asarray(beats[:n]) - t)))
        if 0 < i < n:
            edges[i] += 1.0

    def term(v, agg):
        t = np.array([agg(v[p::4]) for p in range(4)])
        return (t - t.min()) / (t.max() - t.min()) if t.max() > t.min() else np.zeros(4)

    score = 2.0 * term(edges, np.sum) + 2.0 * term(kick, np.mean) + 0.5 * term(flux_low, np.sum) - 0.5 * term(onset_mid, np.sum)
    return int(np.argmax(score))


def analyze_cached(path: str, cache_dir: str) -> dict:
    """Analyse once per (path, size, mtime); cache the JSON next to the project."""
    os.makedirs(cache_dir, exist_ok=True)
    st = os.stat(path)
    key = f"{os.path.basename(path)}.{st.st_size}.{int(st.st_mtime)}.v16.json"
    cache = os.path.join(cache_dir, key)
    data = None
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        # an analysis saved while SongFormer could not run is not kept for ever: try again
        from . import structure_model
        if data.get("structure_source") != "songformer" and structure_model.available() and not structure_model.WORKER.failed:
            data = None
    if data is None:
        data = analyze(path, cache_dir)
    # The genre is added to an existing analysis rather than bumping the cache version:
    # it needs only the model, and re-running everything else for it would be a waste.
    from .genre import add_genre
    try:
        changed = add_genre(data, path)
    except Exception as e:  # noqa: BLE001 - a song without a genre still gets a show
        print(f"[genre] skipped: {type(e).__name__}: {e}")
        changed = False
    if changed or not os.path.exists(cache):
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    return data
