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
import time
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


class Laps:
    """Where an analysis spends its time, step by step: one line in models.log per song learned ahead
    (Run It once took 414 s instead of ~40, and nothing said where the time went)."""

    def __init__(self):
        self.t0 = self.t = time.perf_counter()
        self.steps: list[tuple[str, float]] = []

    def __call__(self, step: str) -> None:
        now = time.perf_counter()
        self.steps.append((step, now - self.t))
        self.t = now

    def line(self) -> str:
        total = time.perf_counter() - self.t0
        return f"{total:.1f} s: " + ", ".join(f"{k} {v:.1f}" for k, v in self.steps if v >= 0.05)


LAPS: Laps | None = None                                   # the analysis running now (analyze_audio)


def analyze_audio(y, sr: int = SR, stereo=None, path: str | None = None, cache_dir: str | None = None,
                  use_model: bool = False, events: bool = True, bpm_hint: float | None = None, segment_fn=None,
                  separate: bool | None = None) -> dict:
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
    global LAPS
    LAPS = lap = Laps()

    # The models (the stems, the sections, the beats: each its own process, on the GPU) are all asked at
    # once, side by side, while the CPU work below goes on; each answer is waited for only where it is
    # needed. One after another they cost ~7 s of a ~23 s analysis.
    want_models = bool(use_model or segment_fn is not None)
    if separate is None:
        separate = want_models
    pending, pool = {}, None
    if want_models:
        from concurrent.futures import ThreadPoolExecutor
        from . import beats_model as _bm, stems as _st, structure_model as _sm
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="models")
        if _bm.available():
            pending["beats"] = pool.submit(_bm.model_grid, y, sr, duration)
        if use_model and path:
            pending["segs"] = pool.submit(_sm.run_model, path, cache_dir)
        elif segment_fn is not None:
            pending["segs"] = pool.submit(segment_fn, y, sr)
        if events and separate:
            pending["stems"] = (pool.submit(_st.separate_file, path) if path
                                else pool.submit(_st.separate_audio, stereo if stereo is not None else y, sr))

    # --- tempo and beat grid --------------------------------------------------------
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    onset_times = librosa.frames_to_time(np.arange(len(onset)), sr=sr, hop_length=HOP)
    # Beat This, where the models are in use (a song file; a copy learned ahead): its tempo, the onsets'
    # precise fit at that tempo, its beats and bar lines (see beats_model.py). librosa's own tempo search
    # and grid run only when it cannot (no models, or no steady pulse heard): they cost seconds a song.
    beats, phases, beat_source = None, None, "librosa"
    if (use_model or segment_fn is not None) and "beats" in pending:
        try:
            from . import beats_model
            got = pending["beats"].result()
            if got:
                m_bpm, m_beats, m_phases, m_down, m_raw = got
                f_bpm, _ = fit_grid(onset, onset_times, m_bpm)
                f_per = 60.0 / f_bpm
                # where the beats sit: the model's (the onsets' strongest pulse can be the off-beat, half
                # a beat away: Great Good Fine Ok, SHOTS I'M NOT CALLING), as their circular mean
                ang = float(np.angle(np.mean(np.exp(2j * np.pi * np.asarray(m_beats, dtype=float) / f_per))))
                f_off = (ang / (2 * np.pi) * f_per) % f_per
                # then onto the attacks themselves: a nudge of at most 70 ms (never near the off-beat),
                # wherever the song's onsets are strongest on the whole grid (the model sits ~20 ms early)
                o_fps = sr / HOP
                at = lambda off: float(np.interp(np.arange(off, duration, f_per) * o_fps, np.arange(len(onset)), onset).mean())
                f_off = max((f_off + d for d in np.arange(-0.07, 0.0701, 0.005)), key=at) % f_per
                f_beats = np.arange(f_off, duration, f_per)
                # (no second opinion from the onsets on where the beats sit: a song with strong hits on the
                # off-beats fools it, and it swapped Kitten's ME onto the off-beat, its drop half a beat late)
                f_phases = beats_model.bar_places(f_beats, m_down) if abs(f_bpm / m_bpm - 1) <= 0.015 else None
                if f_phases:
                    bpm, beats, phases, beat_source = f_bpm, f_beats, f_phases, "beat this tempo, onset fit"
                else:
                    bpm, beats, phases, beat_source = m_bpm, np.asarray(m_beats, dtype=float), m_phases, "beat this"
        except Exception as e:  # noqa: BLE001 - librosa's grid stands in
            print(f"[beats] Beat This skipped: {type(e).__name__}: {e}")
    if beats is None:                                     # librosa's grid: the tempo search, half time fixed
        bpm, offset = fit_grid(onset, onset_times, bpm_hint)
        period = 60.0 / bpm
        beats = np.arange(offset - period * math.floor(offset / period), duration, period)
        if bpm_hint is None:
            bpm, beats = fix_half_tempo(y, sr, bpm, beats)
        bpm, beats = cap_tempo(y, sr, bpm, beats)
    first = float(beats[0]) if len(beats) else 0.0

    lap("beats")
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
    lap("features")
    phase, sections, drops = find_structure(y, sr, beats, rms, duration, phases)
    lap("structure")
    downbeats = beats[phase::4]
    # the loudness detector says what each part is; SongFormer, when it can run, says
    # exactly when each part starts (see structure_model.py for the measurements)
    from . import structure_model

    builtin_sections, builtin_drops = sections, drops
    structure_source, structure_error, segs = "loudness", "SongFormer was not asked", None
    if use_model and path:
        segs, structure_error = pending["segs"].result()
    elif segment_fn is not None:
        try:
            segs = pending["segs"].result()
            structure_error = "" if segs else f"SongFormer failed: {structure_model.WORKER.last_error or 'no answer'}"
        except Exception as e:  # noqa: BLE001
            structure_error = f"SongFormer failed: {type(e).__name__}: {e}"
    if segs:
        sections, drops, _ = structure_model.time_with_songformer(sections, drops, segs, [float(b) for b in beats], phase, duration)
        structure_source, structure_error = "loudness+songformer", ""

    lap("sections model")
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    mood = song_mood(y, sr, energy, centroid)
    ev = {"events": {}, "fills": [], "hook": None, "curves": {}, "stereo": False}
    # The drums and bass pulled out of the mix (Demucs, in the model environment), so the drums are
    # found in a track that has nothing else in it. Only where the models are in use anyway (a song
    # file; Jam mode with SongFormer), and always in memory: a jam's audio never touches the disk.
    stems = None
    problems = []                                            # whatever failed along the way (the show is made regardless)
    if structure_error and structure_error != "SongFormer was not asked":
        problems.append(structure_error)
    if events and separate:
        try:
            from . import stems as stems_mod
            stems = pending["stems"].result() if "stems" in pending else (
                stems_mod.separate_file(path) if path else stems_mod.separate_audio(stereo if stereo is not None else y, sr))
            if stems is None:
                problems.append(f"the stems could not be separated ({stems_mod.WORKER.failed or stems_mod.WORKER.last_error or 'no answer'})")
        except Exception as e:  # noqa: BLE001 - without stems the drums come from the mix, as before
            print(f"[stems] skipped: {type(e).__name__}: {e}")
            problems.append(f"the stems could not be separated ({type(e).__name__}: {e})")
    # The kicks and snares, heard by the drum transcriber in the separated drums (ADTOF; see drum_model.py).
    # Without it (the model environment not set up, or it failed) the pattern finder in events.py finds them.
    lap("mood + stems")
    drum_hits = None
    if events and stems:
        try:
            from . import drum_model
            drum_hits = drum_model.hits(stems)
            if drum_hits is None:
                problems.append(f"the drum model could not run ({drum_model.WORKER.failed or drum_model.WORKER.last_error or 'no answer'})")
        except Exception as e:  # noqa: BLE001 - the pattern finder stands in
            print(f"[drums] skipped: {type(e).__name__}: {e}")
            problems.append(f"the drum model could not run ({type(e).__name__}: {e})")
    lap("drum model")
    if events:
        try:
            from .events import detect_events
            ev = detect_events(path, [float(b) for b in beats], duration, phase, SR, HOP, FEATURE_FPS,
                               audio=(stereo if stereo is not None else y, sr) if path is None else None, stems=stems,
                               drum_hits=drum_hits)
        except Exception as e:  # noqa: BLE001 - a show without flair is better than no show
            print(f"[events] skipped: {type(e).__name__}: {e}")
            problems.append(f"the drums and instruments could not be found ({type(e).__name__}: {e})")
    if stems:                                                # a drop starts where its bass comes in
        try:
            sections, drops, moved = refine_drops(sections, drops, beats, stems)
            if moved:
                print(f"[structure] drops moved to where the bass comes in: {moved}")
        except Exception as e:  # noqa: BLE001 - the found drops stand
            print(f"[structure] drop timing skipped: {type(e).__name__}: {e}")
    lap("drums + instruments")
    if ev.get("rolls"):
        sections, _ = buildups_from_rolls(sections, ev["rolls"], beats, phase)

    out = {
        "duration": duration,
        # the opening 45 seconds as band onsets: enough to find this song's 0:00 by ear in a jam
        "align_bands": [[round(float(v), 2) for v in row] for row in match_features(y[: int(45 * sr)], sr, HOP)],
        "align_fps": round(sr / HOP, 4),
        "mood": mood,
        "events": ev["events"],
        "drum_calibration": ev.get("drum_calibration"),
        "fills": ev["fills"],
        "rolls": ev.get("rolls", []),
        "cuts": ev.get("cuts", []),
        "entries": ev.get("entries", []),
        "hatrolls": ev.get("hatrolls", []),
        "wobbles": ev.get("wobbles", []),
        "key_change": ev.get("key_change"),
        "hook": ev["hook"],
        "curves": ev["curves"],
        "feature_fps": FEATURE_FPS,
        "bpm": round(bpm, 3),
        "beat_offset": round(float(first), 4),
        "downbeat_phase": phase,
        "beat_source": beat_source,
        "beats": [round(float(b), 4) for b in beats],
        "downbeats": [round(float(b), 4) for b in downbeats],
        "feature_fps": FEATURE_FPS,
        "energy": [round(float(e), 4) for e in energy],
        "bands": [[round(float(v), 3) for v in row] for row in bands.T],
        "wave_pps": WAVE_PPS,
        "peaks": [round(float(p), 3) for p in peaks],
        "sections": sections,
        "drops": drops,
        "structure_source": structure_source,
        "structure_error": structure_error,
        "problems": problems,
        "builtin_sections": builtin_sections,
        "builtin_drops": builtin_drops,
        # SongFormer's own sections, labels and all (the studio takes its labels from the loudness and only
        # its timing from SongFormer; settle_sections asks these for a second opinion)
        "songformer": [{"start": round(float(x["start"]), 2), "end": round(float(x["end"]), 2), "label": str(x["label"])} for x in segs or []],
    }
    lap("output")
    moved = settle_sections(out)
    lap("section rules")
    if moved:
        print(f"[structure] sections settled by the drums and the hook: {moved}")
    if pool:
        pool.shutdown(wait=False)
    return out


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


def stem_beats(stems: dict, beats) -> np.ndarray | None:
    """Each beat's level of the separated drums, bass, other instruments and vocals (each 0..1 against its
    own loud level), for telling one part of a song from the next by what is playing."""
    beats = np.asarray(beats, dtype=float)
    if not stems or len(beats) < 8:
        return None
    sr = float(stems.get("sr") or SR)
    cols = []
    for k in ("drums", "bass"):
        x = stems.get(k)
        if x is None:
            return None
        x = np.asarray(x, dtype=np.float32)
        hop = max(1, int(sr / 50))
        m = len(x) // hop
        env, fps = np.sqrt((x[: m * hop].astype(np.float64).reshape(m, hop) ** 2).mean(axis=1)), 50.0
        cols.append((env, fps))
    lv, lfps = stems.get("levels") or {}, float(stems.get("fps") or 30)
    for k in ("other", "vocals"):
        if lv.get(k) is not None:
            cols.append((np.asarray(lv[k], dtype=float), lfps))
    out = np.zeros((len(beats) - 1, len(cols)))
    for j, (env, fps) in enumerate(cols):
        ref = float(np.percentile(env, 95)) + 1e-9
        for b in range(len(beats) - 1):
            i0, i1 = int(beats[b] * fps), max(int(beats[b] * fps) + 1, int(beats[b + 1] * fps))
            out[b, j] = float(env[i0:i1].mean()) / ref if i0 < len(env) else 0.0
    return np.clip(out, 0, 1.5)


