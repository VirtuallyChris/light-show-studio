"""Know the song: its parts, its tension and its moments, from the music itself.

A lighting designer does not light "a verse" and "a drop". They learn the song: which parts come back (so the
second chorus is recognized the moment it starts, and grows), where it winds up and where it lets go, and the
handful of moments the room waits for. This works that out per song from what the analysis already holds
(the spectrum, the separated stems, every drum hit, the hook), and nothing else: no genre, no labels.

- parts: every section compared with every other by how it sounds and plays bar by bar over time; the ones
  that are the same part share a letter (A, B, C...), with which time this is (1st, 2nd...) and how many.
- tension: a curve, 0..1 within the song, of what makes music wind up: the snares speeding up, the sound
  brightening (filters opening, risers), the bass pulling out under drums, the loudness climbing. Releases:
  where wound-up tension snaps into a big landing. Builds: tension rising four bars or more into one.
- moments: the song's top ten, ranked: releases, an instrument entering for the first time, the biggest
  hits, the music stopping dead, a new part arriving, a key change. Kept a phrase apart.

add(analysis) puts it all in analysis["songmap"]; it runs on every analysis, new or saved, like the
section rules, so the learned songs have it without being learned again.
"""

from __future__ import annotations

import numpy as np

SPECTRUM_W = 1.0     # how much the sound counts against the stems in telling parts apart
STEMS = ("vocals", "other", "lead", "bass", "drums_present")


def _bars(a: dict) -> list[float]:
    bars = [float(b) for b in a.get("downbeats") or []]
    if len(bars) < 4:
        beats = [float(b) for b in a.get("beats") or []]
        bars = beats[int(a.get("downbeat_phase") or 0)::4]
    return bars


def bar_features(a: dict) -> tuple[list[float], np.ndarray]:
    """The song bar by bar: its spectrum (each band against its own run) and its stems' levels."""
    bars = _bars(a)
    if len(bars) < 8:
        return bars, np.zeros((0, 0))
    fps = float(a.get("feature_fps", 30))
    cur = a.get("curves") or {}
    cols = []
    B = a.get("align_bands")
    if B:
        B = np.asarray(B, dtype=np.float32)
        afps = float(a.get("align_fps", 43.0))
        B = (B - B.mean(axis=1, keepdims=True)) / (B.std(axis=1, keepdims=True) + 1e-6)
        spec = np.array([B[:, int(b0 * afps):max(int(b0 * afps) + 1, int(b1 * afps))].mean(axis=1) if int(b0 * afps) < B.shape[1] else np.zeros(B.shape[0])
                         for b0, b1 in zip(bars[:-1], bars[1:])])
        cols.append(SPECTRUM_W * spec / np.sqrt(B.shape[0]))
    stem = []
    for k in STEMS + ("energy",):
        c = np.asarray((cur.get(k) or []) if k != "energy" else (a.get("energy") or []), dtype=float)
        if not len(c):
            continue
        v = np.array([c[int(b0 * fps):max(int(b0 * fps) + 1, int(b1 * fps))].mean() if int(b0 * fps) < len(c) else 0.0 for b0, b1 in zip(bars[:-1], bars[1:])])
        stem.append((v - v.mean()) / (v.std() + 1e-6))
    if stem:
        cols.append(np.stack(stem, axis=1) / np.sqrt(len(stem)))
    return bars, (np.concatenate(cols, axis=1) if cols else np.zeros((len(bars) - 1, 0)))


