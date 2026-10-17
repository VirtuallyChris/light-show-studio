"""Beat This (CPJKU) in the analysis: the beats and the bar lines (see beats_infer.py for the worker).

librosa's tracker finds one steady tempo from where notes start, and the bar lines were then guessed
from the kick, the snare and where sections change. It got the tempo wrong on some songs (The Strike's
The Getaway at 90 bpm, a bar line every 5 real beats) and the bar lines a beat off on others (Baggy
Jeans). Beat This is a model trained to find beats and downbeats directly. On the user's hand-checked
song (If Youre Too Shy) its downbeats agree with the bar lines; on Great Good Fine Ok it puts the bar
line a beat after the drums' hit, where the bass lands, as heard.

In the analysis the model sets the tempo (librosa's tracker got it wrong where it went wrong worst),
librosa's fit then lines the beats up with the song's onsets at that tempo (within 4% of it), and the
model's downbeats place the bar lines on that grid. Should the fit leave the model's tempo (more than
1.5% off), the model's own grid below is used.

That grid is one steady tempo fitted through the model's beats (these songs are made to a click):
each beat numbered from the gap to the one before (so a beat the model skips or doubles in a break
cannot slide the count, which a beat-by-beat grid did: If Youre Too Shy's bar lines half a bar off by
4:53), then a straight line through them. Past 135 bpm every other beat is kept (the house rule: a
beat is the pulse you nod to), the half the downbeats land on. All songs are in four, so the
bar line is the one of the four places most of the model's downbeats land on (two places when the grid
is in half time: then the section changes decide between them, in analysis.find_structure).
"""

from __future__ import annotations

import numpy as np

from .model_worker import ModelWorker

WORKER = ModelWorker("lightshow.studio.beats_infer", "beats")
MAX_BPM = 135.0


def available() -> bool:
    return WORKER.available()


def even_out(bt, duration: float) -> np.ndarray:
    """The model's beats with gaps filled evenly, doubles dropped, and run out to both ends."""
    bt = np.sort(np.asarray(bt, dtype=float))
    per = float(np.median(np.diff(bt)))
    out = [float(bt[0])]
    for t in bt[1:]:
        gap = float(t) - out[-1]
        n = int(round(gap / per))
        if n < 1:
            continue                                     # a double (closer than half a beat): dropped
        last = out[-1]
        out.extend(last + gap * k / n for k in range(1, n))
        out.append(float(t))
    while out[0] - per >= 0:
        out.insert(0, out[0] - per)
    while out[-1] + per < duration:
        out.append(out[-1] + per)
    return np.array(out)


def steady(bt, duration: float):
    """One steady grid through the model's beats: (period, beats from 0 to the end)."""
    bt = np.sort(np.asarray(bt, dtype=float))
    per = float(np.median(np.diff(bt)))
    # a stretch the model counts in double time (little image's SHOTS I'M NOT CALLING from 2:00) is
    # read at the song's pulse: a beat closer than three quarters of one to the last kept is dropped
    # (counted as whole beats, those half beats stretched the grid to 92.8 bpm in an 83.3 bpm song)
    kept = [float(bt[0])]
    for t in bt[1:]:
        if t - kept[-1] >= 0.75 * per:
            kept.append(float(t))
    bt = np.array(kept)
    # The tempo from the longest steady run (each beat a beat after the last), then every beat numbered
    # from that line, not from the gap before it: where the model slips half a beat (onto the "and",
    # where it starts counting double time) the slip cannot carry into the rest of the song, and those
    # beats, half a beat off the line, are left out of the fit.
    d = np.diff(bt)
    steady_gap = np.abs(d - per) < 0.15 * per
    best, run0, i = (0, 0), None, 0
    for i, ok in enumerate(np.append(steady_gap, False)):
        if ok and run0 is None:
            run0 = i
        elif not ok and run0 is not None:
            if i - run0 > best[1] - best[0]:
                best = (run0, i)
            run0 = None
    r0, r1 = best
    if r1 - r0 >= 8:
        slope, icpt = np.polyfit(np.arange(r1 - r0 + 1), bt[r0:r1 + 1], 1)
        icpt = icpt - slope * 0                          # numbered from the run's first beat
        base = bt[r0]
    else:
        slope, base = per, bt[0]
    for _ in range(4):                                   # a straight line, the stray beats left out
        idx = np.round((bt - base) / slope)
        keep = np.abs(bt - (base + slope * idx)) < 0.15 * slope
        if keep.sum() < 16:
            break
        slope, base = np.polyfit(idx[keep], bt[keep], 1)
    slope, icpt = float(slope), float(base)
    first = icpt - slope * np.floor(icpt / slope)
    return slope, np.arange(first, duration, slope)