def drumless_drops(a: dict) -> list:
    """A drop starts where its drums come in to stay. Where a section found as a drop (loud and bassy)
    starts with the separated drum track silent (under 0.15 for its first 6 s), in a song that has drums:

    - if the drums never settle in (2 s stretches all at 0.45 or more for 8 s), it is no drop at all: a
      verse (SLANDER's When I'm With You at 1:06, a heavy drumless lead-in);
    - else the drop lands where they do, and when they come in, stop for a moment and come back (the
      classic build), after the stop (the same song's last drop: 3:06 found, drums in at 3:18, a pause
      at 3:33, the drop at 3:37, which is where it is heard). What comes before is a verse.

    Drops that start with drums, however sparse (Coldplay's chorus) or light, are left alone. Across 32
    songs with a drum track this changed four sections in three songs. Changes the analysis in place
    (also one saved before the rule); returns what changed."""
    dp = (a.get("curves") or {}).get("drums_present")
    secs = a.get("sections") or []
    if not dp or not secs:
        return []
    dp = np.asarray(dp, dtype=float)
    fps = float(a.get("feature_fps", FEATURE_FPS))
    lvl = lambda t0, t1: float(dp[int(t0 * fps):max(int(t0 * fps) + 1, int(t1 * fps))].mean()) if int(t0 * fps) < len(dp) else 1.0
    if not any(lvl(s["start"], s["end"]) >= 0.5 for s in secs):
        return []                                        # a song without drums: its drops swell instead
    steady = lambda t, end: t + 8 <= end + 1e-6 and all(lvl(b, b + 2) >= 0.45 for b in (t, t + 2, t + 4, t + 6))
    bars = sorted(a.get("downbeats") or a.get("beats") or [])
    out, changes = [], []
    for s in secs:
        start, end = float(s["start"]), float(s["end"])
        if s.get("type") != "drop" or lvl(start, min(end, start + 6)) >= 0.15:
            out.append(s)
            continue
        came = next((float(t) for t in np.arange(start, end - 7.5, 0.5) if steady(t, end)), None)
        if came is None:                                 # the drums never settle in: no drop here at all
            out.append({**s, "type": "verse"})
            a["drops"] = [d for d in (a.get("drops") or []) if not (start - 0.5 <= d < end)]
            changes.append((round(start, 1), "verse"))
            continue
        at = came
        dip = next((float(t) for t in np.arange(came + 2, min(end - 8, came + 24), 0.5) if lvl(t, t + 2) < 0.35), None)
        if dip is not None:
            after = next((float(t) for t in np.arange(dip + 1, end - 7.5, 0.5) if steady(t, end)), None)
            if after is not None:
                at = after
        # the first moment the drums pass 0.45 there, then the nearest bar line within a second
        k = next((i for i in range(int(at * fps), min(len(dp), int((at + 2) * fps))) if dp[i] >= 0.45), int(at * fps))
        th = k / fps
        near = [b for b in bars if start < b < end and abs(b - th) <= 1.0]
        at = float(min(near, key=lambda b: abs(b - th))) if near else th
        if at - start < 4:
            out.append(s)
            continue
        out.append({**s, "end": at, "type": "verse"})
        out.append({**s, "start": at})
        a["drops"] = sorted([d for d in (a.get("drops") or []) if not (start - 0.5 <= d < end)] + [round(at, 3)])
        changes.append((round(start, 1), "->", round(at, 1)))
    a["sections"] = out
    return changes


def loud_buildups(a: dict) -> list:
    """A buildup that already is its drop: as loud as the drop it leads into (within 0.02), with its bass
    (within 0.1), and far louder than what came before it (by 0.15): the drop starts there. Madeon's Pixel
    Empire: 1:37.7 to 1:47.3 was called a buildup, the drop landing 10 s late. Across the 49 learned buildups
    before a drop it changed that one. Changes the analysis in place; returns what changed."""
    secs = a.get("sections") or []
    bass, en = _curve(a, "bass"), np.asarray(a.get("energy") or [], dtype=float)
    if len(secs) < 3 or bass is None or len(en) == 0:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))

    def mean(c, t0, t1):
        i0, i1 = max(0, int(t0 * fps)), int(t1 * fps)
        return float(c[i0:max(i0 + 1, i1)].mean()) if i0 < len(c) else 0.0
    out, changes, i = [], [], 0
    while i < len(secs):
        s = secs[i]
        nx = secs[i + 1] if i + 1 < len(secs) else None
        pv = out[-1] if out else None
        if (s.get("type") == "buildup" and nx and nx.get("type") == "drop" and pv
                and mean(en, s["start"], s["end"]) >= mean(en, nx["start"], nx["end"]) - 0.02
                and mean(en, s["start"], s["end"]) - mean(en, pv["start"], pv["end"]) >= 0.15
                and mean(bass, s["start"], s["end"]) >= mean(bass, nx["start"], nx["end"]) - 0.1):
            out.append({**nx, "start": s["start"]})
            a["drops"] = sorted([d for d in (a.get("drops") or []) if not (s["start"] - 0.5 <= d < nx["end"])] + [round(float(s["start"]), 3)])
            changes.append((round(s["start"], 1), "buildup is the drop"))
            i += 2
            continue
        out.append(s)
        i += 1
    a["sections"] = out
    return changes


# SongFormer's labels, in the studio's terms (its "inst" says nothing either way: an EDM drop or an instrumental verse)
MODEL_TYPE = {"chorus": "drop", "verse": "verse", "pre-chorus": "verse", "prechorus": "verse", "intro": "intro",
              "outro": "outro", "bridge": "breakdown"}


def _model_says(a: dict, t0: float, t1: float, raw: bool = False) -> dict | None:
    """What share of t0..t1 SongFormer calls each of the studio's types (its own labels, kept with the analysis
    as "songformer"; raw: its own words, a pre-chorus apart from a verse), or None for a song analysed before
    they were kept."""
    segs = a.get("songformer")
    if not segs or t1 <= t0:
        return None
    share: dict[str, float] = {}
    for x in segs:
        o = min(t1, float(x["end"])) - max(t0, float(x["start"]))
        lab = str(x.get("label", "")).lower()
        k = lab if raw else MODEL_TYPE.get(lab)
        if o > 0 and k:
            share[k] = share.get(k, 0.0) + o / (t1 - t0)
    return share


def _model_against_drop(a: dict, t0: float, t1: float) -> bool:
    """SongFormer calls most of this stretch (60%) a verse, an intro or an outro: no rule makes it a drop. The
    loudness alone made The Maine's loud first verse a drop; SongFormer heard a verse there."""
    sh = _model_says(a, t0, t1)
    return bool(sh) and sh.get("verse", 0) + sh.get("intro", 0) + sh.get("outro", 0) >= 0.6


def _model_for_drop(a: dict, t0: float, t1: float, most: float = 0.6) -> bool:
    sh = _model_says(a, t0, t1)
    return bool(sh) and sh.get("drop", 0) >= most


def model_choruses(a: dict) -> list:
    """A verse or breakdown (8 s or more) SongFormer calls a chorus (70% of it or more) that is as big as the
    song's drops (loudness within 0.08, drums and bass within 0.15; in a song with no drop, louder than its
    middle section) is a drop: the second opinion the loudness labels lacked. Changes the analysis in place."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    lv = _section_levels(a)
    if not secs or lv is None or not a.get("songformer"):
        return []
    drops = [x for x in secs if x.get("type") == "drop"]
    if drops:
        de, db, dd = (float(np.mean([lv(x)[k] for x in drops])) for k in range(3))
    else:
        body = [lv(x)[0] for x in secs if x.get("type") not in ("intro", "outro")]
        mid = float(np.median(body)) if body else 1.0
    made, changes = set(), []
    for i, x in enumerate(secs):
        if x.get("type") not in ("verse", "breakdown") or x["end"] - x["start"] < 8 or not _model_for_drop(a, x["start"], x["end"], 0.7):
            continue
        e, b, d = lv(x)
        big = (e >= de - 0.08 and d >= dd - 0.15 and b >= db - 0.15) if drops else e >= mid + 0.03
        if big:
            secs[i] = {**x, "type": "drop"}
            made.add(i)
            changes.append((round(x["start"], 1), "SongFormer hears a chorus, as big as the drops"))
    if made:
        _merge_drops(a, secs, made)
    return changes


def _section_levels(a: dict):
    """mean(section) -> (loudness as a share of the song's loud level, bass, drums), or None without the curves."""
    en = np.asarray(a.get("energy") or [], dtype=float)
    bass, drums = _curve(a, "bass"), _curve(a, "drums_present")
    if len(en) == 0 or bass is None or drums is None:
        return None
    fps = float(a.get("feature_fps", FEATURE_FPS))
    top = float(np.percentile(en, 90)) or 1.0

    def mean(c, s):
        i0 = max(0, int(s["start"] * fps))
        return float(c[i0:max(i0 + 1, int(s["end"] * fps))].mean()) if i0 < len(c) else 0.0
    return lambda sec: (mean(en, sec) / top, mean(bass, sec), mean(drums, sec))


def _standing_out(secs: list, lv) -> callable:
    """Does a drop stand out from the rest of its song (0.05 louder than its middle other section)? A song
    mastered flat (Kim Petras' I Like Ur Look: every section 0.58 to 0.66) gives nothing to compare with."""
    rest = [lv(x)[0] for x in secs if x.get("type") not in ("drop", "intro", "outro") and x["end"] - x["start"] >= 8]
    mid = float(np.median(rest)) if rest else 0.0
    return lambda d: lv(d)[0] >= mid + 0.05


def _merge_drops(a: dict, secs: list, made: set) -> list:
    """Sections turned into drops here join a drop next to them: one drop, starting where the first does."""
    out = []
    for i, sec in enumerate(secs):
        if out and sec.get("type") == "drop" and out[-1].get("type") == "drop" and (i in made or out[-1].get("_made")):
            out[-1] = {**out[-1], "end": sec["end"], "_made": True}
            continue
        out.append({**sec, "_made": i in made} if sec.get("type") == "drop" else sec)
    for sec in out:
        sec.pop("_made", None)
    a["sections"] = out
    a["drops"] = sorted(round(float(x["start"]), 3) for x in out if x.get("type") == "drop")
    return out


def drops_where_none(a: dict) -> list:
    """A song found with no drop, or none bigger than the rest of it, still has its big parts: the sections
    (8 s or more) clearly louder than the song's run (0.05 above the middle section, within 0.1 of the
    loudest, and louder than every drop it has) with bass to them (half the loudest section's or more) are
    its drops. Six of 194 learned songs had none (Missy Elliott's Lose Control, Adam Lambert's WET DREAM, a
    song of drops between breakdowns: Wallpaper.); Martin Garrix's Pressure had its two big drops called
    verses, louder than the one drop it was given."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    lv = _section_levels(a)
    if not secs or lv is None or (a.get("genre") or {}).get("style") == "chill":
        return []                                                    # (a calm song may have no drop: I Put a Spell On You)
    have = [x for x in secs if x.get("type") == "drop"]
    if have and any(_standing_out(secs, lv)(x) for x in have):
        return []
    body = [i for i, x in enumerate(secs) if x.get("type") not in ("intro", "outro") and x["end"] - x["start"] >= 8]
    if len(body) < 3:
        return []
    L = {i: lv(secs[i]) for i in body}
    loudest, mid = max(v[0] for v in L.values()), float(np.median([v[0] for v in L.values()]))
    top_bass = max(v[1] for v in L.values())
    over = max((lv(x)[0] for x in have), default=0.0) + 0.03
    picks = {i for i in body if secs[i].get("type") != "drop" and L[i][0] >= max(loudest - 0.1, mid + 0.05, over)
             and L[i][1] >= 0.5 * top_bass and L[i][2] >= 0.4 and not _model_against_drop(a, secs[i]["start"], secs[i]["end"])}
    if not picks:
        return []
    changes = [(round(secs[i]["start"], 1), "drops missed: one of the loudest parts") for i in sorted(picks)]
    for i in picks:
        secs[i] = {**secs[i], "type": "drop"}
    _merge_drops(a, secs, picks)
    return changes


def _fingerprint(a: dict, fps_out: float = 4.0):
    """The song's sound (align_bands: its spectrum, as the line-up uses it) at 4 frames a second, each band
    scaled to its own run, for comparing one stretch with another; None without it."""
    B = a.get("align_bands")
    if not B:
        return None
    B = np.asarray(B, dtype=np.float32)
    step = max(1, int(round(float(a.get("align_fps", 43.0)) / fps_out)))
    n = B.shape[1] // step
    if n < 16:
        return None
    B = B[:, :n * step].reshape(B.shape[0], n, step).mean(axis=2)
    return (B - B.mean(axis=1, keepdims=True)) / (B.std(axis=1, keepdims=True) + 1e-6), float(a.get("align_fps", 43.0)) / step


def _sounds_alike(fp, s: dict, d: dict, cap: float = 16.0, reach: float = 2.0) -> float:
    """How alike two sections sound: the best correlation of their first 16 s (2 s of lag either way)."""
    F, fr = fp
    n, r = int(min(cap, s["end"] - s["start"], d["end"] - d["start"]) * fr), int(reach * fr)
    i0, j0 = int(s["start"] * fr), int(d["start"] * fr)
    if n < 8 or i0 + n > F.shape[1]:
        return 0.0
    x, best = F[:, i0:i0 + n].ravel(), -1.0
    for j in range(max(0, j0 - r), min(F.shape[1] - n, j0 + r) + 1):
        best = max(best, float(np.corrcoef(x, F[:, j:j + n].ravel())[0, 1]))
    return best


def drops_by_sound(a: dict) -> list:
    """A section that sounds like one of the song's drops, and is as big, is that drop again (the chorus
    comes back). Sounds like: the spectrum of its opening correlates with a drop's by 0.5 or more (across
    the learned songs a chorus heard again scores 0.68 to 0.82, a verse against its drops 0.09 at the middle
    and 0.35 at most nine times in ten). As big: within 0.06 of the drop's loudness, its bass within 0.12,
    its drums within 0.15 (a stripped-down chorus in a breakdown sounds alike and is not a drop). A drop that
    does not stand out from the song (a flat master) needs the sound alike by 0.7: Flo Rida's Low opens on
    its chorus (0.84)."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    lv, fp = _section_levels(a), _fingerprint(a)
    if lv is None or fp is None:
        return []
    out = _standing_out(secs, lv)
    drops = [x for x in secs if x.get("type") == "drop" and x["end"] - x["start"] >= 8]
    if not drops:
        return []
    made, changes = set(), []
    for i, x in enumerate(secs):
        if x.get("type") not in ("verse", "breakdown") or x["end"] - x["start"] < 8 or _model_against_drop(a, x["start"], x["end"]):
            continue
        e, b, dr = lv(x)
        for d in drops:
            de, db, ddr = lv(d)
            if e >= de - 0.06 and b >= db - 0.12 and dr >= ddr - 0.15 and _sounds_alike(fp, x, d) >= (0.5 if out(d) else 0.7):
                secs[i] = {**x, "type": "drop"}
                made.add(i)
                changes.append((round(x["start"], 1), f"sounds like the drop at {d['start']:.0f} s"))
                break
    if made:
        _merge_drops(a, secs, made)
    return changes