def _alike(F: np.ndarray, i0: int, n0: int, j0: int, n1: int, win: int = 8, reach: int = 2, slide: int = 4) -> float:
    """How alike two stretches of bars are: the best correlation of eight bars of one with eight of the other
    in step, starting up to four bars into the first and two bars either way in the second (a chorus heard
    again with a bar more at its start, or its own ending)."""
    n = min(n0, n1, win)
    if n < 2:
        return 0.0
    best = -1.0
    for da in range(0, max(1, min(slide, n0 - n) + 1)):
        for lag in range(-reach, reach + 1):
            a0, b0 = i0 + da, j0 + da + lag
            if b0 < 0 or b0 + n > len(F) or a0 + n > len(F):
                continue
            # each stretch against its own run, feature by feature: alike is moving alike bar by bar, not
            # both being quieter than the chorus (a bridge came out the verse again)
            x, y = F[a0:a0 + n], F[b0:b0 + n]
            x, y = (x - 0.5 * x.mean(axis=0)).ravel(), (y - 0.5 * y.mean(axis=0)).ravel()
            if x.std() < 1e-9 or y.std() < 1e-9:
                continue
            best = max(best, float(np.corrcoef(x, y)[0, 1]))
    return best


SAME_PART = 0.62     # bars in step this alike are the same part (a chorus heard again scores 0.7 to 0.9)
AGREED_PART = 0.42   # ...or this alike, when SongFormer calls both the same thing and they are the same kind of section
MODEL_KIND = {"chorus": "chorus", "verse": "verse", "pre-chorus": "verse", "prechorus": "verse", "intro": "intro", "outro": "outro", "bridge": "bridge"}


def _model_kind(a: dict, t0: float, t1: float) -> str | None:
    """What SongFormer calls most of t0..t1 (its own labels, kept with the analysis), if anything."""
    share: dict[str, float] = {}
    for x in a.get("songformer") or []:
        o = min(t1, float(x["end"])) - max(t0, float(x["start"]))
        k = MODEL_KIND.get(str(x.get("label", "")).lower())
        if o > 0 and k:
            share[k] = share.get(k, 0.0) + o
    return max(share, key=share.get) if share and max(share.values()) >= 0.5 * (t1 - t0) else None


def parts(a: dict) -> list[dict]:
    """Each section with its part: [{start, end, type, part ('A'...), occ (1...), of}]."""
    secs = a.get("sections") or []
    key = (len(a.get("downbeats") or []), (a.get("downbeats") or [None])[0], (a.get("downbeats") or [None])[-1])
    hit = a.get("_songmap_bars")                         # (the same bars both times a load maps the song: kept)
    if hit and hit[0] == key:
        bars, F = hit[1]
    else:
        bars, F = bar_features(a)
        a["_songmap_bars"] = (key, (bars, F))
    if not secs or len(F) < 4:
        return []
    idx = lambda t: int(np.clip(np.searchsorted(bars, t + 0.3) - 1, 0, len(F) - 1))
    found, reps = [], []                                  # reps: (part letter, first bar, bars, kind of section, SongFormer's word)
    for s in secs:
        i0, i1 = idx(s["start"]), idx(s["end"] - 0.3) + 1
        n = max(1, i1 - i0)
        mk = _model_kind(a, s["start"], s["end"])
        best, who = -1.0, None
        for letter, j0, m, typ, mk0 in reps:                     # (every time each part was heard, not only its first)
            sim = _alike(F, i0, n, j0, m)
            need = AGREED_PART if mk and mk == mk0 and typ == s.get("type") else SAME_PART
            if sim >= need and sim > best and 1 / 3 <= n / max(1, m) <= 3:
                best, who = sim, letter
        if who is None:
            who = chr(ord("A") + len({r[0] for r in reps})) if len({r[0] for r in reps}) < 26 else "Z"
        reps.append((who, i0, n, s.get("type"), mk))
        found.append({"start": round(float(s["start"]), 3), "end": round(float(s["end"]), 3), "type": s.get("type"), "part": who})
    count: dict[str, int] = {}
    for f in found:
        count[f["part"]] = count.get(f["part"], 0) + 1
        f["occ"] = count[f["part"]]
    for f in found:
        f["of"] = count[f["part"]]
    return found


