"""Beat This (CPJKU) in the analysis: the beats and the bar lines (see beats_infer.py for the worker).

librosa's tracker finds one steady tempo from where notes start, and the bar lines were then guessed
from the kick, the snare and where sections change. It got the tempo wrong on some songs (The Strike's
The Getaway at 90 bpm, a bar line every 5 real beats) and the bar lines a beat off on others (Baggy
Jeans). Beat This is a model trained to find beats and downbeats directly. On the user's hand-checked
song (If Youre Too Shy) its downbeats agree with the bar lines; on Great Good Fine Ok it puts the bar
line a beat after the drums' hit, where the bass lands, as heard.

The grid is one steady tempo fitted through the model's beats (these songs are made to a click):
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
    idx = np.concatenate([[0], np.cumsum(np.maximum(0, np.round(np.diff(bt) / per)))])
    keep = np.ones(len(bt), dtype=bool)
    for _ in range(3):                                   # a straight line, the stray beats left out
        slope, icpt = np.polyfit(idx[keep], bt[keep], 1)
        keep = np.abs(bt - (icpt + slope * idx)) < 0.2 * slope
        if keep.sum() < 16:
            break
    slope, icpt = float(slope), float(icpt)
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
    votes = np.zeros(4)
    for d in db:
        i = near(d, g)
        if abs(g[i] - d) < 0.25 * per:
            votes[i % 4] += 1
    if votes.sum() < 4:
        return None
    phases = [p for p in range(4) if votes[p] >= 0.6 * votes.max()]
    bpm = round(60.0 * (len(g) - 1) / float(g[-1] - g[0]), 3)            # over the whole song: the beats are on a 20 ms grid
    return bpm, g, phases


def model_grid(y, sr: int, duration: float):
    """Ask the model about audio in memory (a jam's audio never touches the disk); None if it cannot."""
    r = WORKER.ask_audio(np.asarray(y, dtype=np.float32), sr)
    if not r:
        return None
    return grid(r.get("beats") or [], r.get("downbeats") or [], duration)