def drop_neighbors(a: dict) -> list:
    """A verse right against a drop and as big as it (its loudness within 0.02, bass within 0.05, drums
    within 0.05) is that drop's first or last bars: the drop runs over it (Virtual Self's Ghost Voices,
    Dylan Matthew's WANTURBODY, Ghost Town's That's Unusual)."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    lv = _section_levels(a)
    if len(secs) < 2 or lv is None:
        return []
    out = _standing_out(secs, lv)
    made, changes = set(), []
    for i, x in enumerate(secs):
        if x.get("type") != "verse" or _model_against_drop(a, x["start"], x["end"]):
            continue
        e, b, dr = lv(x)
        for j in (i - 1, i + 1):
            if 0 <= j < len(secs) and secs[j].get("type") == "drop" and j not in made and out(secs[j]):
                de, db, ddr = lv(secs[j])
                if e >= de - 0.02 and b >= db - 0.05 and dr >= ddr - 0.05:
                    secs[i] = {**x, "type": "drop"}
                    made.add(i)
                    changes.append((round(x["start"], 1), "as big as the drop beside it"))
                    break
    if made:
        _merge_drops(a, secs, made)
    return changes


def drop_tails_and_releases(a: dict) -> list:
    """Two more ways a drop hides as a verse.
    - Its tail: a verse of four bars or less straight after a drop, still about as full (loudness within 0.1,
      bass and drums within 0.1), is the drop carrying on (Porter Robinson's Everything Goes On: the drop
      ended at 110 s, the music full until 123). Longer, it is a real verse (a loud rock verse after a chorus).
    - A release: a verse right after a breakdown, intro or buildup (8 s or more, clearly quieter: 0.25 under
      the drops), as big as the song's drops (loudness within 0.07, or 0.05 after an intro: a band kicking in after
      an intro is a loud verse more often than not; bass and drums within 0.1), is a drop: the
      build-and-release of a song made of breakdowns and drops (Everything Goes On at 40 s, Pressure's second).
    Changes the analysis in place; returns what changed."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    lv = _section_levels(a)
    bars = [float(b) for b in a.get("downbeats") or []]
    drops = [x for x in secs if x.get("type") == "drop"]
    if len(secs) < 2 or lv is None or not drops:
        return []
    bar = float(np.median(np.diff(bars))) if len(bars) > 2 else 240.0 / float(a.get("bpm") or 120.0)
    de = float(np.mean([lv(x)[0] for x in drops]))
    db = float(np.mean([lv(x)[1] for x in drops]))
    dd = float(np.mean([lv(x)[2] for x in drops]))
    made, changes = set(), []
    for i, x in enumerate(secs):
        if x.get("type") != "verse" or i == 0 or _model_against_drop(a, x["start"], x["end"]):
            continue
        e, b, dr = lv(x)
        prev = secs[i - 1]
        pe, pb, pd = lv(prev)
        if prev.get("type") == "drop" and x["end"] - x["start"] <= 4 * bar + 0.5 and e >= pe - 0.1 and b >= pb - 0.1 and dr >= pd - 0.1:
            changes.append((round(x["start"], 1), "the drop carrying on"))
        elif (prev.get("type") in ("breakdown", "intro", "buildup") and prev["end"] - prev["start"] >= 8 and pe <= de - 0.25
              and e >= de - (0.05 if prev.get("type") == "intro" else 0.07) and b >= db - 0.1 and dr >= dd - 0.1):
            changes.append((round(x["start"], 1), "a release after the breakdown"))
        else:
            continue
        secs[i] = {**x, "type": "drop"}
        made.add(i)
    if made:
        _merge_drops(a, secs, made)
    return changes


def verses_in_long_drops(a: dict) -> list:
    """A drop of 16 bars or more that hides verses: Madeon's Enjoy, mastered loud end to end, came out one
    drop from 0:28 to 2:12, its verses inside it. A drop's own bars are full: its drums near their fullest in
    it, and its bass in when it has bass. A stretch of four bars or more that is not (the drums 0.3 under the
    drop's fullest, or the bass out where the drop's full bars have it) is a verse, or a breakdown when the
    drums are nearly gone; the drop is what is left, four bars or more. Changes the analysis in place;
    returns what changed."""
    secs = a.get("sections") or []
    drums, bass = _curve(a, "drums_present"), _curve(a, "bass")
    bars = [float(b) for b in a.get("downbeats") or []]
    if not secs or drums is None or bass is None or len(bars) < 8:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    bar = float(np.median(np.diff(bars)))

    def mean(c, t0, t1):
        i0 = max(0, int(t0 * fps))
        return float(c[i0:max(i0 + 1, int(t1 * fps))].mean()) if i0 < len(c) else 0.0
    out, changes = [], []
    for sec in secs:
        inb = [b for b in bars if sec["start"] - 0.1 <= b <= sec["end"] - 0.5 * bar]
        if sec.get("type") != "drop" or len(inb) < 16:
            out.append(sec)
            continue
        d = [mean(drums, b, b + bar) for b in inb]
        top_d = float(np.percentile(d, 80))
        fullb = [mean(bass, b, b + bar) for b, x in zip(inb, d) if x >= top_d - 0.1]
        has_bass = len(fullb) > 0 and float(np.median(fullb)) >= 0.3
        full = [x >= top_d - 0.3 and (not has_bass or mean(bass, b, b + bar) >= 0.15) for b, x in zip(inb, d)]
        runs, k = [], 0                                  # the stretches that are not full, four bars or more
        while k < len(inb):
            if full[k]:
                k += 1
                continue
            j = k
            while j < len(inb) and not full[j]:
                j += 1
            r0, r1 = inb[k], (inb[j] if j < len(inb) else sec["end"])
            if j - k >= 4 and not _model_for_drop(a, r0, r1):
                runs.append((k, j))
            k = j
        if not runs or sum(full) < 4:
            out.append(sec)
            continue
        label = ["drop"] * len(inb)                      # each bar: drop, or the verse / breakdown it is part of
        for k0, k1 in runs:
            kind = "breakdown" if float(np.mean(d[k0:k1])) < 0.2 else "verse"
            for k in range(k0, k1):
                label[k] = kind
        pieces = []                                      # [type, first bar, last bar + 1]
        for k, lab in enumerate(label):
            if pieces and pieces[-1][0] == lab:
                pieces[-1][2] = k + 1
            else:
                pieces.append([lab, k, k + 1])
        for pc in list(pieces):                          # a drop of under four bars between them joins the one before
            if pc[0] == "drop" and pc[2] - pc[1] < 4 and len(pieces) > 1:
                i = pieces.index(pc)
                j = i - 1 if i > 0 else i + 1
                pieces[j][1], pieces[j][2] = min(pieces[j][1], pc[1]), max(pieces[j][2], pc[2])
                pieces.remove(pc)
        if not any(pc[0] == "drop" for pc in pieces):
            out.append(sec)                              # (never the whole drop gone)
            continue
        for n, (lab, k0, k1) in enumerate(pieces):
            t0 = sec["start"] if n == 0 else inb[k0]
            t1 = sec["end"] if n == len(pieces) - 1 else inb[k1]
            out.append({**sec, "start": t0, "end": t1, "type": lab})
            if lab != "drop":
                changes.append((round(t0, 1), f"a {lab} inside a long drop", round(t1, 1)))
        a["drops"] = sorted([x for x in (a.get("drops") or []) if not (sec["start"] - 0.5 <= x < sec["end"])]
                            + [round(out[-len(pieces) + n]["start"], 3) for n, pc in enumerate(pieces) if pc[0] == "drop"])
    if changes:
        a["sections"] = out
    return changes


def choruses_in_long_sections(a: dict) -> list:
    """A verse or breakdown of 24 bars or more that hides its choruses. A song mastered loud from end to end
    gives the section finder nothing to cut on: little image's LUNGS BURN came out one 129 s verse and one
    drop, its two choruses inside the verse. They are still plain: the bars where the song lifts, its drums
    clearly fuller than the section's middle (0.8, and 0.15 over it) or its hook playing (near the section's
    middle loudness, 0.03 under it at most), lifted bars together (two quiet bars between allowed), four bars or more from
    the first to the last, are a drop. Runs first (before the drop rules), so they see the drops it finds.
    Changes the analysis in place; returns what changed."""
    secs = a.get("sections") or []
    en = np.asarray(a.get("energy") or [], dtype=float)
    drums = _curve(a, "drums_present")
    bars = [float(b) for b in a.get("downbeats") or []]
    if not secs or len(en) == 0 or drums is None or len(bars) < 8:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    bar = float(np.median(np.diff(bars)))
    hooks = sorted(float(t) for t in ((a.get("hook") or {}).get("times") or []))

    def mean(c, t0, t1):
        i0 = max(0, int(t0 * fps))
        return float(c[i0:max(i0 + 1, int(t1 * fps))].mean()) if i0 < len(c) else 0.0
    out, changes = [], []
    for sec in secs:
        inb = [b for b in bars if sec["start"] - 0.1 <= b <= sec["end"] - 0.5 * bar]
        if sec.get("type") not in ("verse", "breakdown") or len(inb) < 24:
            out.append(sec)
            continue
        mid_d = float(np.median([mean(drums, b, b + bar) for b in inb]))
        mid_e = float(np.median([mean(en, b, b + bar) for b in inb]))
        lifted = [mean(drums, b, b + bar) >= max(0.8, mid_d + 0.15)
                  or (any(b - 0.2 <= h < b + bar for h in hooks) and mean(en, b, b + bar) >= mid_e - 0.03) for b in inb]
        groups, run, quiet = [], [], 0
        for b, up in zip(inb, lifted):
            if up:
                run.append(b)
                quiet = 0
            elif run:
                quiet += 1
                if quiet > 2:
                    groups.append(run)
                    run, quiet = [], 0
        if run:
            groups.append(run)
        groups = [(g[0], min(sec["end"], g[-1] + bar)) for g in groups if (g[-1] - g[0]) / bar + 1 >= 4]
        groups = [g for g in groups if not _model_against_drop(a, g[0], g[1])]
        if not groups:
            out.append(sec)
            continue
        t = sec["start"]
        for g0, g1 in groups:
            if g0 - t >= bar:
                out.append({**sec, "start": t, "end": g0})
            out.append({**sec, "start": max(t, g0), "end": g1, "type": "drop"})
            changes.append((round(g0, 1), f"a chorus inside a long {sec.get('type')}", round(g1, 1)))
            t = g1
        if sec["end"] - t >= bar:
            out.append({**sec, "start": t, "end": sec["end"]})
        else:
            out[-1]["end"] = sec["end"]
    if changes:
        a["sections"] = out
        a["drops"] = sorted(set(list(a.get("drops") or []) + [round(float(c[0]), 3) for c in changes]))
        merged = []
        for x in out:                                    # (a chorus that runs into a drop is one drop with it)
            if merged and x.get("type") == "drop" and merged[-1].get("type") == "drop" and abs(merged[-1]["end"] - x["start"]) < 0.5:
                merged[-1] = {**merged[-1], "end": x["end"]}
                a["drops"] = [d for d in a["drops"] if abs(d - x["start"]) > 0.5]
            else:
                merged.append(x)
        a["sections"] = merged
    return changes


def drops_on_their_bar(a: dict) -> list:
    """A drop starts on the bar where it is heard: within the two bars before where it was found, the first
    bar line where the song is already as loud as the drop (within 0.06 of the loud level, and no more
    than 0.15 louder: a bar far louder than the drop says the drop itself is wrong, not late) and its drums
    are in (0.8, or the drop's own less 0.1, when the drop has drums), after a clearly quieter bar (0.15
    quieter, or the drums 0.4 down). The Maine's how to exit a room: the drop at 57.1 s, heard at 54.0, a
    bar early (the breakdown's bass kept drop_landings from seeing it); Midnight City's three, a bar each.
    45 drops of 194 learned songs. Changes the analysis in place; returns what changed."""
    secs = a.get("sections") or []
    en = np.asarray(a.get("energy") or [], dtype=float)
    drums = _curve(a, "drums_present")
    bars = [float(b) for b in a.get("downbeats") or []]
    if len(secs) < 2 or len(en) == 0 or drums is None or len(bars) < 4:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    top = float(np.percentile(en, 90)) or 1.0
    bar = float(np.median(np.diff(bars)))

    def mean(c, t0, t1):
        i0 = max(0, int(t0 * fps))
        return float(c[i0:max(i0 + 1, int(t1 * fps))].mean()) if i0 < len(c) else 0.0
    changes = []
    for i, sec in enumerate(secs):
        if sec.get("type") != "drop" or i == 0 or secs[i - 1].get("type") == "drop":
            continue
        prev = secs[i - 1]
        de, dd = mean(en, sec["start"], sec["start"] + 2 * bar) / top, mean(drums, sec["start"], sec["start"] + 2 * bar)
        for b in sorted(x for x in bars if sec["start"] - 2 * bar - 0.3 <= x <= sec["start"] - 0.5 * bar and x > prev["start"] + bar):
            e1, d1 = mean(en, b, b + bar) / top, mean(drums, b, b + bar)
            e0, d0 = mean(en, b - bar, b) / top, mean(drums, b - bar, b)
            # ...and it stays up, every bar from there to where the drop was found (The Wldlfe's A Boy On A
            # Phone: loud and quiet bars by turns before its drop, a stop-start build, moved the drop a bar early)
            stays = all(mean(en, x, x + bar) / top >= de - 0.08 and (dd < 0.5 or mean(drums, x, x + bar) >= min(0.75, dd - 0.15))
                        for x in bars if b <= x < sec["start"] - 0.3)
            if (de - 0.06 <= e1 <= de + 0.15 and (dd < 0.5 or d1 >= min(0.8, dd - 0.1))
                    and (e1 - e0 >= 0.15 or d1 - d0 >= 0.4) and stays):
                changes.append((round(sec["start"], 1), "drop heard a bar early", round(b, 1)))
                a["drops"] = sorted([d for d in (a.get("drops") or []) if not (sec["start"] - 0.5 <= d < sec["end"])] + [round(b, 3)])
                prev["end"], sec["start"] = b, b
                break
    return changes


def flat_buildups(a: dict) -> list:
    """A buildup where nothing builds is not one. A buildup builds by at least one of: its snares speeding
    up (the last quarter 1.5 times the first half and one a beat or more), the song climbing through it (its
    last quarter louder than its first by 0.08 of the song's loud level) or the synths opening up (by 0.12),
    or the drop landing on it louder (by 0.1). Across the 196 buildups of 194 learned songs, 21 showed none
    (Turnover's New Scream had three flat ones in its verses). Where SongFormer calls a long one (6 bars or
    more) a verse, only its snares speeding up or its last quarter above every earlier one counts: a verse
    lands on a louder chorus too, and its instruments coming back after a one-bar dip is no climb (SHOTS I'M
    NOT CALLING, 0:55 to 1:17: a steady backbeat called a buildup). One that shows none:

    - loud from its first bar (0.25 above the bars before it), into a drop: the drop starts there;
    - inside a drop (a drop either side of it): part of the drop before it;
    - not before a drop, with the drums falling away (under 0.3): a breakdown;
    - else part of the section before it (it is that verse going on);
    - and one SongFormer calls a verse is a verse, whatever is around it.

    A buildup builds by holding the bass back: one with more bass than the drop it leads into (its first 8
    bars, by 0.05),
    where SongFormer hears a chorus (80% of it) and the snares do not speed up, is that chorus: the drop
    starts there (SHOTS I'M NOT CALLING's last chorus, 2:06, its bass the song's strongest).

    Changes the analysis in place (a saved one too, on load); returns what changed."""
    secs = [dict(s) for s in (a.get("sections") or [])]
    en = np.asarray(a.get("energy") or [], dtype=float)
    if len(secs) < 2 or len(en) == 0:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beat = 60.0 / float(a.get("bpm") or 120.0)
    other, drums, bass = _curve(a, "other"), _curve(a, "drums_present"), _curve(a, "bass")
    loud_top = float(np.percentile(en, 90)) or 1.0
    ev = a.get("events") or {}
    snares = np.array(sorted(float(e[0]) for k in ("snare", "roll") for e in (ev.get(k) or [])))

    def mean(c, t0, t1):
        if c is None:
            return None
        i0 = max(0, int(t0 * fps))
        return float(c[i0:max(i0 + 1, int(t1 * fps))].mean()) if i0 < len(c) else None

    def rate(t0, t1):
        return float(((snares >= t0) & (snares < t1)).sum()) / max(0.5, (t1 - t0) / beat)
    out, changes = [], []
    for i, sec in enumerate(secs):
        nx = secs[i + 1] if i + 1 < len(secs) else None
        pv = out[-1] if out else None
        if sec.get("type") != "buildup":
            out.append(sec)
            continue
        t0, t1 = sec["start"], sec["end"]
        q = (t1 - t0) / 4
        e1, e4, e_all = mean(en, t0, t0 + q), mean(en, t1 - q, t1), mean(en, t0, t1)
        o1, o4 = mean(other, t0, t0 + q), mean(other, t1 - q, t1)
        r1, r4 = rate(t0, t0 + 2 * q), rate(t1 - q, t1)
        accel = r4 >= max(1.5 * r1, r1 + 0.5) and r4 >= 1.0
        climbs = (e4 - e1) / loud_top >= 0.08 or (o1 is not None and o4 - o1 >= 0.12)
        into_drop = nx is not None and nx.get("type") == "drop"
        lands = into_drop and (mean(en, nx["start"], min(nx["end"], nx["start"] + 8 * beat)) - e4) / loud_top >= 0.1
        says = _model_says(a, t0, t1, raw=True) or {}
        if (into_drop and not accel and says.get("chorus", 0.0) >= 0.8 and bass is not None
                and mean(bass, t0, t1) >= mean(bass, nx["start"], min(nx["end"], nx["start"] + 32 * beat)) + 0.05):
            nx["start"] = t0                                         # the bass is all there: the chorus has begun
            a["drops"] = sorted([d for d in (a.get("drops") or []) if not (t0 - 0.5 <= d < nx["end"])] + [round(float(t0), 3)])
            changes.append((round(t0, 1), "buildup with the drop's bass, a chorus: the drop starts here"))
            continue
        heard_verse = t1 - t0 >= 24 * beat and says.get("verse", 0.0) >= 0.8
        if heard_verse:                                              # a long one SongFormer calls a verse: rising to its end
            qs = [mean(en, t0 + k * q, t0 + (k + 1) * q) for k in range(3)]
            oq = [mean(other, t0 + k * q, t0 + (k + 1) * q) for k in range(3)] if other is not None else None
            climbs = (e4 - max(qs)) / loud_top >= 0.08 or (oq is not None and o4 - max(oq) >= 0.12)
            lands = False
        if accel or climbs or lands:
            out.append(sec)
            continue
        before = mean(en, max(pv["start"], pv["end"] - 8 * beat), pv["end"]) if pv else None
        if heard_verse:                                              # SongFormer's verse, and nothing builds: a verse
            if pv is not None and pv.get("type") == "verse":
                pv["end"] = t1
            else:
                out.append({**sec, "type": "verse"})
            changes.append((round(t0, 1), "flat buildup SongFormer hears as a verse: a verse"))
        elif into_drop and (pv is None or (e_all - before) / loud_top >= 0.25):
            nx["start"] = t0                                         # the drop arrives here
            a["drops"] = sorted([d for d in (a.get("drops") or []) if not (t0 - 0.5 <= d < nx["end"])] + [round(float(t0), 3)])
            changes.append((round(t0, 1), "flat buildup: the drop starts here"))
        elif pv is not None and pv.get("type") == "drop" and into_drop:
            pv["end"] = t1
            changes.append((round(t0, 1), "flat buildup: inside the drop"))
        elif not into_drop and (mean(drums, t0, t1) or 0.0) < 0.3:
            out.append({**sec, "type": "breakdown"})
            changes.append((round(t0, 1), "flat buildup: a breakdown"))
        elif pv is not None:
            pv["end"] = t1
            changes.append((round(t0, 1), f"flat buildup: more {pv.get('type')}"))
        else:
            out.append({**sec, "type": "intro"})
            changes.append((round(t0, 1), "flat buildup: the intro"))
    a["sections"] = out
    return changes


def drop_landings(a: dict) -> list:
    """A drop lands where the bass comes back and the drums go full after a build without bass. A bar line
    where the two bars before have no bass (under 0.1), and the two after have it (0.2 or more), the drums
    at full (0.85, from under 0.8) and the song louder (by 0.06) is a landing:

    - a drop with one inside it, more than 4 s in, starts there; what comes before is its buildup (a snare
      roll: 1.5 snares a beat or more) or else a verse (Forester's All I Need: the drop was put at 2:07,
      where the roll starts, and lands at 2:22.9, where everything comes in);
    - a verse or breakdown with one, and full drums (0.8 or more, two bars at a time) for 8 bars after, has
      a drop there, to where the drums fall away (under 0.5), and its buildup is the snare roll before it,
      up to 8 bars (the same song's first drop, at 1:03.5, was inside an 87 s verse).

    Changes the analysis in place; returns what changed."""
    secs = a.get("sections") or []
    bass, dp = _curve(a, "bass"), _curve(a, "drums_present")
    en = np.asarray(a.get("energy") or [], dtype=float)
    bars = sorted(float(b) for b in (a.get("downbeats") or []))
    if not secs or bass is None or dp is None or len(en) == 0 or len(bars) < 8 or not a.get("bpm"):
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beat = 60.0 / float(a["bpm"])
    two = 8 * beat

    def mean(c, t0, t1):
        i0, i1 = max(0, int(t0 * fps)), int(t1 * fps)
        return float(c[i0:max(i0 + 1, i1)].mean()) if i0 < len(c) else 0.0
    ev = a.get("events") or {}
    snares = np.array(sorted(float(e[0]) for e in (ev.get("snare") or []) + (ev.get("roll") or [])))
    rolling = lambda t0, t1: float(((snares >= t0) & (snares < t1)).sum()) / max(1e-6, (t1 - t0) / beat) >= 1.5

    def lands(t):
        return (mean(bass, t - two, t) < 0.1 and mean(bass, t, t + two) >= 0.2 and mean(dp, t, t + two) >= 0.85
                and mean(dp, t - two, t) < 0.8 and mean(en, t, t + two) - mean(en, t - two, t) >= 0.06)

    out, changes = [], []
    for s in secs:
        start, end, kind = float(s["start"]), float(s["end"]), s.get("type")
        if kind == "drop":
            t = next((b for b in bars if start + 4 < b < min(end - two, start + 40) and lands(b)), None)
            if t is None:
                out.append(s)
                continue
            out.append({**s, "end": t, "type": "buildup" if rolling(max(start, t - two), t) else "verse"})
            out.append({**s, "start": t})
            a["drops"] = sorted([d for d in (a.get("drops") or []) if not (start - 0.5 <= d < end)] + [round(t, 3)])
            changes.append((round(start, 1), "drop lands at", round(t, 1)))
            continue
        if kind not in ("verse", "breakdown"):
            out.append(s)
            continue
        cur = s
        for b in bars:
            c0, c1 = float(cur["start"]), float(cur["end"])
            if not (c0 + two <= b < c1 - 4 * two) or not lands(b):
                continue
            if not all(mean(dp, b + k * two, b + (k + 1) * two) >= 0.8 for k in range(4)):
                continue
            e1 = next((x for x in bars if b + 4 * two <= x < c1 and mean(dp, x, x + two) < 0.5), c1)
            b0 = b
            while b0 - two >= c0 and b - b0 < 4 * two and rolling(b0 - two, b0) and mean(bass, b0 - two, b0) < 0.15:
                b0 -= two
            b0 = min(bars, key=lambda x: abs(x - b0))
            if b0 > c0 + 1:
                out.append({**cur, "end": b0})
            if b - b0 > 1:
                out.append({**cur, "start": b0, "end": b, "type": "buildup"})
            out.append({**cur, "start": b, "end": e1, "type": "drop"})
            a["drops"] = sorted((a.get("drops") or []) + [round(b, 3)])
            changes.append((round(b, 1), f"drop in a {kind}", round(b0, 1), round(e1, 1)))
            cur = {**cur, "start": e1}
            if e1 >= c1 - 1:
                cur = None
                break
        if cur is not None:
            out.append(cur)
    a["sections"] = out
    return changes


def _bar_levels(a: dict, t0: float, t1: float) -> list:
    """The bars (downbeat to downbeat) inside t0..t1, each with its mean loudness (a share of the song's loud
    level), bass, drums present, drum loudness, vocals and the other instruments: [(b0, b1, {...})]."""
    en = np.asarray(a.get("energy") or [], dtype=float)
    bars = [float(b) for b in a.get("downbeats") or []]
    if len(en) == 0 or len(bars) < 3:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    top = float(np.percentile(en, 90)) or 1.0
    curves = {"e": en / top, **{k: _curve(a, n) for k, n in (("bass", "bass"), ("dp", "drums_present"), ("drum", "drum"),
                                                              ("voc", "vocals"), ("other", "other"))}}

    def mean(c, b0, b1):
        if c is None:
            return 0.0
        i0 = max(0, int(b0 * fps))
        return float(c[i0:max(i0 + 1, int(b1 * fps))].mean()) if i0 < len(c) else 0.0
    edges = [b for b in bars if t0 - 0.05 <= b <= t1 + 0.05]
    return [(b0, b1, {k: mean(c, b0, b1) for k, c in curves.items()}) for b0, b1 in zip(edges, edges[1:])]


def _split(secs: list, i: int, parts: list) -> None:
    """Section i replaced by parts [(start, end, type)], each keeping the section's other fields; parts shorter
    than half a second are left out."""
    secs[i:i + 1] = [{**secs[i], "start": round(p0, 3), "end": round(p1, 3), "type": k} for p0, p1, k in parts if p1 - p0 >= 0.5]


def _join_same(secs: list) -> list:
    """Two breakdowns (or outros) side by side are one."""
    out = []
    for x in secs:
        if out and out[-1].get("type") == x.get("type") and abs(out[-1]["end"] - x["start"]) < 0.05 and x.get("type") in ("breakdown", "outro"):
            out[-1] = {**out[-1], "end": x["end"]}
        else:
            out.append(x)
    return out


def drums_and_bass_place_drops(a: dict) -> list:
    """Where a drop is, from the drums, the bass and the voice, bar by bar (WannaCry by Ninajirachi and Porter
    Robinson, and Brooke Wheeler's whisper, had drops in the wrong places, a 39 s buildup and drops running on):

    - an instrumental drop: 2 bars or more (7 s or more) of a verse without a voice (vocals under 0.12) as
      big as the song's drops (drums there, 0.75 or more, and at least as loud as the drops' drums; bass
      and loudness within 0.12 and 0.1 of theirs) is a drop (an intro stays an intro, however big: The
      Maine's how to exit a room) (whisper's opening 0:02 to 0:19: the heaviest
      drums of the song, no voice);
    - a drop starts with its bass: when its first bars (2 or more, within its first 40%) have none (under
      0.15) and are quieter (by 0.1) than the rest of it, which has (0.4), those bars are the buildup into
      it (as loud as the drop, they are the drop: Passion Pit's Sleepyhead); of a longer stretch (over 4
      bars) only the last 4 build, the rest is a verse (a breakdown without drums: under 0.4) (WannaCry's last drop came
      14 s early, over the build's bare kick and snares);
    - a drop ends where its drums stop: its last bars with the drums gone (under 0.35) belong to what
      follows (whisper's last drop ran 4 s into the outro);
    - a bridge inside a drop: 4 bars or more (7 s or more) after its first where the drums and the other
      instruments both fall well back (under 0.65 of the drop's strong bars) and SongFormer hears a bridge
      (60%) is a breakdown (whisper, 1:47 to 1:58);
    (A long buildup: long_buildups.)

    Changes the analysis in place; returns what changed."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    drops = [x for x in secs if x.get("type") == "drop"]
    if len(secs) < 2 or not drops or _curve(a, "drums_present") is None or _curve(a, "bass") is None:
        return []
    allbars = [lv for d in drops for _, _, lv in _bar_levels(a, d["start"], d["end"])]
    if not allbars:
        return []
    ref = {k: float(np.mean([b[k] for b in allbars])) for k in allbars[0]}
    changes = []
    # instrumental drops in verses and intros
    i = 0
    while i < len(secs):
        x = secs[i]
        if x.get("type") == "verse" and _curve(a, "vocals") is not None:
            bl = _bar_levels(a, x["start"], x["end"])
            big = [lv["voc"] < 0.12 and lv["dp"] >= 0.75 and lv["drum"] >= ref["drum"] and lv["bass"] >= ref["bass"] - 0.12
                   and lv["e"] >= ref["e"] - 0.1 for _, _, lv in bl]
            run = None
            for j in range(len(bl)):
                if not big[j]:
                    continue
                k = j
                while k + 1 < len(bl) and big[k + 1]:
                    k += 1
                if k - j + 1 >= 2 and bl[k][1] - bl[j][0] >= 7.0:
                    run = (bl[j][0], bl[k][1])
                    break
            if run:
                r0 = x["start"] if run[0] - x["start"] <= (bl[0][1] - bl[0][0]) + 0.1 else run[0]   # (a bar's lead-in joins it)
                r1 = x["end"] if x["end"] - run[1] < 1.0 else run[1]
                _split(secs, i, [(x["start"], r0, x["type"]), (r0, r1, "drop"), (r1, x["end"], x["type"])])
                changes.append((round(r0, 1), "an instrumental drop (no voice, as big as the drops)"))
                i += 1
                continue
        i += 1
    # each drop: its start with the bass, its end with the drums, a bridge inside it
    i = 0
    while i < len(secs):
        x = secs[i]
        if x.get("type") != "drop":
            i += 1
            continue
        bl = _bar_levels(a, x["start"], x["end"])
        n = len(bl)
        if n >= 6:
            j = 0
            while j < n and j < 0.4 * n and bl[j][2]["bass"] >= 0.15:
                j += 1
            k = j
            while k < n and bl[k][2]["bass"] < 0.15:
                k += 1
            rest = [lv["bass"] for _, _, lv in bl[k:]]
            quieter = k > j and float(np.mean([lv["e"] for _, _, lv in bl[j:k]])) <= float(np.mean([lv["e"] for _, _, lv in bl[k:]] or [0])) - 0.1
            if j < 0.4 * n and k - j >= 2 and k < n and rest and float(np.mean(rest)) >= 0.4 and n - k >= 4 and quieter:
                t, b4 = bl[k][0], bl[max(0, k - 4)][0]               # (a buildup is its last 4 bars at most)
                if i > 0 and secs[i - 1].get("type") == "buildup" and b4 <= x["start"] + 0.05:
                    secs[i - 1] = {**secs[i - 1], "end": round(t, 3)}
                    _split(secs, i, [(t, x["end"], "drop")])
                else:
                    calm = "verse" if float(np.mean([lv["dp"] for _, _, lv in bl[:max(0, k - 4)]] or [1])) >= 0.4 else "breakdown"
                    parts = [(x["start"], b4, calm), (b4, t, "buildup"), (t, x["end"], "drop")]
                    _split(secs, i, parts)
                    i += len([q for q in parts if q[1] - q[0] >= 0.5]) - 1
                changes.append((round(t, 1), "the drop starts with its bass"))
                x = secs[i]
                bl = _bar_levels(a, x["start"], x["end"])
                n = len(bl)
        if n >= 5:
            k = n
            while k > 0 and bl[k - 1][2]["dp"] < 0.35:
                k -= 1
            if 0 < n - k and k >= 4:
                t = bl[k][0]
                nx = secs[i + 1] if i + 1 < len(secs) else None
                if nx is not None and nx.get("type") in ("outro", "breakdown", "verse"):
                    secs[i + 1] = {**nx, "start": round(t, 3)}
                    secs[i] = {**x, "end": round(t, 3)}
                else:
                    _split(secs, i, [(x["start"], t, "drop"), (t, x["end"], "outro" if nx is None else "breakdown")])
                changes.append((round(t, 1), "the drop ends where its drums stop"))
                x = secs[i]
                bl = _bar_levels(a, x["start"], x["end"])
                n = len(bl)
        if n >= 6:
            own = {k: float(np.percentile([lv[k] for _, _, lv in bl], 75)) for k in ("drum", "other")}   # (its strong bars)
            low = [lv["drum"] < 0.65 * own["drum"] and lv["other"] < 0.65 * own["other"] for _, _, lv in bl]
            for j in range(1, n):
                if not low[j]:
                    continue
                k = j
                while k + 1 < n and low[k + 1]:
                    k += 1
                r0, r1 = bl[j][0], bl[k][1]
                if k - j + 1 >= 4 and r1 - r0 >= 7.0 and (_model_says(a, r0, r1, raw=True) or {}).get("bridge", 0.0) >= 0.6:
                    tail = x["end"] if x["end"] - r1 < 1.0 else r1
                    _split(secs, i, [(x["start"], r0, "drop"), (r0, tail, "breakdown"), (tail, x["end"], "drop")])
                    changes.append((round(r0, 1), "a bridge inside the drop"))
                    break
        i += 1
    if changes:
        secs = _join_same(secs)
        a["sections"] = secs
        a["drops"] = sorted(round(float(x["start"]), 3) for x in secs if x.get("type") == "drop")
    return changes


def verses_without_synths(a: dict) -> list:
    """A verse hidden in a long drop: in a drop of 45 s or more, a stretch of 6 bars or more (12 s or more)
    where the other instruments (the synths, the guitars) fall to half of the drop's full bars or less while
    the drums play on (within 0.2) is a verse. Astrale's Frisson, mastered loud from end to end, was one drop
    of 184 s; its verses (0:34, 1:42) drop the synths to almost nothing and the voice carries them. Learned
    from the songs the user set right by ear (shows/_jam/section_truth.json): 3 of their stretches found,
    none of their drops cut; 7 of the 246 learned songs change. Changes the analysis in place; returns what
    changed."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    if _curve(a, "other") is None or _curve(a, "drums_present") is None:
        return []
    changes, i = [], 0
    while i < len(secs):
        x = secs[i]
        if x.get("type") != "drop" or x["end"] - x["start"] < 45.0:
            i += 1
            continue
        bl = _bar_levels(a, x["start"], x["end"])
        if len(bl) < 6:
            i += 1
            continue
        full = float(np.percentile([lv["other"] for _, _, lv in bl], 75))
        drums = float(np.percentile([lv["dp"] for _, _, lv in bl], 75))
        low = [lv["other"] <= 0.5 * full and lv["dp"] >= drums - 0.2 for _, _, lv in bl]
        parts, j, t = [], 0, x["start"]
        while j < len(bl):
            if not low[j]:
                j += 1
                continue
            k = j
            while k + 1 < len(bl) and low[k + 1]:
                k += 1
            if k - j + 1 >= 6 and bl[k][1] - bl[j][0] >= 12.0:
                r0 = x["start"] if bl[j][0] - x["start"] < 1.0 else bl[j][0]
                r1 = x["end"] if x["end"] - bl[k][1] < 1.0 else bl[k][1]
                parts += [(t, r0, "drop"), (r0, r1, "verse")]
                t = r1
                changes.append((round(r0, 1), "a verse in a long drop: the synths gone, the drums on"))
            j = k + 1
        if parts:
            parts.append((t, x["end"], "drop"))
            before = len(secs)
            _split(secs, i, parts)
            i += len(secs) - before + 1
        else:
            i += 1
    if changes:
        a["sections"] = secs
        a["drops"] = sorted(round(float(x["start"]), 3) for x in secs if x.get("type") == "drop")
    return changes


def long_buildups(a: dict) -> list:
    """A long buildup (20 s or more) starts where its drums come in (0.4 or more, through to its end but for
    a last bar of the pause before the drop), when there are 8 s or more before that without them (under
    0.3 on average): those are a breakdown. WannaCry (Ninajirachi and Porter Robinson): its soft chorus,
    0:57 to 1:25, was part of a 39 s buildup; the build is 1:25 to 1:35. Run after drops_on_their_bar, so
    the drop's own first bar is not taken for the drums coming in. Changes the analysis in place; returns
    what changed."""
    secs = [dict(x) for x in (a.get("sections") or [])]
    if _curve(a, "drums_present") is None:
        return []
    changes, i = [], 0
    while i < len(secs):
        x = secs[i]
        i += 1
        if x.get("type") != "buildup" or x["end"] - x["start"] < 20.0:
            continue
        bl = _bar_levels(a, x["start"], x["end"])
        k = len(bl)
        if k and bl[-1][2]["dp"] < 0.3:
            k -= 1                                                   # (the pause before the drop)
        j = k
        while j > 0 and bl[j - 1][2]["dp"] >= 0.4:
            j -= 1
        if j == k or j == 0:
            continue
        t = bl[j][0]
        if t - x["start"] >= 8.0 and float(np.mean([lv["dp"] for _, _, lv in bl[:j]])) < 0.3:
            secs[i - 1] = {**x, "start": round(t, 3)}
            secs.insert(i - 1, {**x, "end": round(t, 3), "type": "breakdown"})
            changes.append((round(x["start"], 1), "a long buildup starts where its drums come in"))
            i += 1
    if changes:
        a["sections"] = _join_same(secs)
    return changes


def _curve(a: dict, name: str):
    c = (a.get("curves") or {}).get(name)
    return np.asarray(c, dtype=float) if c else None


def shift_beats(a: dict, seconds: float) -> None:
    """Move an analysis' whole beat grid (its beats, bar lines and first beat) by seconds, in place; a beat
    pushed before the start is dropped, and one comes in at the start where the gap allows (the bar count
    from the first beat kept by moving the downbeat phase with it)."""
    beats = [round(float(b) + seconds, 4) for b in a.get("beats") or []]
    if not beats:
        return
    period = (beats[-1] - beats[0]) / max(1, len(beats) - 1)
    phase = int(a.get("downbeat_phase") or 0)
    while beats and beats[0] < 0:
        beats.pop(0)
        phase = (phase - 1) % 4
    while beats and beats[0] - period >= 0:
        beats.insert(0, round(beats[0] - period, 4))
        phase = (phase + 1) % 4
    a["beats"], a["downbeat_phase"] = beats, phase
    if beats:
        a["beat_offset"] = beats[0]
    a["downbeats"] = beats[phase::4]


# The onset envelope the grid is fitted to (librosa's, hop 512 with a 2048-sample window) peaks ~28 ms after a
# drum's attack: measured on 7 songs with a steady kick, each kick's attack found by the spectral flux at 2.9 ms
# steps (2026-10-18): the beat grid 26.8 to 32.3 ms late (median 28.8), the kick events 13 ms early. So the
# grids placed on that envelope (the onset fit of Beat This' beats, librosa's own) are moved back onto the
# attacks; Beat This' grid taken as it is was never fitted to it.
GRID_LAG = 0.028


def grid_on_attacks(a: dict) -> list:
    """Move a grid fitted to the onset envelope GRID_LAG earlier, onto the attacks: once (flagged
    grid_on_attack), for a new analysis and a saved one alike. Returns what changed."""
    if a.get("grid_on_attack") or not a.get("beats"):
        return []
    a["grid_on_attack"] = True
    src = str(a.get("beat_source") or "librosa")
    if "onset" not in src and src != "librosa":
        return []
    shift_beats(a, -GRID_LAG)
    return [(0.0, f"beat grid onto the attacks ({-GRID_LAG * 1000:.0f} ms)")]


HIT_LEAD = 0.013        # the kick and snare events lead their attacks by ~13 ms (measured with GRID_LAG)


def grid_tempo_from_hits(a: dict) -> list:
    """A grid at the wrong tempo drifts off the music: Charlie Puth's Light Switch was fitted at 91.81 bpm, the
    song 92.0, and its kicks and snares slid 2.2 ms a second against the beats (370 ms over the song: the lights
    went off and back every minute and a half). Across 222 learned songs the next drifted 45 ms in six minutes.

    The tempo within 0.8% of the grid's at which the drum hits (kicks and snares, 64 or more) sit closest to one
    lattice of quarter beats is found; when they sit clearly closer than on the grid (0.1 more of a 0..1 fit,
    and 0.6 or more) and it is 0.05% away or more, the grid is laid again at that tempo: through the hits (as
    far ahead of the beat as the events always are, HIT_LEAD), on the quarter of a beat nearest the old grid
    mid-song, the bars counted on from there. Once (flagged grid_tempo_checked). Returns what changed."""
    if a.get("grid_tempo_checked") or len(a.get("beats") or []) < 32:
        return []
    a["grid_tempo_checked"] = True
    ev = a.get("events") or {}
    h = np.sort(np.asarray([float(e[0]) for k in ("kick", "snare") for e in (ev.get(k) or [])], dtype=float))
    if len(h) < 64:
        return []
    beats = np.asarray(a["beats"], dtype=float)
    P = float((beats[-1] - beats[0]) / (len(beats) - 1))
    mid = float(np.median(h))
    fit = lambda per: float(np.abs(np.mean(np.exp(2j * np.pi * (h - mid) / (per / 4)))))
    cands = P * (1 + np.linspace(-0.008, 0.008, 1601))
    scores = np.array([fit(c) for c in cands])
    best = float(cands[int(np.argmax(scores))])
    if scores.max() < 0.6 or scores.max() - fit(P) < 0.1 or abs(best / P - 1) < 0.0005:
        return []
    q = best / 4
    phi = float(np.angle(np.mean(np.exp(2j * np.pi * (h - mid) / q)))) / (2 * np.pi) * q
    lattice = mid + phi + HIT_LEAD                                   # a point of the hits' quarter-beat lattice, as a beat
    i_old = int(np.argmin(np.abs(beats - mid)))
    anchor = lattice + round((beats[i_old] - lattice) / q) * q       # the quarter beat nearest the old grid mid-song
    j0 = int(np.floor(anchor / best))
    new = anchor - j0 * best + best * np.arange(0, int((float(a.get("duration") or beats[-1]) - (anchor - j0 * best)) / best) + 1)
    new = new[new >= 0]
    i_new = int(np.argmin(np.abs(new - anchor)))
    phase = (int(a.get("downbeat_phase") or 0) + (i_new - i_old)) % 4
    a["beats"] = [round(float(b), 4) for b in new]
    a["downbeat_phase"] = phase
    a["downbeats"] = a["beats"][phase::4]
    a["beat_offset"] = a["beats"][0]
    old_bpm = float(a.get("bpm") or 60.0 / P)
    a["bpm"] = round(60.0 / best, 3)
    return [(0.0, f"tempo {old_bpm:.2f} -> {a['bpm']:.2f} bpm: the drums drifted off the grid")]


def grid_on_the_backbeat(a: dict) -> list:
    """The snare backbeat lands on the grid's beats or half beats. Where 40 or more snares sit together (0.6 or
    more of a 0..1 fit) between them, a third of a half beat or more away, the grid is half a real beat off:
    on a grid counting a fast song at half its tempo, a quarter of a counted beat. Moved onto the snares.
    Found in 3 of 246 learned songs (sleepover, WANTURBODY, Sad Machine: the snares 165-201 ms off the
    beat); All-In-One's own beats sat where the snares are in both it had run on.
    Once (flagged grid_backbeat_checked). Returns what changed."""
    if a.get("grid_backbeat_checked") or len(a.get("beats") or []) < 16:
        return []
    a["grid_backbeat_checked"] = True
    sn = np.asarray([float(e[0]) for e in ((a.get("events") or {}).get("snare") or [])], dtype=float) + HIT_LEAD
    if len(sn) < 40:
        return []
    beats = np.asarray(a["beats"], dtype=float)
    half = float(np.median(np.diff(beats))) / 2
    z = np.mean(np.exp(2j * np.pi * ((sn - beats[0]) / half)))
    off = float(np.angle(z) / (2 * np.pi))
    if abs(z) < 0.6 or abs(off) < 0.3:
        return []
    # either way puts the snares on the half-beat lattice; the one with more kicks on the beat itself is the beat
    kicks = np.asarray([float(e[0]) for e in ((a.get("events") or {}).get("kick") or [])], dtype=float) + HIT_LEAD
    on_beat = lambda s: float(np.mean(np.cos(2 * np.pi * (kicks - beats[0] - s) / (2 * half)))) if len(kicks) else 0.0
    shift = max((off * half, (off - np.sign(off)) * half), key=on_beat)
    shift_beats(a, shift)
    return [(0.0, f"beat grid onto the snare backbeat ({shift * 1000:+.0f} ms)")]


def settle_sections(a: dict) -> list:
    """The rules that correct where sections start and what they are, from the stems and the hook: run
    on every analysis, new or saved (a learned jam song, the cache), so they apply without relearning.
    Always from the sections as first found (kept as sections_found): run twice, the same answer."""
    if "sections_found" in a:
        a["sections"] = [dict(s) for s in a["sections_found"]]
        a["drops"] = list(a.get("drops_found") or [])
    else:
        a["sections_found"] = [dict(s) for s in a.get("sections") or []]
        a["drops_found"] = list(a.get("drops") or [])
    grid = grid_on_attacks(a) + grid_tempo_from_hits(a) + grid_on_the_backbeat(a)
    from . import songmap
    if a.get("sections_truth"):                             # a song the user set right by ear: their sections, as they are
        a["sections"] = [dict(s) for s in a["sections_truth"]]   # (the rules' would be thrown away: not run, ~70 ms)
        a["drops"] = [round(float(s["start"]), 3) for s in a["sections"] if s.get("type") == "drop"]
        songmap.add(a)
        a.pop("_songmap_bars", None)
        return grid + [(0.0, "the sections the user set by ear")]
    changes = grid + (choruses_in_long_sections(a) + verses_in_long_drops(a) + drumless_drops(a) + drop_landings(a) + loud_buildups(a) + drop_after_pause(a) + hook_drops(a) + drop_pickup(a) + drums_start_verse(a)
               + drummed_breakdowns(a) + repeated_drops(a) + drops_where_none(a) + drops_by_sound(a) + drop_neighbors(a)
               + model_choruses(a) + drop_tails_and_releases(a) + drums_and_bass_place_drops(a) + verses_without_synths(a) + drops_on_their_bar(a) + gap_before_section(a) + outro_where_it_rings_out(a) + long_buildups(a) + flat_buildups(a) + sections_on_bar_lines(a))
    songmap.add(a)                                          # the song's map on the rules' sections (the labeler reads it)
    final = None
    if a.get("track_id") and not a.get("_no_labeler"):      # the learned labeler's sections, where it can (labeler.py)
        try:
            from . import labeler
            got = labeler.apply(a)
        except Exception as e:  # noqa: BLE001 - the rules' sections stand
            print(f"[labeler] {a.get('track_id')}: {type(e).__name__}: {e}")
            got = None
        if got:
            final, why = got, "the learned labeler's sections"
    if final is not None:
        a["sections_rules"] = [dict(s) for s in a["sections"]]
        a["sections"] = final
        a["drops"] = [round(float(s["start"]), 3) for s in final if s.get("type") == "drop"]
        changes = changes + [(0.0, why)]
        songmap.add(a)                                      # (and its map again, on the sections that play)
    a.pop("_songmap_bars", None)                            # (the map's bars, kept between its two runs: not saved)
    return changes


def hook_drops(a: dict) -> list:
    """Drops are where the song's hook plays. Where the hook (the riff the song comes back to, see
    events) plays nearly only inside drops, it marks the choruses, and:

    - a drop it never plays in, that has no snare backbeat either (under 0.4 of the hooked drops'),
      is a verse (Coldplay's Higher Power at 0:52: a synth stretch without snares);
    - a long drop whose hooks stop for 10 bars or more, while the melody falls away, is two drops
      with a verse between (Higher Power's bridge, 2:05 to 2:31, was inside one 100 s drop).

    Songs whose hook is found in only some of their drops are left alone. Changes the analysis in
    place (a saved one too, on load); returns what changed."""
    hook = a.get("hook") or {}
    times = sorted(float(t) for t in (hook.get("times") or []))
    secs = [dict(s) for s in (a.get("sections") or [])]
    if len(times) < 3 or not secs:
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beats = [float(b) for b in a.get("beats") or []]
    bars = beats[int(a.get("downbeat_phase") or 0)::4]
    if len(bars) < 8:
        return []
    bar = float(np.median(np.diff(bars)))
    other, lead = _curve(a, "other"), _curve(a, "lead")
    mel = other if lead is None else (other + lead) / 2 if other is not None else lead
    if mel is None:
        return []
    lvl = lambda t0, t1: float(mel[max(0, int(t0 * fps)):max(int(t0 * fps) + 1, int(t1 * fps))].mean()) if int(t0 * fps) < len(mel) else 0.0
    en = np.asarray(a.get("energy") or [0.0], dtype=float)
    loud = lambda t0, t1: float(en[max(0, int(t0 * fps)):max(int(t0 * fps) + 1, int(t1 * fps))].mean()) if int(t0 * fps) < len(en) else 0.0
    drops = [s for s in secs if s.get("type") == "drop"]
    inside = lambda s: [t for t in times if s["start"] - 0.5 <= t < s["end"]]
    in_drops = sum(len(inside(s)) for s in drops)
    # the hook marks the choruses only where nearly all of it plays inside drops, in more than one place
    if not drops or in_drops < 0.75 * len(times) or len(times) < 3:
        return []
    period = float(np.median(np.diff(times))) if len(times) > 1 else 4 * bar
    ev = a.get("events") or {}
    snares = np.array(sorted(float(e[0]) for e in (ev.get("snare") or [])))
    snare_rate = lambda t0, t1: float(((snares >= t0) & (snares < t1)).sum()) / max(1e-6, (t1 - t0) / bar)
    hooked = [d for d in drops if inside(d)]
    backbeat = float(np.mean([snare_rate(d["start"], d["end"]) for d in hooked])) if hooked else 0.0
    sung = float(np.mean([lvl(d["start"], d["end"]) for d in hooked])) if hooked else 0.0
    near_bar = lambda t: min(bars, key=lambda b: abs(b - t))
    changes, out = [], []
    for s in secs:
        if s.get("type") != "drop":
            out.append(s)
            continue
        hs = inside(s)
        if not hs:                                    # a drop the hook never plays in: a verse, when...
            no_beat = all(inside(d) for d in drops if d is not s) and len(drops) >= 2 and backbeat >= 1.0 \
                and snare_rate(s["start"], s["end"]) < 0.4 * backbeat and lvl(s["start"], s["end"]) < sung
            # ...the hook marks two or more other drops and this one's melody is clearly quieter than theirs
            # (James Ivy's Last Star at 0:43 and 1:59: the hook-less part after a verse, 0.61 to 0.77)
            prev = out[-1] if out else None
            lifts = prev is not None and loud(s["start"], s["end"]) - loud(prev["start"], prev["end"]) >= 0.1
            quieter = len(hooked) >= 2 and lvl(s["start"], s["end"]) < 0.85 * sung and not lifts
            # (but one that leaps out of what came before stays a drop: Kitten's ME at 0:48, 0.3 to 0.8)
            if no_beat or quieter:
                out.append({**s, "type": "verse"})
                changes.append((round(s["start"], 1), "verse (no hook)"))
                continue
            out.append(s)
            continue
        # groups of hooks: a long gap between two (over 2.5 hook periods) is no chorus
        groups, g = [], [hs[0]]
        for t in hs[1:]:
            if t - g[-1] > 2.5 * period and t - g[-1] >= 10 * bar:
                groups.append(g); g = [t]
            else:
                g.append(t)
        groups.append(g)
        if groups[0][0] - s["start"] > 3 * bar:      # the hook comes late in this drop: only its start may move
            groups = [[s["start"]] + [t for gg in groups for t in gg]]
        pieces = []
        cur_start = s["start"]
        for i, g in enumerate(groups):
            # where the drop lands: the bar near its first hook where the melody lifts most
            cands = [b for b in bars if g[0] - 2.2 * bar <= b <= g[0] + 0.5 * bar and b >= cur_start - 0.1 * bar]
            if i == 0:
                cands = [b for b in cands if s["start"] - 0.1 <= b <= s["start"] + 2.1 * bar] or [s["start"]]
            rise = lambda b: lvl(b, b + bar) - lvl(b - bar, b)
            land = max(cands, key=rise) if cands else s["start"]
            if i == 0:
                land = s["start"]                     # a drop's own start stands (moving it to where the melody lifts
                                                      # suited Higher Power and put If Youre Too Shy's last drop 3 s late)
            if i > 0:
                pieces.append(("verse", cur_start, land))
            # where it ends, if another group follows: the bar after its last hook where the melody falls most
            if i < len(groups) - 1:
                ends = [b for b in bars if g[-1] + bar <= b <= g[-1] + period + bar]
                fall = lambda b: lvl(b - 2 * bar, b) - lvl(b, b + 2 * bar)
                end = max(ends, key=fall) if ends else g[-1] + period
                nxt = groups[i + 1][0]
                if lvl(end, nxt - bar) > 0.75 * lvl(land, end):     # the melody does not fall away between: one drop
                    groups[i + 1] = g + groups[i + 1]
                    continue
                pieces.append(("drop", land, end))
                cur_start = end
            else:
                pieces.append(("drop", land, s["end"]))
        if pieces[0][1] - s["start"] >= 0.5 * bar:     # the drop lands later than found: what comes before runs on to it
            if out and abs(out[-1]["end"] - s["start"]) < 0.01:
                out[-1] = {**out[-1], "end": round(pieces[0][1], 3)}
            else:
                pieces.insert(0, ("verse", s["start"], pieces[0][1]))
        for typ, t0, t1 in pieces:
            if t1 - t0 >= bar - 0.01:
                out.append({**s, "type": typ, "start": round(t0, 3), "end": round(t1, 3)})
        if len(pieces) > 1 or abs(pieces[0][1] - s["start"]) > 0.1:
            changes.append((round(s["start"], 1), [(p[0], round(p[1], 1), round(p[2], 1)) for p in pieces]))
    if not changes:
        return []
    # a verse next to a verse is one
    merged = []
    for s in out:
        if merged and merged[-1]["type"] == s["type"] == "verse" and abs(merged[-1]["end"] - s["start"]) < 0.01:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)
    a["sections"] = merged
    a["drops"] = [s["start"] for s in merged if s["type"] == "drop" and not any(p["type"] == "drop" and abs(p["end"] - s["start"]) < 0.01 for p in merged)]
    return changes