def tension(a: dict, step: float = 0.25) -> dict:
    """The tension curve (one value a quarter of a bar, 0..1 within the song), its releases and its builds."""
    bars = _bars(a)
    en = np.asarray(a.get("energy") or [], dtype=float)
    cur = a.get("curves") or {}
    if len(bars) < 8 or not len(en):
        return {"step": step, "t0": 0.0, "values": [], "releases": [], "builds": []}
    fps = float(a.get("feature_fps", 30))
    bar = float(np.median(np.diff(bars)))
    dt = step * bar
    t = np.arange(bars[0], min(bars[-1] + bar, len(en) / fps), dt)
    at = lambda c, x: float(np.mean(c[int(x * fps):max(int(x * fps) + 1, int((x + dt) * fps))])) if int(x * fps) < len(c) else 0.0
    ev = a.get("events") or {}
    sn = np.array(sorted(float(e[0]) for k in ("snare", "roll") for e in ev.get(k) or []))
    snare = np.array([((sn >= x - bar) & (sn < x)).sum() / 8.0 for x in t]) if len(sn) else np.zeros(len(t))  # per bar, 8 = sixteenths
    B = a.get("align_bands")
    if B:
        B = np.asarray(B, dtype=np.float32)
        afps = float(a.get("align_fps", 43.0))
        hi = B[int(B.shape[0] * 0.66):].mean(axis=0) - B.mean(axis=0)            # the top of the spectrum against the rest
        bright = np.array([hi[int(x * afps):max(int(x * afps) + 1, int((x + dt) * afps))].mean() if int(x * afps) < len(hi) else 0.0 for x in t])
    else:
        bright = np.zeros(len(t))
    loud = np.array([at(en, x) for x in t])
    bass_c, drums_c = np.asarray(cur.get("bass") or [0.0]), np.asarray(cur.get("drums_present") or [0.0])   # (once, not per step)
    bass = np.array([at(bass_c, x) for x in t])
    drums = np.array([at(drums_c, x) for x in t])

    def norm(v):
        lo, hi_ = np.percentile(v, 5), np.percentile(v, 95)
        return np.clip((v - lo) / (hi_ - lo + 1e-9), 0, 1)

    def smooth(v, n):
        n = max(1, int(n))
        return np.convolve(v, np.ones(n) / n, mode="same")
    per_bar = int(round(1 / step))
    rise = np.clip(np.gradient(smooth(loud, 2 * per_bar)) * per_bar * 4, 0, None)       # loudness climbing over two bars
    bass_out = (1 - norm(bass)) * norm(drums)                                            # the bass pulled out, the drums going on
    raw = 0.35 * norm(snare) + 0.25 * norm(smooth(bright, per_bar)) + 0.2 * bass_out + 0.2 * norm(rise)
    ten = norm(smooth(raw, per_bar))
    # releases: wound up (0.55 or more over the bar before), then the music lands big within a beat (the
    # loudness up 0.12 of the song's loud level, or the drums full again)
    top = float(np.percentile(en, 90)) or 1.0
    releases, last = [], -1e9
    for k in range(per_bar, len(t) - per_bar):
        before = ten[k - per_bar:k].max()
        jump = (loud[k:k + per_bar].mean() - loud[k - per_bar:k].mean()) / top
        dj = drums[k:k + per_bar].mean() - drums[k - per_bar:k].mean()
        if before >= 0.55 and (jump >= 0.12 or dj >= 0.35) and t[k] - last > 4 * bar:
            b = min(bars, key=lambda x: abs(x - t[k]))                                   # on the bar line nearest
            # ...or on the drop it is, when one starts within a bar and a half (the loudness can climb a bar
            # ahead of the drop: Enjoy's first release came out at 45.0, its drop lands at 47.3)
            near = [s["start"] for s in a.get("sections") or [] if s.get("type") == "drop" and abs(s["start"] - t[k]) <= 1.5 * bar]
            if near:
                b = min(near, key=lambda x: abs(x - t[k]))
            releases.append({"t": round(b, 3), "strength": round(float(before * min(1.0, max(jump * 3, dj * 1.5))), 3)})
            last = t[k]
    builds = []
    for r in releases:                                                                   # how far back it was winding up
        k = int(np.searchsorted(t, r["t"])) - 1
        j = k
        while j > 0 and ten[j - 1] <= ten[j] + 0.03 and t[k] - t[j - 1] <= 16 * bar:
            j -= 1
        if t[k] - t[j] >= 4 * bar - 1e-6:
            builds.append({"start": round(float(t[j]), 3), "end": r["t"]})
    return {"step": step, "t0": round(float(t[0]), 3), "dt": round(float(dt), 4),
            "values": [round(float(v), 3) for v in ten], "releases": releases, "builds": builds}


