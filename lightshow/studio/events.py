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


# Bands tried for each drum, per song. Kicks sit anywhere from a deep sub thump to a punchy 150 Hz;
# snares from a fat 200 Hz body to a dry 3 kHz crack (a dubstep snare can live around 600 Hz).
KICK_BANDS = [(30, 70), (40, 100), (50, 130), (60, 160), (80, 200)]
SNARE_BANDS = [(150, 300), (200, 450), (300, 700), (400, 1000), (700, 1600), (1500, 3500), (2500, 6000)]
# How a song's drum pattern repeats: (beats per cycle, slots per beat). Four beats in sixteenths is
# most pop and dance; in triplets, a shuffle or a 6/8 feel; three beats, a waltz.
LAYOUTS = [(4, 4), (4, 3), (3, 4), (3, 3)]
NO_DRUMS_BELOW = 3.0       # kick pattern score under which a song is taken to have no drums at all
FP_FRAMES = 6              # a hit's fingerprint: its first ~140 ms
FP_MIN_SEPARATION = 0.3    # how much better the kick places must match the fingerprint than the rest


def _rise(S_p, freqs, lo, hi):
    """How sharply a band's energy jumps each frame, scaled so a strong hit in this song is about 1."""
    m = (freqs >= lo) & (freqs <= hi)
    env = S_p[m].mean(axis=0) if m.any() else np.zeros(S_p.shape[1])
    r = np.maximum(0.0, np.diff(env, prepend=env[:1]))
    return env, r / (float(np.percentile(r, 99.5)) + 1e-9)


class Grid:
    """The song's beats laid out as repeating cycles: `per` beats a cycle, `subs` slots a beat,
    the first cycle starting at beat `start`."""

    def __init__(self, beats, per=4, subs=4, start=0):
        self.beats, self.per, self.subs, self.start = np.asarray(beats, dtype=float), per, subs, int(start)
        self.n = per * subs

    def cycles(self):
        return range(self.start, len(self.beats) - self.per, self.per)

    def slot(self, t):
        """Which slot of its cycle a moment falls on (None outside the grid)."""
        i = int(np.searchsorted(self.beats, t)) - 1
        if i < self.start:
            return None
        c = (i - self.start) // self.per * self.per + self.start
        if c + self.per >= len(self.beats):
            return None
        b0, b1 = self.beats[c], self.beats[c + self.per]
        return int(round((t - b0) / (b1 - b0) * self.n)) % self.n

    def profile(self, rise, frame_t):
        """A band's average jump at each slot of the cycle, over the cycles that have drums."""
        rows = []
        for c in self.cycles():
            b0, b1 = self.beats[c], self.beats[c + self.per]
            js = np.searchsorted(frame_t, b0 + (b1 - b0) * np.arange(self.n) / self.n)
            rows.append([rise[max(0, j - 1):j + 3].max() if j < len(rise) else 0.0 for j in js])
        if not rows:
            return None
        rows = np.array(rows)
        busy = rows.sum(axis=1)
        keep = busy >= np.percentile(busy, 30)      # the quiet cycles (intros, breakdowns) would blur the pattern
        return rows[keep].mean(axis=0) if keep.any() else rows.mean(axis=0)

    def to_json(self):
        return {"beats_per_cycle": self.per, "slots_per_beat": self.subs, "start": self.start}