def drums_start_verse(a: dict) -> list:
    """An intro ends where the drums come in to stay, not a bar or two later (Higher Power: the drums and
    the voice come in at 0:14, the verse was found at 0:17). Returns what changed."""
    dp = _curve(a, "drums_present")
    secs = a.get("sections") or []
    if dp is None or len(secs) < 2 or secs[0].get("type") != "intro":
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beats = [float(b) for b in a.get("beats") or []]
    bars = beats[int(a.get("downbeat_phase") or 0)::4]
    if len(bars) < 4:
        return []
    bar = float(np.median(np.diff(bars)))
    lvl = lambda t0, t1: float(dp[max(0, int(t0 * fps)):max(int(t0 * fps) + 1, int(t1 * fps))].mean()) if int(t0 * fps) < len(dp) else 0.0
    end = float(secs[0]["end"])
    for b in [x for x in bars if end - 2.5 * bar <= x <= end - 0.5 * bar]:
        if lvl(b - bar, b) < 0.3 and all(lvl(t, t + bar) >= 0.4 for t in (b, b + bar, b + 2 * bar)) and b - secs[0]["start"] >= 2 * bar:
            secs[0] = {**secs[0], "end": round(b, 3)}
            secs[1] = {**secs[1], "start": round(b, 3)}
            a["sections"] = secs
            return [(round(end, 1), "->", round(b, 1))]
    return []