def moments(a: dict, ps: list[dict], ten: dict, keep: int = 10) -> list[dict]:
    """The song's top moments, ranked and kept four bars apart: [{t, kind, strength}]."""
    bars = _bars(a)
    if len(bars) < 8:
        return []
    bar = float(np.median(np.diff(bars)))
    fps = float(a.get("feature_fps", 30))
    cur = a.get("curves") or {}
    en = np.asarray(a.get("energy") or [], dtype=float)
    top = float(np.percentile(en, 90)) if len(en) else 1.0
    cands = [{"t": r["t"], "kind": "release", "strength": 0.6 + 0.4 * r["strength"]} for r in ten.get("releases") or []]
    # an instrument coming in for the first time (not at the song's start), on its bar
    for k, name in (("vocals", "the voice comes in"), ("lead", "the lead comes in"), ("bass", "the bass comes in"), ("drums_present", "the drums come in")):
        c = np.asarray(cur.get(k) or [], dtype=float)
        if not len(c):
            continue
        quiet = 0
        for b in bars[1:]:
            lvl = float(c[int(b * fps):max(int(b * fps) + 1, int((b + bar) * fps))].mean()) if int(b * fps) < len(c) else 0.0
            if lvl < 0.1:
                quiet += 1
            elif lvl >= 0.4 and quiet >= 2 and b > bars[0] + 4 * bar:
                cands.append({"t": round(b, 3), "kind": name, "strength": 0.55})
                break
            elif lvl >= 0.4:
                break
    # the biggest hits: the strongest kicks and snares where the song is loud
    ev = a.get("events") or {}
    hits = sorted(((float(e[1]) if len(e) > 1 else 0.5) * (float(en[min(len(en) - 1, int(float(e[0]) * fps))]) / top if len(en) else 1.0), float(e[0]))
                  for k in ("kick", "snare") for e in ev.get(k) or [])
    for s, t in hits[-3:]:
        cands.append({"t": round(t, 3), "kind": "the biggest hit", "strength": round(0.45 + 0.3 * min(1.0, s), 3)})
    for t0, t1 in a.get("cuts") or []:                                                   # the music stopping dead
        if t1 - t0 >= 0.4:
            cands.append({"t": round(float(t0), 3), "kind": "the music stops", "strength": 0.7})
    seen = set()
    for p in ps:                                                                         # a new part, the first time
        if p["occ"] == 1 and p["part"] not in seen and p["start"] > bars[0] + 4 * bar:
            cands.append({"t": p["start"], "kind": f"a new part ({p['part']})", "strength": 0.5 if p["of"] == 1 else 0.4})
        seen.add(p["part"])
    kc = a.get("key_change")
    if kc and kc.get("time"):
        cands.append({"t": round(float(kc["time"]), 3), "kind": "the key changes", "strength": 0.75})
    out = []
    for c in sorted(cands, key=lambda c: -c["strength"]):
        if all(abs(c["t"] - o["t"]) >= 4 * bar for o in out):
            out.append({**c, "strength": round(float(c["strength"]), 3)})
        if len(out) >= keep:
            break
    return sorted(out, key=lambda c: c["t"])


def add(a: dict) -> dict:
    """Work it all out and keep it in the analysis (analysis["songmap"]); returns it."""
    try:
        ps = parts(a)
        ten = tension(a)
        m = {"parts": ps, "tension": ten, "moments": moments(a, ps, ten)}
    except Exception as e:  # noqa: BLE001 - a song is still shown without its map
        m = {"parts": [], "tension": {"values": [], "releases": [], "builds": []}, "moments": [], "error": f"{type(e).__name__}: {e}"}
    a["songmap"] = m
    return m