def grid(beats, downbeats, duration: float):
    """(bpm, beats, phases): the grid from the model's answer, and the bar-line places it allows (beat
    index mod 4). None when the model's beats are too uneven to trust (then librosa's grid stands)."""
    bt, db = np.asarray(beats, dtype=float), np.asarray(downbeats, dtype=float)
    if len(bt) < 32:
        return None
    ibi = np.diff(bt)
    per = float(np.median(ibi))
    if per <= 0 or float(np.median(np.abs(ibi - per))) > 0.1 * per:
        return None                                      # no steady pulse to speak of
    per, g = steady(bt, duration)
    near = lambda d, arr: int(np.argmin(np.abs(arr - d)))
    if 60.0 / per > MAX_BPM:                             # half time: keep the half the downbeats are on
        par = np.zeros(2)
        for d in db:
            i = near(d, g)
            if abs(g[i] - d) < 0.25 * per:
                par[i % 2] += 1
        g = g[int(np.argmax(par))::2]
        per *= 2
    phases = bar_places(g, db)
    if phases is None:
        return None
    bpm = round(60.0 * (len(g) - 1) / float(g[-1] - g[0]), 3)            # over the whole song: the beats are on a 20 ms grid
    return bpm, g, phases


def bar_places(g, downbeats):
    """Where on a grid the bar lines may be (beat index mod 4): the places most of the model's downbeats
    land on (two when the grid is in half time). None with too few downbeats to tell."""
    g, db = np.asarray(g, dtype=float), np.asarray(downbeats, dtype=float)
    if len(g) < 8:
        return None
    per = float(np.median(np.diff(g)))
    votes = np.zeros(4)
    for d in db:
        i = int(np.argmin(np.abs(g - d)))
        if abs(g[i] - d) < 0.25 * per:
            votes[i % 4] += 1
    if votes.sum() < 4:
        return None
    return [p for p in range(4) if votes[p] >= 0.6 * votes.max()]


def fits(beats, onset, fps: float) -> float:
    """How well a grid sits on the music: the share of its 30 s stretches whose beats land on the onsets
    (moving the stretch's beats would not line them up better by more than 8% of a beat, or 50 ms)."""
    beats = np.asarray(beats, dtype=float)
    if len(beats) < 8:
        return 0.0
    per = float(np.median(np.diff(beats)))
    shifts = np.arange(-per / 2, per / 2, 0.01)
    good = n = 0
    for t0 in np.arange(0, beats[-1], 30.0):
        w = beats[(beats >= t0) & (beats < t0 + 30)]
        if len(w) < 8:
            continue
        at = lambda s: float(onset[np.clip(((w + s) * fps).astype(int), 0, len(onset) - 1)].mean())
        best = max(shifts, key=at)
        n += 1
        good += abs(best) <= max(0.05, 0.08 * per)
    return good / n if n else 0.0


def model_grid(y, sr: int, duration: float):
    """Ask the model about audio in memory (a jam's audio never touches the disk); None if it cannot."""
    r = WORKER.ask_audio(np.asarray(y, dtype=np.float32), sr)
    if not r:
        return None
    got = grid(r.get("beats") or [], r.get("downbeats") or [], duration)
    return (*got, list(r.get("downbeats") or []), list(r.get("beats") or [])) if got else None