def drop_after_pause(a: dict) -> list:
    """A drop that comes out of a pause starts at the hit that ends it. Where, in the two beats before a
    drop, the song goes quiet (under 0.4) and then jumps loud (0.7 or more, and staying up), the drop is
    that jump, on the nearest beat: The Band CAMINO's Baggy Jeans at 0:53, where the bass only fills in
    a beat later (and the bass rule had put the drop there). Across 44 learned songs it moved 13 drops
    in 8 songs, each about a beat; Great Good Fine Ok, whose drums hit before its bass lands with no
    pause, is left alone. Returns what changed."""
    e, secs = a.get("energy"), a.get("sections") or []
    beats = np.asarray(a.get("beats") or [], dtype=float)
    if not e or not secs or len(beats) < 8:
        return []
    e = np.asarray(e, dtype=float)
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beat = float(np.median(np.diff(beats)))
    w, hold = max(2, int(0.2 * fps)), int(0.5 * fps)
    changes = []
    for k, s in enumerate(secs):
        if s.get("type") != "drop" or k == 0 or s.get("hit"):
            continue
        d, best = float(s["start"]), None
        for i in range(max(w, int((d - 2.2 * beat) * fps)), int((d - 0.3 * beat) * fps)):
            before, after = e[i - w:i].mean(), e[i:i + hold].mean()
            if before < 0.4 and after >= 0.7 and e[i:i + 2 * hold].min() >= 0.45 and (best is None or after - before > best[1] + 0.05):
                best = (i, after - before)
        if best is None:
            continue
        t = best[0] / fps
        nb = float(beats[np.argmin(np.abs(beats - t))])
        at = nb if abs(nb - t) <= 0.35 * beat and nb < d - 0.1 else t
        if at <= float(secs[k - 1]["start"]) + beat:
            continue
        secs[k - 1] = {**secs[k - 1], "end": round(at, 3)}
        secs[k] = {**s, "start": round(at, 3), "hit": True}
        a["drops"] = sorted([x for x in (a.get("drops") or []) if abs(x - d) > 0.5] + [round(at, 3)])
        changes.append((round(d, 2), "->", round(at, 2)))
    if changes:
        a["sections"] = secs
    return changes