def _pattern(prof):
    """The busiest quarter of the cycle against the quietest quarter. A drum pattern, sparse or busy,
    hits some places hard and leaves others empty; music without drums (a strummed guitar, a piano,
    a pad) jumps a little everywhere. Over 20 songs: no drums 2.4, drums 3.7 and up."""
    q = np.sort(prof)
    k = max(2, len(q) // 4)
    return float(q[-k:].mean() / (q[:k].mean() + 0.02))


def _contrast(prof, where):
    top = np.sort(prof[where])[-2:].mean()
    return float(top / (np.median(prof[where]) + 0.02))


def choose_layout(S_p, freqs, sr, hop, beats, kick_rises):
    """How the song's beat divides and repeats: (beats per cycle, slots per beat, how it was decided).

    Two separate questions, each answered with a measure that is fair to both answers.
    Sixteenths or triplets: how tightly all the song's percussive hits (hats included: they show the
    subdivision best) gather on a repeating place when the beat is divided in four, and in three.
    Four beats or three: whether the kick's strength from beat to beat repeats every 4 beats or every 3.
    """
    import librosa
    from scipy.signal import find_peaks
    beats = np.asarray(beats, dtype=float)
    _, r = _rise(S_p, freqs, 150, 10000)
    _, rk = _rise(S_p, freqs, 30, 200)
    flux = r + rk
    idx, props = find_peaks(flux, height=0.1, distance=max(1, int(0.05 * sr / hop)))
    t = librosa.frames_to_time(idx, sr=sr, hop_length=hop)
    w = props["peak_heights"]
    i = np.searchsorted(beats, t) - 1
    ok = (i >= 0) & (i < len(beats) - 1)
    t, w, i = t[ok], w[ok], i[ok]
    pos = (t - beats[i]) / (beats[i + 1] - beats[i])          # where in its beat, 0..1
    # How tightly the hits gather on a repeating place within the beat, dividing it in n: 1 if every hit
    # lands on the same place of the n, 0 if they are spread evenly. A constant offset between the
    # grid's beat times and the drums' onsets does not matter. Straight sixteenths gather at n=4 and
    # cancel at n=3; triplets the other way round; hits only on the beat gather at both (then: 4).
    share = {}
    for n in (4, 3):
        phase_n = np.exp(2j * np.pi * pos * n)
        share[n] = float(np.abs((w * phase_n).sum()) / (w.sum() + 1e-9))
    subs = 3 if share[3] > 0.3 and share[3] > share[4] * 1.3 else 4
    # the kick, beat by beat: does the pattern come round every 4 beats or every 3?
    frame_t = librosa.frames_to_time(np.arange(S_p.shape[1]), sr=sr, hop_length=hop)
    k = max(kick_rises.values(), key=lambda x: float(np.percentile(x, 99)))
    js = np.searchsorted(frame_t, beats)
    per_beat = np.array([k[max(0, j - 2):j + 3].max() if j < len(k) else 0.0 for j in js])
    x = per_beat - per_beat.mean()

    def ac(lag):
        return float(np.dot(x[:-lag], x[lag:]) / (np.dot(x, x) + 1e-9)) if len(x) > lag + 8 else 0.0
    a3, a4 = ac(3), ac(4)
    per = 3 if a3 > 0.1 and a3 > 1.2 * max(a4, 0.0) else 4
    return per, subs, {"sixteenth_share": round(share[4], 2), "triplet_share": round(share[3], 2), "repeat3": round(a3, 2), "repeat4": round(a4, 2)}


def calibrate_drums(S_p, freqs, sr, hop, beats, phase):
    """Where this song's kick and snare live, learned from where they land in the song's own cycle.

    Every song mixes its drums differently, and not every song is in four: so for each way the beat
    could repeat (four beats in sixteenths, four in triplets, three in sixteenths or triplets, each
    three-beat layout tried from every beat) and each low band, the band's average jump is laid out
    over the cycle. The kick is the layout and band whose pattern stands out most: some places hit
    hard, others empty. If none does, the song has no drums. The snare is the band (above the kick)
    that stands out in the places the kick does not land and stays quiet where it does.
    """
    import librosa
    if beats is None or len(beats) < 16:
        return None
    frame_t = librosa.frames_to_time(np.arange(S_p.shape[1]), sr=sr, hop_length=hop)
    kick_rises = {band: _rise(S_p, freqs, *band)[1] for band in KICK_BANDS}
    per, subs, how = choose_layout(S_p, freqs, sr, hop, beats, kick_rises)
    best_k = None
    for start in ([phase] if per == 4 else range(per)):
        grid = Grid(beats, per, subs, start)
        for band, r in kick_rises.items():
            prof = grid.profile(r, frame_t)
            if prof is None:
                continue
            sc = _pattern(prof)
            if best_k is None or sc > best_k[0]:
                best_k = (sc, band, prof, grid)
    if best_k is None:
        return None
    sc, kband, kprof, grid = best_k
    if sc < NO_DRUMS_BELOW:
        return {"has_drums": False, "kick_score": round(sc, 2), "grid": {**grid.to_json(), **how}}   # nothing in the low end keeps time
    allpos = np.arange(grid.n)
    kick_pos = np.where(kprof >= 0.5 * kprof.max())[0]
    free = np.array([q for q in allpos if q not in set(kick_pos)])
    cal = {"has_drums": True, "grid": {**grid.to_json(), **how}, "kick_band": list(kband), "kick_score": round(sc, 2), "kick_pos": kick_pos.tolist()}
    # The snare's places: where its band jumps hard, and either the kick does not land, or the band
    # jumps far more for the kick band's jump than at the kick's other places. That second case is
    # dance music: a kick on every beat and a clap on 2 and 4, on top of it; looking only where the
    # kick is not, there is nowhere left to look. The band must stand out at its places (contrast)
    # and be quieter, for the kick band's jump, where only the kick lands (selectivity): a kick is
    # broad, and a band it fills is no use for telling the two apart.
    best_s = None
    kset = set(kick_pos.tolist())
    for band in SNARE_BANDS:
        prof = grid.profile(_rise(S_p, freqs, *band)[1], frame_t)
        r = prof / (kprof + 0.05)
        r_kick = float(np.median(r[kick_pos]))
        spos = [q for q in allpos if prof[q] >= 0.5 * prof.max() and (q not in kset or r[q] >= 1.6 * r_kick)]
        if not spos or len(spos) > grid.n // 2:
            continue
        spos = np.array(spos)
        others = np.array([q for q in allpos if q not in set(spos.tolist())])
        c = float(prof[spos].mean() / (np.median(prof[others]) + 0.02))
        kick_only = np.array([q for q in kick_pos if q not in set(spos.tolist())])
        sel = float(r[spos].mean() / (r[kick_only].mean() + 1e-3)) if len(kick_only) else float(prof[spos].mean() / (prof[others].mean() + 0.05))
        if c < 2.0 or sel < 1.4:                      # a weak pattern is worse than none: false snares flash on synths
            continue
        if best_s is None or c * sel > best_s[0]:
            best_s = (c * sel, band, prof, c, sel, spos)
    if best_s is not None:
        prof, snare_pos = best_s[2], best_s[5]
        # a backbeat comes in twos, half a cycle apart (2 and 4): a quieter twin is still the pattern
        if grid.per % 2 == 0:
            half = grid.n // 2
            twins = [(q + half) % grid.n for q in snare_pos if prof[(q + half) % grid.n] >= 0.25 * prof.max()]
            snare_pos = np.array(sorted(set(snare_pos.tolist()) | set(twins)), dtype=int)
        ratio = lambda where: float(prof[where].mean()) / (float(kprof[where].mean()) + 0.05)
        sset = set(snare_pos.tolist())
        others = np.array([q for q in allpos if q not in sset and q not in kset])
        kick_only = np.array([q for q in kick_pos if q not in sset])
        cal.update({"snare_band": list(best_s[1]), "snare_score": round(best_s[3], 2), "snare_selectivity": round(best_s[4], 2),
                    "snare_pos": snare_pos.tolist(),
                    # how hard this band jumps where a snare is (normalized units)
                    "snare_level": round(float(prof[snare_pos].mean()), 3),
                    # and in the places neither drum lands: the band's own busyness (synths, vocals)
                    "snare_noise": round(float(np.median(prof[others])) if len(others) else 0.0, 3),
                    # this band's jump over the kick band's, where a snare lands and where only a kick does
                    "ratio_snare": round(ratio(snare_pos), 3),
                    "ratio_kick": round(ratio(kick_only) if len(kick_only) else 0.5 * ratio(snare_pos), 3)})
    return cal


def _grid_of(cal, beats, phase):
    g = (cal or {}).get("grid") or {}
    return Grid(beats, g.get("beats_per_cycle", 4), g.get("slots_per_beat", 4), g.get("start", phase))


def _pattern_share(times, grid, places):
    """The share of these hits that land on the given slots of the cycle."""
    places, n, on = set(places), 0, 0
    for t in times:
        q = grid.slot(t)
        if q is None:
            continue
        n += 1
        on += q in places
    return on / n if n else 0.0


def _fingerprint_kicks(S_p, freqs, sr, hop, duration, grid, cal, t_k, s_k):
    """Find the kicks again by how this song's kick sounds, where that tells them apart.

    The confident kicks (strong, and on the song's kick places) are averaged into a fingerprint: the
    shape of the first ~140 ms of the hit, in fine detail from 20 to 400 Hz, so the thump and its
    falling pitch, not just its loudness in a band. Every jump in the low end is then compared with
    it. Songs with a produced, consistent kick tell kicks from bass notes this way; songs with live,
    varied kicks do not (each hit sounds a little different). So each song measures how well its
    fingerprint separates its kick places from the rest, and keeps the kicks it has unless the
    separation is clear. What it did is recorded in `cal["kick_fingerprint"]`.
    """
    import librosa
    from scipy.signal import find_peaks
    places = set(cal.get("kick_pos") or [])
    seeds = [t for t, st in zip(t_k, s_k) if st >= 0.5 and grid.slot(t) in places][:200]
    if len(seeds) < 12:
        cal["kick_fingerprint"] = {"used": False, "why": "too few confident kicks"}
        return t_k, s_k
    m = (freqs >= 20) & (freqs <= 400)
    Sm = S_p[m]
    L = np.log1p(Sm / (np.percentile(Sm, 99) + 1e-9) * 100)
    total = Sm.sum(axis=0)
    rise = np.maximum(0.0, np.diff(total, prepend=total[:1]))
    top = float(np.percentile(rise, 99.5)) + 1e-9
    cand, _ = find_peaks(rise, distance=max(1, int(0.07 * sr / hop)), height=0.04 * top)

    def prints(idx):
        out = np.zeros((len(idx), L.shape[0] * FP_FRAMES))
        for r, i in enumerate(idx):
            if 2 <= i and i + FP_FRAMES <= L.shape[1]:
                v = np.maximum(0.0, L[:, i:i + FP_FRAMES] - L[:, i - 2:i].mean(axis=1, keepdims=True)).ravel()
                out[r] = v / (np.linalg.norm(v) + 1e-9)
        return out
    tmpl = prints(librosa.time_to_frames(np.array(seeds), sr=sr, hop_length=hop)).mean(axis=0)
    tmpl /= np.linalg.norm(tmpl) + 1e-9
    sim = prints(cand) @ tmpl
    ct = librosa.frames_to_time(cand, sr=sr, hop_length=hop)
    on = np.array([grid.slot(t) in places for t in ct])
    if on.sum() < 8 or (~on).sum() < 8:
        cal["kick_fingerprint"] = {"used": False, "why": "not enough to compare"}
        return t_k, s_k
    med_on, med_off = float(np.median(sim[on])), float(np.median(sim[~on]))
    info = {"separation": round(med_on - med_off, 2), "on": round(med_on, 2), "off": round(med_off, 2)}
    if med_on - med_off < FP_MIN_SEPARATION:
        cal["kick_fingerprint"] = {**info, "used": False, "why": "kicks vary too much to fingerprint"}
        return t_k, s_k
    thr = float(np.clip((med_on + med_off) / 2, 0.55, 0.8))
    keep = (sim >= thr) & (rise[cand] >= 0.1 * top) & (ct > 0.02) & (ct < duration)
    if keep.sum() < 8:
        cal["kick_fingerprint"] = {**info, "used": False, "why": "too few matches"}
        return t_k, s_k
    st = np.clip(rise[cand][keep] / (np.percentile(rise[cand][keep], 95) + 1e-9), 0, 1)
    cal["kick_fingerprint"] = {**info, "used": True, "threshold": round(thr, 2)}
    return ct[keep], st


def find_drums(S_p, freqs, sr, hop, duration, beats=None, phase=0):
    """The kick and snare hits themselves, from the percussive part of the mix.

    The bands come from calibrate_drums when the song has a clear pattern; a song whose low end keeps
    no time at all has no drums, and gets no hits. Otherwise the kick is a thump below 150 Hz and the
    snare a 1.5-5 kHz crack with some 150-400 Hz body (which a hi-hat lacks). Every hit is found, not
    only those on the pattern: rolls and fills count. Returns two lists of (time, strength 0..1), and
    the calibration (or None).
    """
    import librosa
    from scipy.signal import find_peaks

    cal = calibrate_drums(S_p, freqs, sr, hop, beats, phase)
    if cal and not cal.get("has_drums", True):
        return [], [], cal                               # a song without drums: nothing to flash on
    grid = _grid_of(cal, beats, phase) if cal else None

    def band(lo, hi):
        m = (freqs >= lo) & (freqs <= hi)
        return S_p[m].mean(axis=0) if m.any() else np.zeros(S_p.shape[1])

    def hits(env, idx):
        times = librosa.frames_to_time(idx, sr=sr, hop_length=hop)
        keep = (times > 0.02) & (times < duration)
        idx, times = idx[keep], times[keep]
        if not len(idx):
            return idx, times, np.array([])
        st = env[idx]
        return idx, times, np.clip(st / (np.percentile(st, 95) + 1e-9), 0, 1)

    # The kick, measured on a linear scale: in decibels the faint low end of a snare or a
    # hat looks as big as a kick. A kick is a jump in low-band energy that reaches a real
    # share of the song's typical kick.
    k_lo, k_hi = cal["kick_band"] if cal else (30, 150)
    low_all = band(k_lo, k_hi)
    rise = np.maximum(0.0, np.diff(low_all, prepend=low_all[:1]))
    idx_k = np.array([], dtype=int)
    if rise.size > 4 and rise.max() > 0:
        cand, _ = find_peaks(rise, distance=max(1, int(0.09 * sr / hop)), height=0.15 * float(np.percentile(rise, 99.5)))
        if len(cand):
            level = np.array([low_all[i:i + 3].max() for i in cand])
            idx_k = cand[level >= 0.3 * float(np.percentile(level, 90))]
    idx_k, t_k, s_k = hits(rise, idx_k)
    if cal:
        t_k, s_k = _fingerprint_kicks(S_p, freqs, sr, hop, duration, grid, cal, t_k, s_k)

    if cal and "snare_band" in cal:
        # The calibrated snare: every jump in its band that is as big as a real snare in this song.
        # Next to a kick the band jumps anyway (a kick is broad), so there it must clear the
        # kick's own jump by a good margin.
        s_lo, s_hi = cal["snare_band"]
        _, r = _rise(S_p, freqs, s_lo, s_hi)
        _, kr = _rise(S_p, freqs, *cal["kick_band"])
        lvl, noise = cal["snare_level"], cal.get("snare_noise", 0.0)
        floor = max(0.4 * lvl, noise + 0.6 * max(0.0, lvl - noise))   # a hit must stand well clear of the band's busyness
        split = float(np.sqrt(max(cal["ratio_snare"], 1e-3) * max(cal["ratio_kick"], 1e-3)))   # between a kick's ratio and a snare's
        cand, _ = find_peaks(r, distance=max(1, int(0.07 * sr / hop)), height=floor)
        t_c = librosa.frames_to_time(cand, sr=sr, hop_length=hop)
        hat_check = s_lo >= 1500                         # a crack band also hears the hats: ask for body
        body = band(150, 400)
        ref = float(np.percentile(body[cand], 90)) + 1e-9 if len(cand) else 1.0
        snare = []
        for i, t in zip(cand, t_c):
            if not (0.02 < t < duration):
                continue
            v = float(r[max(0, i - 1):i + 2].max())
            dk = t - t_k if len(t_k) else np.array([1.0])
            if np.any((dk > -0.03) & (dk < 0.06)) and v / (float(kr[max(0, i - 1):i + 2].max()) + 0.05) < split:
                continue                                 # the kick's own jump in this band
            if hat_check and float(body[max(0, i - 1):i + 3].max()) < 0.35 * ref:
                continue                                 # no body: a hat or a shaker
            snare.append((t, float(np.clip(v / (lvl * 1.6 + 1e-9), 0, 1))))
        # The check: a drummer repeats the pattern, so most real snares land on the places the
        # calibration found. If they are scattered all over the cycle, the band was hearing
        # something else (a synth, a vocal), and the fixed bands are safer.
        share = _pattern_share([t for t, _ in snare], grid, cal["snare_pos"])
        cal["snare_on_pattern"] = round(share, 2)
        if share >= 0.6:
            return list(zip(t_k, s_k)), snare, cal
        cal["snare_rejected"] = True

    env_c, idx_c, _ = _band_onsets(S_p, freqs, 1500, 5000, sr, hop, 0.10, 1.6)
    idx_c, t_c, s_c = hits(env_c, idx_c)
    body, low, crack = band(150, 400), band(30, 150), band(1500, 5000)
    snare = []
    if len(idx_c):
        ref = float(np.percentile(body[idx_c], 90)) + 1e-9
        for i, t, st in zip(idx_c, t_c, s_c):
            if st < 0.35:
                continue                                         # too faint to light: ghost notes, ringing
            j = slice(max(0, i - 1), i + 3)                      # the hit's first few frames
            b = float(body[j].max())
            if b < 0.35 * ref:
                continue                                         # no body: a hat or a shaker
            dk = t - t_k if len(t_k) else np.array([1.0])
            near_kick = bool(np.any((dk > -0.03) & (dk < 0.09)))  # a kick just before (or with) this crack
            if near_kick and (st < 0.5 or float(low[j].max()) > 1.5 * b):
                continue                                         # the kick's own click, not a snare with it
            snare.append((t, st))
    return list(zip(t_k, s_k)), snare, cal


DRUMS_PRESENT_BELOW = 0.1  # separated drums quieter than this share of the song: the song has no drums


def _hit_quality(hits, cal, beats, phase, kind):
    """How believable a set of hits is: how much of it lands on a few places of the song's cycle
    (a drummer repeats the pattern), at a sensible number per cycle. 0 for nothing."""
    if not hits:
        return 0.0
    grid = _grid_of(cal, beats, phase) if cal and cal.get("has_drums") else Grid(beats, 4, 4, phase)
    cycles = max(1, len(grid.cycles()))
    sp = np.zeros(grid.n)
    for t, _ in hits:
        q = grid.slot(t)
        if q is not None:
            sp[q] += 1
    if not sp.sum():
        return 0.0
    conc = float(np.sort(sp)[-4:].sum() / sp.sum())
    per = len(hits) / cycles
    lo, hi = (1.0, 8.0) if kind == "kick" else (0.75, 5.0)
    return conc * min(1.0, per / lo) * min(1.0, hi / per)


def drums_present(d, sr, hop, n_frames):
    """0..1 per frame: how much the separated drum track is playing here (a drumless intro is 0)."""
    w = max(1, int(0.5 * sr / hop))                    # half a second either side
    m = len(d) // hop
    rms = np.sqrt((d[: m * hop].reshape(m, hop) ** 2).mean(axis=1)) if m else np.zeros(1)
    rms = np.pad(rms, (0, max(0, n_frames - len(rms))))[:n_frames]
    k = np.ones(2 * w + 1) / (2 * w + 1)
    sm = np.convolve(rms, k, mode="same")
    ref = float(np.percentile(sm, 90)) + 1e-9
    return np.clip(sm / ref, 0, 1)


def find_rolls(S_d, freqs, sr, hop, duration, beats, kick_band=(30, 150)):
    """Drum rolls: stretches of the drum track where the hits above the kick come three or more to
    a beat for at least a bar (snare rolls, tom runs, the machine-gun build into a drop). Every hit
    in them, as close as 46 ms apart, so a buildup can follow the roll exactly. Returns the hits as
    (time, strength) and the stretches as [start, end]."""
    import librosa
    from scipy.signal import find_peaks
    beats = np.asarray(beats, dtype=float)
    if len(beats) < 8:
        return [], []
    m = (freqs >= max(150, kick_band[1])) & (freqs <= 2500)       # snares and toms: below most of a hi-hat's energy
    env = S_d[m].sum(axis=0)
    r = np.maximum(0.0, np.diff(env, prepend=env[:1]))
    r /= float(np.percentile(r, 99.5)) + 1e-9
    pk, props = find_peaks(r, height=0.12, distance=2)
    t = librosa.frames_to_time(pk, sr=sr, hop_length=hop)
    st = props["peak_heights"]
    per_beat = np.histogram(t, bins=beats)[0]
    # a roll is a change from the song's own groove: at least three hits a beat, and twice its usual
    # density (a groove of steady sixteenths is not a roll, however busy)
    usual = float(np.median(per_beat[per_beat > 0])) if (per_beat > 0).any() else 0.0
    busy = per_beat >= max(3.0, 2.0 * usual)
    spans, i = [], 0
    while i < len(busy):
        if busy[i]:
            j = i
            while j < len(busy) and busy[j]:
                j += 1
            if j - i >= 4:                                 # a bar or more
                spans.append([float(beats[i]), float(beats[min(j, len(beats) - 1)])])
            i = j
        else:
            i += 1
    hits = [(float(a), float(min(1.0, b))) for a, b in zip(t, st) if any(s0 <= a < s1 for s0, s1 in spans) and a < duration]
    return hits, [[round(a, 3), round(b, 3)] for a, b in spans]


def find_drums_both(S_mix, freqs, sr, hop, duration, beats, phase, mid, stems, model_hits=None, S_d=None):
    """Kicks and snares from the full mix and from the separated drum track, keeping for each drum
    whichever is more believable. Separation makes most drums far clearer, but not all: a clap can
    land in "other", and strums can leak into the drum track. The separated drums also tell whether
    the song has drums at all (quieter than 10% of the song: no drums), and when they are playing:
    a hit from the mix at a moment the drum track is silent is dropped.

    `model_hits` are the drum transcriber's (drum_model.hits: ADTOF on the drum track): where given,
    its kicks and snares are the ones used (on the attack, and found in quiet verses and broken beats
    alike); the pattern finders above still run, for the calibration the rest of the analysis reads
    (the kick's band, the cycle), and are the fallback where the model cannot run."""
    import librosa
    d = np.asarray(stems["drums"], dtype=np.float32)
    if stems.get("sr", sr) != sr:
        d = librosa.resample(d, orig_sr=stems["sr"], target_sr=sr)
    d = np.pad(d, (0, max(0, len(mid) - len(d))))[: len(mid)]
    level = float(np.sqrt(np.mean(d ** 2)) / (np.sqrt(np.mean(mid ** 2)) + 1e-9))
    if level < DRUMS_PRESENT_BELOW:
        return [], [], {"has_drums": False, "drum_level": round(level, 3), "source": "stems"}, np.zeros(S_mix.shape[1])
    present = drums_present(d, sr, hop, S_mix.shape[1])
    if S_d is None:
        S_d = np.abs(librosa.stft(d, hop_length=hop))
    # with the drum model's hits, only the drum track's calibration is read (the kick's band, the cycle):
    # the mix's pattern pass, a fallback for the hits themselves, is not run
    mk, ms, mcal = ([], [], None) if model_hits is not None else find_drums(S_mix, freqs, sr, hop, duration, beats, phase)
    sk, ss, scal = find_drums(S_d, freqs, sr, hop, duration, beats, phase)
    q = {("kick", "mix"): _hit_quality(mk, mcal, beats, phase, "kick"), ("kick", "stem"): _hit_quality(sk, scal, beats, phase, "kick"),
         ("snare", "mix"): _hit_quality(ms, mcal, beats, phase, "snare"), ("snare", "stem"): _hit_quality(ss, scal, beats, phase, "snare")}
    k_src = "stem" if q[("kick", "stem")] >= q[("kick", "mix")] else "mix"
    s_src = "stem" if q[("snare", "stem")] >= q[("snare", "mix")] else "mix"
    kicks, snares = (sk if k_src == "stem" else mk), (ss if s_src == "stem" else ms)
    playing = lambda t: present[min(len(present) - 1, int(t * sr / hop))] >= 0.15
    kicks = [h for h in kicks if playing(h[0])]
    snares = [h for h in snares if playing(h[0])]
    cal_src = k_src                                      # whose calibration the analysis keeps
    if model_hits is not None:                           # the transcriber's, where it ran
        kicks = [h for h in model_hits.get("kick", []) if 0.02 < h[0] < duration and playing(h[0])]
        snares = [h for h in model_hits.get("snare", []) if 0.02 < h[0] < duration and playing(h[0])]
        k_src = s_src = "adtof"
    elif not kicks and not snares:                       # drums, but no pattern to calibrate on: their attacks
        ak, asn = stem_attack_hits(S_d, freqs, sr, hop)
        kicks, snares = [h for h in ak if playing(h[0])], [h for h in asn if playing(h[0])]
        k_src = s_src = "stem attacks"
        cal_src = "mix"
    cal = {"has_drums": True, "drum_level": round(level, 3), "kick_source": k_src, "snare_source": s_src,
           "quality": {f"{a}_{b}": round(v, 2) for (a, b), v in q.items()},
           **((scal if cal_src == "stem" else mcal) or {}), "mix": mcal, "stem": scal}
    cal["has_drums"] = True
    roll_hits, rolls = find_rolls(S_d, freqs, sr, hop, duration, beats, tuple(cal.get("kick_band") or (30, 150)))
    roll_hits = [h for h in roll_hits if playing(h[0])]
    cal["rolls"], cal["roll_hits"] = rolls, roll_hits
    return kicks, snares, cal, present


def stem_attack_hits(S_d, freqs, sr, hop):
    """Kicks and snares straight from the separated drum track's attacks, for drums with no steady
    pattern to calibrate on (Porter Robinson's Unfold: broken beats, a drum track at 45% of the song,
    and not one hit found, so the lights pulsed on every beat instead). A kick is a jump in the thump
    below 150 Hz, a snare one in the 1.5-5 kHz crack; each a clear peak (above the song's typical
    attack, at least 70 ms after the last). Returns two lists of (time, strength 0..1)."""
    import librosa
    from scipy.signal import find_peaks

    fps = sr / hop

    def hits(lo, hi):
        band = (freqs >= lo) & (freqs < hi)
        if not band.any():
            return []
        e = np.log1p(S_d[band].sum(axis=0))
        flux = np.maximum(0.0, np.diff(e, prepend=e[0]))
        if not flux.any():
            return []
        ref = float(np.percentile(flux[flux > 0], 90))
        idx, _ = find_peaks(flux, height=0.5 * ref, distance=max(1, int(0.07 * fps)))
        st = np.clip(flux[idx] / (np.percentile(flux[idx], 95) + 1e-9), 0, 1) if len(idx) else []
        return [(float(i / fps), float(v)) for i, v in zip(idx, st)]

    return hits(30, 150), hits(1500, 5000)


def find_cuts(mid, sr, hop=512):
    """Audio cuts: the whole mix drops to almost nothing for a moment (0.2 to 4 s) and then comes
    back, like the gap before a drop or a stop-time break. Compared with the level around it, so a
    quiet song's quiet parts are not cuts. Returns [start, end] in seconds."""
    m = len(mid) // hop
    if m < 10:
        return []
    rms = np.sqrt((mid[: m * hop].reshape(m, hop) ** 2).mean(axis=1))
    fps = sr / hop
    k = max(1, int(0.05 * fps))
    rms = np.convolve(rms, np.ones(k) / k, mode="same")
    w = int(4 * fps)
    ref = np.array([np.median(rms[max(0, i - w):i + w]) for i in range(0, m, max(1, int(fps / 4)))])
    ref = np.repeat(ref, max(1, int(fps / 4)))[:m]
    low = rms < 0.12 * (ref + 1e-9)
    cuts, i = [], 0
    while i < m:
        if low[i]:
            j = i
            while j < m and low[j]:
                j += 1
            d = (j - i) / fps
            back = j < m and rms[j:j + int(0.3 * fps)].max(initial=0) > 0.5 * ref[min(j, m - 1)]
            if 0.2 <= d <= 4.0 and back and i > 0:
                cuts.append([round(i / fps, 3), round(j / fps, 3)])
            i = j
        else:
            i += 1
    return cuts


def find_bass_hits(b, sr, beats):
    """The bass line's hits, for lights that flash with it: on the separated bass track, every eighth
    note of the beat grid where the bass clearly attacks (a jump in its level within 60 ms of the
    slot, at least a quarter of the song's strong ones, while the bass is actually sounding). On the
    grid, as a person would mark them: in Someone Else's first drop this matched 20 of the 27 flashes
    placed by hand, with 6 more. Returns (time, strength 0..1)."""
    import librosa
    beats = np.asarray(beats, dtype=float)
    if len(beats) < 8 or b is None or not len(b):
        return []
    hop = 256
    S = np.abs(librosa.stft(np.asarray(b, dtype=np.float32), n_fft=2048, hop_length=hop))
    f = librosa.fft_frequencies(sr=sr, n_fft=2048)
    env = S[(f >= 30) & (f <= 400)].sum(axis=0)
    e = np.log1p(env / (np.percentile(env, 99) + 1e-9) * 100)
    flux = np.maximum(0.0, np.diff(e, prepend=e[:1]))
    ft = librosa.frames_to_time(np.arange(len(flux)), sr=sr, hop_length=hop)
    level = env / (np.percentile(env, 80) + 1e-9)
    slots = np.sort(np.concatenate([beats[:-1], (beats[:-1] + beats[1:]) / 2]))
    i0 = np.searchsorted(ft, slots - 0.06)
    i1 = np.searchsorted(ft, slots + 0.06)
    strength = np.array([flux[a:b_].max() if b_ > a else 0.0 for a, b_ in zip(i0, i1)])
    sounding = np.array([level[a:b_ + 4].max() if b_ + 4 > a else 0.0 for a, b_ in zip(i0, i1)]) >= 0.2
    if not (strength > 0).any():
        return []
    ref = float(np.percentile(strength[strength > 0], 90)) + 1e-9
    keep = (strength >= 0.25 * ref) & sounding
    return [(float(t), float(min(1.0, v / ref))) for t, v in zip(slots[keep], strength[keep])]


def find_entries(level, fps, beats, phase):
    """Where an instrument comes in: its level (0..1 per frame) sits near nothing for two bars and
    then plays for the next two, on a bar line. Returns the times."""
    beats = np.asarray(beats, dtype=float)
    bars = beats[int(phase)::4]
    if len(bars) < 6 or not len(level):
        return []
    lv = np.asarray(level, dtype=float)
    ref = float(np.percentile(lv, 80)) + 1e-9
    bar_lv = [float(lv[int(bars[i] * fps):int(bars[i + 1] * fps)].mean()) if int(bars[i + 1] * fps) > int(bars[i] * fps) else 0.0 for i in range(len(bars) - 1)]
    out = []
    for i in range(2, len(bar_lv) - 1):
        before = max(bar_lv[i - 2], bar_lv[i - 1])
        after = (bar_lv[i] + bar_lv[i + 1]) / 2
        if before < 0.25 * ref and after > 0.6 * ref and (not out or bars[i] - out[-1] > 8 * (bars[1] - bars[0])):
            out.append(round(float(bars[i]), 3))
    return out


def detect_events(path, beats, duration, phase=0, sr_target=22050, hop=512, feature_fps=30.0, audio=None, stems=None,
                  drum_hits=None) -> dict:
    """`audio` is (array, sr) already in memory, mono or (2, n); otherwise `path` is loaded. `drum_hits`
    are the drum transcriber's hits in the stems' drum track (drum_model.hits), if it ran."""
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
    freqs = librosa.fft_frequencies(sr=sr)
    # The percussive part: the separated drum track itself where the drum model has heard it (cleaner than
    # pulling the percussion out of the mix, and the mix's separation, ~7 s a song, is not needed at all);
    # else the mix's, as before (the pattern finders then read both, see find_drums_both).
    S_d = None
    if stems and stems.get("drums") is not None and drum_hits is not None:
        d = np.asarray(stems["drums"], dtype=np.float32)
        if stems.get("sr", sr) != sr:
            d = librosa.resample(d, orig_sr=stems["sr"], target_sr=sr)
        d = np.pad(d, (0, max(0, len(mid) - len(d))))[: len(mid)]
        S_d = S_p = np.abs(librosa.stft(d, hop_length=hop))
        S_h = None                                       # (the bass comes from its own track)
    else:
        mono_h, mono_p = librosa.effects.hpss(mid)
        S_h, S_p = np.abs(librosa.stft(mono_h, hop_length=hop)), np.abs(librosa.stft(mono_p, hop_length=hop))

    # wide instruments (synth lead, horns, stabs) - vocals and kick are centre and mostly gone
    # Deliberately sparse: a light that marks every note is a strobe, one that marks
    # the notable notes reads as following the instrument.
    # The lead: the separated "other" track (synths, guitars, keys: no drums, no vocals) when there is
    # one, which is where a lead synth or a guitar line actually is; else the stereo side channel.
    S_lead = S_side
    if stems and stems.get("other") is not None:
        o = np.asarray(stems["other"], dtype=np.float32)
        if stems.get("sr", sr) != sr:
            o = librosa.resample(o, orig_sr=stems["sr"], target_sr=sr)
        o = np.pad(o, (0, max(0, len(mid) - len(o))))[: len(mid)]
        S_lead = np.abs(librosa.stft(o, hop_length=hop))
    env_lead, idx_lead, _ = _band_onsets(S_lead, freqs, 300, 5000, sr, hop, 0.35, 3.2)
    # the bass line: from the separated bass track when there is one (nothing else in it), else the
    # harmonic part of the mix below 250 Hz
    S_b, bass_smooth = S_h, 0.25
    if stems and stems.get("bass") is not None:
        b = np.asarray(stems["bass"], dtype=np.float32)
        if stems.get("sr", sr) != sr:
            b = librosa.resample(b, orig_sr=stems["sr"], target_sr=sr)
        b = np.pad(b, (0, max(0, len(mid) - len(b))))[: len(mid)]
        S_b, bass_smooth = np.abs(librosa.stft(b, hop_length=hop)), 0.12      # quick enough to follow a wobble
    if S_b is None:                                      # no bass track after all: the mix's harmonic part
        S_b = np.abs(librosa.stft(librosa.effects.harmonic(mid), hop_length=hop))
    env_bass, idx_bass, _ = _band_onsets(S_b, freqs, 35, 250, sr, hop, 0.30, 2.8)
    env_drum, idx_drum, _ = _band_onsets(S_p, freqs, 1200, 9000, sr, hop, 0.08, 1.8)
    present = None
    if stems:
        kick, snare, drum_cal, present = find_drums_both(S_p, freqs, sr, hop, duration, beats, phase, mid, stems, drum_hits, S_d=S_d)
    else:
        kick, snare, drum_cal = find_drums(S_p, freqs, sr, hop, duration, beats, phase)

    streams = {}
    for name, env, idx, S, lo, hi in (("lead", env_lead, idx_lead, S_lead, 300, 5000),
                                      ("bass", env_bass, idx_bass, S_b, 35, 250),
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
    cuts = find_cuts(mid, sr, hop)
    bass_hits = []
    if stems and stems.get("bass") is not None:
        bb = np.asarray(stems["bass"], dtype=np.float32)
        if stems.get("sr", sr) != sr:
            bb = librosa.resample(bb, orig_sr=stems["sr"], target_sr=sr)
        bass_hits = find_bass_hits(bb, sr, beats)
    # what makes this kind of song different: 808s, hat rolls, wobbles, a key change (see flavor.py)
    from . import flavor
    b808, hatrolls, wobbles = [], [], []
    if stems and stems.get("bass") is not None:
        b808 = flavor.find_808s(bb, sr, bass_hits)
        wobbles = flavor.find_wobbles(bb, sr, beats)
    if stems and stems.get("drums") is not None and (drum_cal or {}).get("has_drums", True) is not False:
        dd = np.asarray(stems["drums"], dtype=np.float32)
        if stems.get("sr", sr) != sr:
            dd = librosa.resample(dd, orig_sr=stems["sr"], target_sr=sr)
        hatrolls = flavor.find_hat_rolls(dd, sr, beats)
    try:
        key_change = flavor.find_key_change(mid, sr, duration)
    except Exception:  # noqa: BLE001 - a key change is a nicety
        key_change = None
    levels = (stems or {}).get("levels") or {}
    lfps = float((stems or {}).get("fps") or 30)
    regrid = lambda xs: [round(float(v), 3) for v in np.interp(out_times, np.arange(len(xs)) / lfps, xs / (np.percentile(xs, 95) + 1e-9)).clip(0, 1)] if len(xs) else []
    vocals = regrid(np.asarray(levels.get("vocals") or [], dtype=float))
    other = regrid(np.asarray(levels.get("other") or [], dtype=float))
    entries = find_entries(other, feature_fps, beats, phase) if other else []
    fills = find_fills(streams["drum"][0], duration, beats)

    def pack(name):
        t, s, p = streams[name]
        return [[round(float(a), 3), round(float(b), 2), int(c)] for a, b, c in zip(t, s, p)]

    def pack_hits(hits):
        return [[round(float(a), 3), round(float(b), 2), 0] for a, b in hits]

    return {
        "events": {**{k: pack(k) for k in ("lead", "bass", "ghost")}, "kick": pack_hits(kick), "snare": pack_hits(snare),
                   "roll": pack_hits((drum_cal or {}).pop("roll_hits", [])), "basshit": pack_hits(bass_hits),
                   "b808": [[round(t, 3), round(v, 2), 0, h] for t, v, h in b808]},
        "rolls": (drum_cal or {}).get("rolls", []),
        "fills": fills,
        "hook": hook,
        "curves": {
            "lead": [round(float(v), 3) for v in _level(S_lead, freqs, 300, 5000, sr, hop, out_times)],
            "bass": [round(float(v), 3) for v in _level(S_b, freqs, 35, 250, sr, hop, out_times, bass_smooth)],
            "drum": [round(float(v), 3) for v in _level(S_p, freqs, 1200, 9000, sr, hop, out_times, 0.15)],
            **({"drums_present": [round(float(v), 3) for v in np.interp(out_times, librosa.frames_to_time(np.arange(len(present)), sr=sr, hop_length=hop), present)]}
               if present is not None else {}),
            **({"vocals": vocals} if vocals else {}), **({"other": other} if other else {}),
        },
        "stereo": bool(wide),
        "drum_calibration": drum_cal,                   # the bands this song's kick and snare were found in
        "cuts": cuts,                                   # the music drops out for a moment, then comes back
        "entries": entries,                             # the synths and guitars come in after being away
        "hatrolls": hatrolls,                           # trap hi-hat rolls: [start, end, hits a beat]
        "wobbles": wobbles,                             # the bass wobbling: [start, end, wobbles a beat]
        "key_change": key_change,                       # {time, up}: the song moves up a key near the end
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