def drummed_breakdowns(a: dict) -> list:
    """A breakdown is the song pulling back: one with the drums playing through it (the drum track on in
    three quarters of it, 2 s at a time, and a backbeat of 1.5 snares a bar or more) is a verse
    (Baggy Jeans' opening 0:11 to 0:44). Returns what changed."""
    dp, secs = _curve(a, "drums_present"), a.get("sections") or []
    if dp is None or not secs or not a.get("bpm"):
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    bar = 240.0 / float(a["bpm"])
    snares = [float(x[0]) for x in ((a.get("events") or {}).get("snare") or [])]
    changes = []
    for k, s in enumerate(secs):
        if s.get("type") != "breakdown":
            continue
        t0, t1 = float(s["start"]), float(s["end"])
        blocks = [dp[int(t * fps):int((t + 2) * fps)].mean() for t in np.arange(t0, t1 - 1.9, 2.0) if int(t * fps) < len(dp)]
        if not blocks:
            continue
        on = float(np.mean([b >= 0.45 for b in blocks]))
        per_bar = sum(t0 <= x < t1 for x in snares) / max(1.0, (t1 - t0) / bar)
        if on >= 0.75 and per_bar >= 1.5:
            secs[k] = {**s, "type": "verse"}
            changes.append((round(t0, 1), "breakdown -> verse"))
    if changes:
        a["sections"] = secs
    return changes


def drop_pickup(a: dict) -> list:
    """A drop starts where its bass first comes in, even off the bar line. The bass rule starts a drop
    on the first bar line where the bass plays; but a drop whose bass comes in a beat or two early
    (and drops out for a moment before the bar line) is heard there: Nightly's the feeling at 0:42.7,
    not 0:43.9. In the two beats before a drop: the beat where the bass comes in (under 0.12 the beat
    before, 0.45 or more the beat after), from where the song stays loud (no 0.2 s under 0.45, 0.65
    on average, and louder than the beat before). Across 44 learned songs it moved 6 drops in 5
    songs; none in the studio's songs (Great Good Fine Ok included). Returns what changed."""
    secs = a.get("sections") or []
    bass, e = (a.get("curves") or {}).get("bass"), a.get("energy")
    beats = np.asarray(a.get("beats") or [], dtype=float)
    if not secs or not bass or not e or len(beats) < 8:
        return []
    bass, e = np.asarray(bass, dtype=float), np.asarray(e, dtype=float)
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beat = float(np.median(np.diff(beats)))
    m = lambda x, t0, t1: float(x[max(0, int(t0 * fps)):max(int(t0 * fps) + 1, int(t1 * fps))].mean())
    changes = []
    for k, s in enumerate(secs):
        if s.get("type") != "drop" or k == 0 or s.get("hit"):
            continue
        d = float(s["start"])
        for b in [float(x) for x in beats if d - 2.2 * beat <= x <= d - 0.5 * beat]:
            if b <= float(secs[k - 1]["start"]) + beat:
                continue
            blocks = [m(e, t, t + 0.2) for t in np.arange(b, d - 0.05, 0.2)]
            if m(bass, b - beat, b - 0.1 * beat) < 0.12 and m(bass, b, b + beat) >= 0.45 \
                    and blocks and min(blocks) >= 0.45 and m(e, b, d) >= 0.65 and m(e, b - beat, b) < m(e, b, d) - 0.1:
                secs[k - 1] = {**secs[k - 1], "end": round(b, 3)}
                secs[k] = {**s, "start": round(b, 3), "hit": True}
                a["drops"] = sorted([x for x in (a.get("drops") or []) if abs(x - d) > 0.5] + [round(b, 3)])
                changes.append((round(d, 2), "->", round(b, 2)))
                break
    if changes:
        a["sections"] = secs
    return changes


def gap_before_section(a: dict) -> list:
    """A verse, breakdown or buildup starts where its music does, not in the silence before it. Where a
    section opens on a quiet stretch (under 0.35 for a second or more, starting within half a second
    of it) and the music comes back within a bar and a quarter (0.5 or more for a second), the section
    starts there, on the bar line if one is near: the quiet is the section before ringing out
    (The 1975's Settle Down: the intro's riff stops at 0:23, the verse comes in at 0:25.3). Longer
    quiet is a break of its own and stays. Across 44 learned songs it moved 8 sections, 1.0 to 2.2 s.
    Returns what changed."""
    secs = a.get("sections") or []
    e = a.get("energy")
    beats = np.asarray(a.get("beats") or [], dtype=float)
    if not secs or not e or len(beats) < 8:
        return []
    e = np.asarray(e, dtype=float)
    fps = float(a.get("feature_fps", FEATURE_FPS))
    beat = float(np.median(np.diff(beats)))
    bars = beats[int(a.get("downbeat_phase") or 0)::4]
    blk = lambda t: float(e[max(0, int(t * fps)):max(int(t * fps) + 1, int((t + 0.25) * fps))].mean()) if int(t * fps) < len(e) else 0.0
    changes = []
    for k, s in enumerate(secs):
        if k == 0 or s.get("type") not in ("verse", "breakdown", "buildup"):
            continue
        t0, t1 = float(s["start"]), float(s["end"])
        # a quiet run that starts within half a second of the section
        t = t0 - 0.25
        while t < t0 + 0.5 and blk(t) >= 0.35:
            t += 0.25
        if t >= t0 + 0.5:
            continue
        q0 = t
        while t < t0 + 8 * beat and blk(t) < 0.35:
            t += 0.25
        if t - q0 < 1.0 or t >= t0 + 8 * beat:
            continue
        back = t                                        # the music comes back
        if np.mean([blk(x) for x in np.arange(back, back + 1.0, 0.25)]) < 0.5:
            continue
        nb = [b for b in bars if abs(b - back) <= 0.35 * beat]
        at = float(nb[0]) if nb else float(beats[np.argmin(np.abs(beats - back))])
        if at - t0 < beat or at - t0 > 5 * beat or at > t1 - 8 * beat:
            continue
        secs[k - 1] = {**secs[k - 1], "end": round(at, 3)}
        secs[k] = {**s, "start": round(at, 3)}
        changes.append((round(t0, 2), "->", round(at, 2), s["type"]))
    if changes:
        a["sections"] = secs
    return changes


def outro_where_it_rings_out(a: dict) -> list:
    """The outro starts on the bar where the song lets go: the last bar line, up to two bars before the
    outro found, after which the sound only dies away (the next bar under 0.75 of the bar before, and
    never back up to it). The 1975's Settle Down: the last hit at 3:44.75 rings out, the outro was
    found a bar later. Returns what changed."""
    secs, e = a.get("sections") or [], a.get("energy")
    beats = np.asarray(a.get("beats") or [], dtype=float)
    if len(secs) < 2 or secs[-1].get("type") != "outro" or not e or len(beats) < 8:
        return []
    e = np.asarray(e, dtype=float)
    fps = float(a.get("feature_fps", FEATURE_FPS))
    bars = beats[int(a.get("downbeat_phase") or 0)::4]
    bar = 4 * float(np.median(np.diff(beats)))
    m = lambda t0, t1: float(e[max(0, int(t0 * fps)):max(int(t0 * fps) + 1, int(t1 * fps))].mean()) if int(t0 * fps) < len(e) else 0.0
    start = float(secs[-1]["start"])
    for b in [float(x) for x in bars if start - 2.1 * bar <= x < start - 0.1 * bar]:
        before = m(b - bar, b)
        after = [m(t, t + 0.5) for t in np.arange(b + 0.25, len(e) / fps - 0.5, 0.5)]
        if before >= 0.4 and after and m(b + 0.25, b + bar) < 0.75 * before and max(after) < before and b > float(secs[-2]["start"]) + bar:
            secs[-2] = {**secs[-2], "end": round(b, 3)}
            secs[-1] = {**secs[-1], "start": round(b, 3)}
            a["sections"] = secs
            return [(round(start, 2), "outro ->", round(b, 2))]
    return []


def sections_on_bar_lines(a: dict) -> list:
    """Every section starts on a bar line (a downbeat): the nearest one, the later of two as near (the
    music has come in by then). The rules above find where the music changes; with the bar lines right,
    a change heard a beat early (a pickup hit before the drop, the bass coming in ahead of the bar) still
    belongs to the bar it leads into. Returns what changed."""
    secs = a.get("sections") or []
    beats = np.asarray(a.get("beats") or [], dtype=float)
    if len(secs) < 2 or len(beats) < 8:
        return []
    bars = beats[int(a.get("downbeat_phase") or 0)::4]
    beat = float(np.median(np.diff(beats)))
    changes = []
    for k in range(1, len(secs)):
        t = float(secs[k]["start"])
        d = np.abs(bars - t)
        if d.min() <= 0.15 * beat:
            continue
        near = bars[d <= d.min() + 0.15 * beat]
        at = float(near.max())
        if at <= float(secs[k - 1]["start"]) + beat or at >= float(secs[k]["end"]) - beat:
            continue
        if secs[k].get("type") == "drop":
            a["drops"] = sorted([x for x in (a.get("drops") or []) if abs(x - t) > 0.5] + [round(at, 3)])
        secs[k - 1] = {**secs[k - 1], "end": round(at, 3)}
        secs[k] = {**secs[k], "start": round(at, 3)}
        changes.append((round(t, 2), "on the bar", round(at, 2)))
    if changes:
        a["sections"] = secs
    return changes


_REPEAT_KEYS = ("drum", "bass", "other", "lead")


def repeated_drops(a: dict) -> list:
    """A section that repeats a drop is a drop. A verse or breakdown (8 bars or more) with the drop's
    snares (0.7 to 1.4 times as many) and either its hook at the same places (two or more, within 0.6 s)
    and nearly its sound (energy, drums, bass, other, lead within 0.08 on average), or its sound almost
    exactly (within 0.035), is that chorus again: Nightly's don't even think about it at 1:30, the
    0:34 drop over again (within 0.019, the hook 6 and 14 s in both times). Across 44 learned songs
    it changed that one section. Returns what changed."""
    secs = a.get("sections") or []
    cur = a.get("curves") or {}
    e = a.get("energy")
    if not secs or not e or not all(k in cur for k in _REPEAT_KEYS) or not a.get("bpm"):
        return []
    fps = float(a.get("feature_fps", FEATURE_FPS))
    bar = 240.0 / float(a["bpm"])
    e = np.asarray(e, dtype=float)
    curves = {k: np.asarray(cur[k], dtype=float) for k in _REPEAT_KEYS}
    snares = np.array(sorted(float(x[0]) for x in ((a.get("events") or {}).get("snare") or [])))
    hooks = sorted(float(t) for t in ((a.get("hook") or {}).get("times") or []))

    def profile(s):
        i0, i1 = int(s["start"] * fps), max(int(s["start"] * fps) + 1, int(s["end"] * fps))
        return np.array([e[i0:i1].mean()] + [curves[k][i0:i1].mean() for k in _REPEAT_KEYS])

    def snare_rate(s):
        return float(((snares >= s["start"]) & (snares < s["end"])).sum()) / max(1e-6, s["end"] - s["start"])

    def offsets(s):
        return [round(t - s["start"], 1) for t in hooks if s["start"] - 0.5 <= t < s["end"]]

    drops = [s for s in secs if s.get("type") == "drop"]
    if not drops:
        return []
    changes = []
    for k, s in enumerate(secs):
        if s.get("type") not in ("verse", "breakdown") or s["end"] - s["start"] < 8 * bar:
            continue
        ps, rs, os_ = profile(s), snare_rate(s), offsets(s)
        for d in drops:
            pd, rd = profile(d), snare_rate(d)
            dist = float(np.abs(ps - pd).mean())
            od = offsets(d)
            same_hook = len(os_) >= 2 and sum(any(abs(x - y) <= 0.6 for y in od) for x in os_) >= 2
            close_drums = rd > 0 and 0.7 <= rs / rd <= 1.4
            if close_drums and ((same_hook and dist <= 0.08) or dist <= 0.035):
                secs[k] = {**s, "type": "drop"}
                a["drops"] = sorted(set((a.get("drops") or []) + [round(float(s["start"]), 3)]))
                changes.append((round(s["start"], 1), s["type"] + " -> drop", "like", round(d["start"], 1), round(dist, 3), same_hook))
                break
    if changes:
        a["sections"] = secs
    return changes


def refine_drops(sections: list[dict], drops: list, beats, stems: dict) -> tuple[list[dict], list, list]:
    """Start each drop on the beat its bass comes in, as it is heard.

    Loudness puts a drop on the bar line where the drums hit, but you hear the drop when the bass
    lands, and in many songs that is a beat or a few later (Great Good Fine Ok's last drop: the drums
    on the bar line, the bass one beat after, which is where it is heard). So, on the separated bass:
    a drop whose bass is silent going in (under 0.15 of its loud level over the bar before) moves to
    the first beat of its first bar where the bass plays (0.3 and up for two beats running). A drop
    whose bass is already playing, or never clearly comes in, stays where it is. Over 24 songs this
    moved 10 of 64 drops, each by 1 to 3 beats. Returns (sections, drops, moves)."""
    F = stem_beats(stems, beats)
    sections = [dict(x) for x in sections]
    drops = list(drops)
    moves = []
    if F is None:
        return sections, drops, moves
    beats = np.asarray(beats, dtype=float)
    bass = F[:, 1]
    for i, sec in enumerate(sections):
        if sec["type"] != "drop" or i == 0:
            continue
        b = int(np.argmin(np.abs(beats - sec["start"])))
        if b < 4 or b + 5 >= len(bass) or bass[b - 4:b].mean() >= 0.15:
            continue
        for c in range(b, b + 4):
            if bass[c] >= 0.3 and bass[c + 1] >= 0.3:
                if c != b and beats[c] < sec["end"] - 1.0:
                    t_old, t_new = float(sec["start"]), round(float(beats[c]), 4)
                    sec["start"] = sections[i - 1]["end"] = t_new
                    drops = [t_new if abs(dp - t_old) < 0.6 else dp for dp in drops]
                    moves.append((round(t_old, 2), round(t_new, 2), c - b))
                break
    return sections, sorted(set(drops)), moves


def buildups_from_rolls(sections: list[dict], rolls: list, beats, phase: int) -> tuple[list[dict], int]:
    """A drum roll that runs straight into a drop is that drop's buildup. Where the sections have no
    buildup there, the bars the roll covers (from the bar line it starts in, two bars at least, sixteen
    at most) become one. Returns the sections and how many buildups were added."""
    beats = np.asarray(beats, dtype=float)
    if not rolls or len(beats) < 8 or len(sections) < 2:
        return sections, 0
    sections = [dict(x) for x in sections]                                 # the detector's own list stays as it was
    period = float(np.median(np.diff(beats)))
    bars = beats[int(phase)::4]
    added = 0
    for r0, r1 in rolls:
        idx = next((i for i, x in enumerate(sections) if x["type"] == "drop" and abs(x["start"] - r1) <= 1.5 * period), None)
        if not idx:
            continue
        prev, drop = sections[idx - 1], sections[idx]
        if prev["type"] == "buildup":
            continue
        start = float(bars[bars <= r0 + 0.05].max()) if (bars <= r0 + 0.05).any() else prev["start"]
        start = min(start, drop["start"] - 8 * period)                      # two bars at least
        start = max(start, drop["start"] - 64 * period)                     # sixteen at most
        if start <= prev["start"] + 4 * period:
            prev["type"] = "buildup"                                         # the roll is the whole section
        else:
            sections.insert(idx, {**prev, "start": round(start, 3), "end": drop["start"], "type": "buildup"})
            prev["end"] = round(start, 3)
        added += 1
    return sections, added


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


MAX_BPM = 135.0
ANALYSIS_VERSION = 32         # bumped when saved analyses (the cache, learned jam songs) go stale


CLIMB_MIN = 0.18            # how much a run-in must brighten toward its drop to count as a buildup
BUILDUP_CHECKS: list = []   # every drop's run-in, as measured (for tuning and tests)


def cap_tempo(y, sr, bpm, beats):
    """Count anything faster than 135 bpm in half time.

    The house rule for these shows: a beat is the pulse you would nod to, and past
    135 that is every other beat. The half of the grid kept is the one the kick
    lands on (the one with more low end).
    """
    import librosa

    beats = np.asarray(beats, dtype=float)
    while bpm > MAX_BPM and len(beats) >= 2:
        low = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=HOP, n_mels=8, fmin=30, fmax=200).mean(axis=0)
        at_beat = low[np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, len(low) - 1)]
        keep = 0 if float(at_beat[0::2].mean()) >= float(at_beat[1::2].mean()) else 1
        bpm, beats = bpm / 2, beats[keep::2]
    return bpm, beats


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


def find_structure(y, sr, beats, rms, duration, phases=None):
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
    # the beat model's downbeats say where the bar lines may be (one place, or two in half time)
    allowed = [p for p in (phases or range(4)) if 0 <= p < 4] or list(range(4))
    phase = int(max(allowed, key=lambda p: score[p]))

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
        # Only a real climb is a buildup. Not every drop has one: some songs just arrive at the
        # chorus. What marks a buildup is the sound opening up toward the drop (filters opening,
        # risers, cymbals): its brightness climbs over the last 8 bars. Measured on hand-labelled
        # songs, real buildups brightened by +0.23 to +0.44 and forced ones by +0.14 at most (usually
        # they darkened). Without that climb the run-in keeps its own label.
        win = act[d - L:d]
        k = max(1, L // 4)
        rise = float(win[-k:].mean() - win[:k].mean())
        slope = float(np.polyfit(np.arange(L), win, 1)[0] * L) if L >= 2 else 0.0
        w8 = min(8, d - run0); q = max(1, w8 // 4)
        feats = {nm: round(float(B[nm][d - q:d].mean() - B[nm][d - w8:d - w8 + q].mean()), 3) for nm in B}
        feats["hits_slope"] = round(float(np.polyfit(np.arange(w8), B["hits"][d - w8:d], 1)[0] * w8), 3) if w8 >= 2 else 0.0
        feats["sub_last"] = round(float(B["sub"][d - q:d].mean()), 3)
        BUILDUP_CHECKS.append({"drop_bar": int(d), "bars": int(L), "rise": round(rise, 3), "slope": round(slope, 3), **feats})
        if feats["cent"] < CLIMB_MIN:
            continue
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
    key = f"{os.path.basename(path)}.{st.st_size}.{int(st.st_mtime)}.v{ANALYSIS_VERSION}.json"
    cache = os.path.join(cache_dir, key)
    data = None
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        # an analysis saved while SongFormer could not run is not kept for ever: try again
        from . import structure_model
        if data.get("structure_source") != "loudness+songformer" and structure_model.available():
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
    from .genre import restyle                             # (decided by the newest rule, on what was heard)
    data["genre"] = restyle(data.get("genre"), float((data.get("mood") or {}).get("drive", 0.6)))
    settle_sections(data)                                  # (a saved analysis too: drops where the drums and the hook are)
    data["version"] = ANALYSIS_VERSION                     # a show built on it records which analysis it was
    return data
