"""The learned section labeler: a song's sections from what the user taught it (the studio's review).

The rules in analysis.settle_sections read a song first; then, where the song has SongFormer's features
(songformer_infer.features, saved as it is learned) and a model has been trained, the labeler labels every bar
(intro / verse / buildup / drop / breakdown / outro) and its sections replace the rules'. A song the user reviewed
keeps the user's own sections whatever either says. Each bar is described by:

  - its levels (loudness, bass, drums, voice, the other instruments), against the song's and the bars around it;
  - SongFormer's labels and three of its label heads (dataset prefixes 5, 0 and 6), its boundary strength;
  - the rules' own label, the song map (parts, tension), drum events, its place in the song;
  - SongFormer's audio embeddings (MuQ + MusicFM, PCA 64 raw and against the song), how much it repeats;
  - the lyrics: sung or not, how often the words sung here come back, the hook line, first time heard.

XGBoost on the graphics card learns them from every reviewed song (train: ~10 s); the bars' labels are smoothed
into sections (the likeliest run of labels, a cost for every change, nothing shorter than two bars).

2026-10-19, 84 reviewed songs, leaving one song out (time share right, types / drop-not-drop):
  all 84: the rules 78.7% / 86.2%, the labeler 83.7% / 91.5%; the 35 reviewed after the rules were last tuned
  (the fair test, the most disputed songs): the rules 68.5% / 78.1%, the labeler 80.1% / 89.7%.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JAM = os.path.join(ROOT, "shows", "_jam")
FEAT = os.path.join(JAM, "features")                        # SongFormer's features, one set a song
CACHE = os.path.join(FEAT, "bars")                          # per-bar features, one file a song
MODEL = os.path.join(JAM, "labeler.joblib")
TYPES = ["intro", "verse", "buildup", "drop", "breakdown", "outro"]
SF = ["intro", "verse", "pre-chorus", "chorus", "bridge", "inst", "outro", "silence"]
K = ("e", "bass", "dp", "drum", "voc", "other")
NPC = 64
CHANGE_COST = 2.0                                           # (log-probability) for each change of label
MIN_BARS = 2


def has_features(tid: str) -> bool:
    return bool(tid) and all(os.path.exists(os.path.join(FEAT, tid + ext)) for ext in (".emb2.npy", ".npz", ".json"))


def truth(jam: str = JAM) -> dict:
    try:
        with open(os.path.join(jam, "section_truth.json"), encoding="utf-8") as fh:
            return {k: v for k, v in json.load(fh).items() if not k.startswith("_") and v.get("sections")}
    except (OSError, ValueError):
        return {}


def _share(segs, t0, t1, names, key="label"):
    v = np.zeros(len(names))
    for s in segs or []:
        o = min(t1, float(s["end"])) - max(t0, float(s["start"]))
        lab = str(s.get(key, "")).lower()
        if o > 0 and lab in names:
            v[names.index(lab)] += o / max(1e-6, t1 - t0)
    return v


def _label_at(secs, t):
    return next((s["type"] for s in secs if s["start"] <= t < s["end"]), None)


def _lyrics(tid, spans):
    try:
        with open(os.path.join(JAM, "lyrics", tid + ".json"), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return np.full((len(spans), 6), np.nan)
    words = [w for w in d.get("words") or [] if w.get("w") and not w.get("bg")]
    lines = d.get("lines") or []
    if not words:
        return np.full((len(spans), 6), np.nan)
    toks = [set(" ".join(re.sub(r"[^a-z0-9' ]", " ", str(x.get("text")).lower()).split()).split()) for x in lines]
    sim = lambda a, b: bool(a and b and len(a & b) / len(a | b) >= 0.7)
    rep = np.array([sum(1 for b in toks if sim(a, b)) for a in toks], float)
    top = rep.max() if len(rep) else 1.0
    hook = {i for i in range(len(lines)) if rep[i] == top and top >= 3}
    first = [not any(sim(a, b) for b in toks[:i]) for i, a in enumerate(toks)]
    out = []
    for b0, b1 in spans:
        ws = [w for w in words if b0 <= float(w["t"]) < b1]
        sung = sum(max(0.0, min(b1, float(w.get("end", w["t"]))) - max(b0, float(w["t"]))) for w in ws) / max(1e-6, b1 - b0)
        li = {int(w["line"]) for w in ws if w.get("line") is not None and int(w["line"]) < len(lines)}
        r = max((rep[i] for i in li), default=0.0)
        out.append([min(1.0, sung), r, r / top if top else 0.0, float(bool(li & hook)), float(any(first[i] for i in li)) if li else 0.0,
                    len(ws) / max(1e-6, b1 - b0)])
    return np.array(out, float)


def _songformer(tid, spans):
    """SongFormer's embeddings (0.48 s steps) and heads for the bars, or (None, None)."""
    try:
        E = np.load(os.path.join(FEAT, tid + ".emb2.npy")).astype(np.float32)
        Z = np.load(os.path.join(FEAT, tid + ".npz"))
        with open(os.path.join(FEAT, tid + ".json")) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None, None
    fps = float(meta.get("emb2_fps", 25.0 / 12))
    emb = np.array([E[int(b0 * fps):max(int(b0 * fps) + 1, int(b1 * fps))].mean(0) if int(b0 * fps) < len(E) else E[-1] for b0, b1 in spans])
    cols = []
    for name in ("sf5", "sf6", "sf0"):
        f, b, hf = Z[f"{name}_func"], Z[f"{name}_bound"], meta[name]["fps"]
        rows = []
        for b0, b1 in spans:
            i0 = int(b0 * hf)
            seg = f[i0:max(i0 + 1, int(b1 * hf))] if i0 < len(f) else f[-1:]
            bs = b[max(0, i0 - 2):min(len(b), i0 + 3)] if i0 < len(b) else b[-1:]
            rows.append(list(seg.mean(0)) + [float(bs.max()) if len(bs) else 0.0])
        cols.append(np.array(rows))
    return emb, np.concatenate(cols, 1)


def bar_features(a: dict, tid: str) -> dict | None:
    """Every bar's features, from an analysis the rules have settled (its sections the rules', its song map on them)."""
    from .analysis import _bar_levels
    dur = float(a["duration"])
    bl = _bar_levels(a, 0.0, dur)
    spans = [(b0, b1) for b0, b1, _ in bl]
    emb, heads = _songformer(tid, spans)
    if len(bl) < 16 or emb is None:
        return None
    L = np.array([[lv[k] for k in K] for _, _, lv in bl])
    Z = (L - L.mean(0)) / (L.std(0) + 1e-6)
    n = len(bl)
    ctx = lambda lo, hi: np.array([Z[max(0, i + lo):max(0, i + hi) + 1].mean(0) if max(0, i + lo) <= min(n - 1, i + hi) else Z[i] for i in range(n)])
    prev2, next2, around = ctx(-2, -1), ctx(1, 2), ctx(-4, 4)
    ev = a.get("events") or {}
    parts = (a.get("songmap") or {}).get("parts") or []
    ten = (a.get("songmap") or {}).get("tension") or {}
    tv, tt0, tdt = np.asarray(ten.get("values") or [0.0]), float(ten.get("t0") or 0), float(ten.get("dt") or 1)
    letters = [p["part"] for p in parts]
    base, rule = [], []
    for i, (b0, b1) in enumerate(spans):
        mid = 0.5 * (b0 + b1)
        r = _label_at(a["sections"], mid) or "verse"
        part = next((p for p in parts if p["start"] <= mid < p["end"]), None)
        ti = int((mid - tt0) / tdt) if tdt else 0
        base.append(list(L[i]) + list(Z[i]) + list(Z[i] - prev2[i]) + list(next2[i] - Z[i]) + list(around[i])
                    + list(_share(a.get("songformer"), b0, b1, SF)) + [1.0 if r == t else 0.0 for t in TYPES]
                    + [i / n, (n - i) / n, min(i, 16) / 16, min(n - 1 - i, 16) / 16]
                    + [letters.count(part["part"]) if part else 0, (part["end"] - part["start"]) / dur if part else 0.0]
                    + [float(tv[ti]) if 0 <= ti < len(tv) else 0.0]
                    + [sum(1 for e in ev.get(k) or [] if b0 <= e[0] < b1) for k in ("snare", "kick", "roll", "lead")])
        rule.append(TYPES.index(r) if r in TYPES else 1)
    return {"base": np.array(base, float), "emb": emb, "heads": heads, "lyr": _lyrics(tid, spans), "rule": np.array(rule),
            "spans": np.array(spans)}


def matrix(s, pcas):
    E = s["emb"]
    Pr, Pc = pcas[0].transform(E), pcas[1].transform(E - E.mean(0))
    n = len(E)
    nb = lambda M, k: np.array([M[min(n - 1, max(0, i + k))] for i in range(n)])
    U = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-6)
    S = U @ U.T
    np.fill_diagonal(S, np.nan)
    rep = np.c_[np.nanmean(S, 1), np.sort(np.nan_to_num(S, nan=-1), 1)[:, -5:].mean(1)]
    return np.concatenate([s["base"], Pr, Pc, Pc - nb(Pc, -1), nb(Pc, 1) - Pc, rep, s["heads"], s["lyr"]], 1).astype(np.float32)


class Model:
    """XGBoost on the graphics card (300 rounds on ~7,000 bars in ~5 s), each label weighted as if equally common."""

    def __init__(self):
        from xgboost import XGBClassifier
        self.clf = XGBClassifier(n_estimators=300, learning_rate=0.05, max_depth=6, subsample=0.9, colsample_bytree=0.6,
                                 reg_lambda=1.0, tree_method="hist", device="cuda", n_jobs=8, random_state=0)

    def fit(self, X, y):
        from sklearn.utils.class_weight import compute_sample_weight
        self.classes_ = np.unique(y)
        remap = {c: i for i, c in enumerate(self.classes_)}           # (XGBoost wants the labels it saw as 0..n-1)
        self.clf.fit(X, np.array([remap[v] for v in y]), sample_weight=compute_sample_weight("balanced", y))
        self.clf.set_params(device="cpu")                            # (labelling a song: a few hundred bars, no card needed)
        return self

    def proba(self, X):
        P = np.zeros((len(X), len(TYPES)))
        P[:, self.classes_] = self.clf.predict_proba(X)
        return P


def smooth(P: np.ndarray, cost: float = CHANGE_COST, min_bars: int = MIN_BARS) -> np.ndarray:
    """The likeliest labels for the bars as runs (a cost for each change), then runs shorter than min_bars folded
    into the neighbor they sound more like."""
    n, k = P.shape
    lp = np.log(P + 1e-9)
    D, back = lp[0].copy(), np.zeros((n, k), int)
    for i in range(1, n):
        T = D[:, None] - cost * (1 - np.eye(k))
        back[i] = T.argmax(0)
        D = T.max(0) + lp[i]
    lab = [int(D.argmax())]
    for i in range(n - 1, 0, -1):
        lab.append(int(back[i][lab[-1]]))
    lab = np.array(lab[::-1])
    changed = True
    while changed:
        changed = False
        runs, i = [], 0
        while i < n:
            j = i
            while j + 1 < n and lab[j + 1] == lab[i]:
                j += 1
            runs.append((i, j + 1))
            i = j + 1
        for r, (i0, i1) in enumerate(runs):
            if i1 - i0 >= min_bars or len(runs) == 1:
                continue
            left = lab[runs[r - 1][0]] if r > 0 else None
            right = lab[runs[r + 1][0]] if r + 1 < len(runs) else None
            opts = [c for c in (left, right) if c is not None]
            lab[i0:i1] = max(opts, key=lambda c: lp[i0:i1, c].sum())
            changed = True
            break
    return lab


def to_sections(lab: np.ndarray, spans: np.ndarray, duration: float) -> list[dict]:
    out = []
    for i, c in enumerate(lab):
        b0, b1 = float(spans[i][0]), float(spans[i][1])
        if out and out[-1]["type"] == TYPES[c]:
            out[-1]["end"] = round(b1, 3)
        else:
            out.append({"type": TYPES[c], "start": round(b0, 3), "end": round(b1, 3)})
    if out:
        out[0]["start"], out[-1]["end"] = 0.0, round(float(duration), 3)
    return out


# ---------------------------------------------------------------- the trained model, used as songs load

_LOADED: dict = {}
_LOCK = threading.Lock()


def _load():
    try:
        mtime = os.path.getmtime(MODEL)
    except OSError:
        return None
    with _LOCK:
        if _LOADED.get("mtime") != mtime:
            import joblib
            _LOADED.update(joblib.load(MODEL), mtime=mtime)
        return _LOADED


def apply(a: dict) -> list[dict] | None:
    """The labeler's sections for a settled analysis (the rules' sections and song map in it), or None: no model, no
    SongFormer features for the song, too short."""
    tid = a.get("track_id")
    if not tid or not has_features(tid):
        return None
    m = _load()
    if not m:
        return None
    f = bar_features(a, tid)
    if f is None:
        return None
    P = m["model"].proba(matrix(f, m["pcas"]))
    return to_sections(smooth(P), f["spans"], float(a["duration"]))


def learn_features(track, path: str) -> dict | None:
    """SongFormer's features for a song just learned (learn ahead), from its copy: the model host does the work."""
    if has_features(track.id):
        return None
    from . import structure_model
    return structure_model.save_features(path, FEAT, track.id)


# ---------------------------------------------------------------- training (the review's songs)


RETRAIN_EVERY = 5                                           # reviews between the studio's own retrains
_RETRAIN: dict = {"running": False}


def trained_on() -> int:
    m = _load()
    return int(m.get("songs", 0)) if m else 0


def retrain_soon(jam: str = JAM, log=print) -> bool:
    """Retrain in the background (its own process: the studio is never held up) once RETRAIN_EVERY more songs have
    been reviewed than the model was trained on (and the first time, from 10). Returns whether one started."""
    reviewed = sum(1 for t in truth(jam) if has_features(t))
    have = trained_on()
    if _RETRAIN["running"] or reviewed < 10 or (have and reviewed < have + RETRAIN_EVERY):
        return False
    _RETRAIN["running"] = True

    def run():
        import subprocess
        import sys
        try:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "section_labeler.py"), "train"], cwd=ROOT,
                               capture_output=True, text=True, timeout=1800,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            line = next((x for x in r.stdout.splitlines() if x.startswith("[labeler]")), "")
            log(line or f"[labeler] retrain failed: {(r.stderr or '').strip().splitlines()[-1:]}")
        except Exception as e:  # noqa: BLE001
            log(f"[labeler] retrain failed: {type(e).__name__}: {e}")
        finally:
            _RETRAIN["running"] = False
    threading.Thread(target=run, daemon=True, name="labeler-retrain").start()
    log(f"[labeler] retraining on {reviewed} reviewed songs (was {have})")
    return True


def song_bars(lib, tid: str) -> dict | None:
    """One learned song's bar features as the rules read it (cached), with the user's labels (-1 where none)."""
    path = os.path.join(CACHE, tid + ".npz")
    if os.path.exists(path):
        z = np.load(path)
        out = {k: z[k] for k in z.files}
    else:
        doc = lib.learned(tid)
        if not doc:
            return None
        a = doc["analysis"]
        a.pop("sections_truth", None)                         # the rules' own answer, not the user's
        a["_no_labeler"] = True                                # nor the labeler's
        from .analysis import settle_sections
        settle_sections(a)
        f = bar_features(a, tid)
        if f is None:
            return None
        out = {**f, "name": np.array(doc["track"]["name"])}
        os.makedirs(CACHE, exist_ok=True)
        np.savez_compressed(path, **out)
    t = truth(lib.dir).get(tid)
    out["y"] = np.array([TYPES.index(_label_at(t["sections"], 0.5 * (b0 + b1))) if t and _label_at(t["sections"], 0.5 * (b0 + b1)) in TYPES
                         else -1 for b0, b1 in out["spans"]])
    out["tid"] = tid
    return out


def load_all(lib) -> list[dict]:
    tids = [f[:-9] for f in os.listdir(FEAT) if f.endswith(".emb2.npy")] if os.path.isdir(FEAT) else []
    return [s for s in (song_bars(lib, t) for t in tids) if s is not None]


def _pca(songs):
    from sklearn.decomposition import PCA
    R = np.concatenate([s["emb"][::2] for s in songs])
    C = np.concatenate([(s["emb"] - s["emb"].mean(0))[::2] for s in songs])
    return PCA(NPC, random_state=0).fit(R), PCA(NPC, random_state=0).fit(C)


def train(lib, log=print) -> dict:
    """Fit on every reviewed song and save it; then rank the songs not reviewed by how unsure the labeler is of them
    (review_order.json: the review goes through those first)."""
    t0 = time.time()
    songs = load_all(lib)
    pcas = _pca(songs)
    rev = [s for s in songs if (s["y"] >= 0).any()]
    if len(rev) < 10:
        raise ValueError(f"only {len(rev)} reviewed songs with features: review a few more first")
    M = {s["tid"]: matrix(s, pcas) for s in songs}
    X = np.concatenate([M[s["tid"]][s["y"] >= 0] for s in rev]); yy = np.concatenate([s["y"][s["y"] >= 0] for s in rev])
    model = Model().fit(X, yy)
    import joblib
    tmp = MODEL + ".tmp"
    joblib.dump({"model": model, "pcas": pcas, "songs": len(rev), "bars": int(len(yy)), "trained": time.strftime("%Y-%m-%d %H:%M")}, tmp)
    os.replace(tmp, MODEL)
    # The songs to review next: where the labeler is least sure of itself (its bars split between labels, above all
    # between drop and not-drop), so each review teaches it the most. (It used to be where it disagreed with the
    # rules: a song it knew well, a twin of one reviewed, came first: Found - Live, after Found was reviewed.)
    reviewed = {s["tid"] for s in rev}
    doubt = {}
    for s in songs:
        if s["tid"] in reviewed or any(t in reviewed for t, _ in lib.twins(s["tid"])):
            continue
        P = model.proba(M[s["tid"]])
        w = s["spans"][:, 1] - s["spans"][:, 0]
        unsure = 1.0 - P.max(1)
        drop_unsure = 1.0 - np.abs(2.0 * P[:, 3] - 1.0)
        doubt[s["tid"]] = round(float(np.average(0.5 * unsure + drop_unsure, weights=w)), 4)
    with open(os.path.join(lib.dir, "review_order.json"), "w", encoding="utf-8") as fh:
        json.dump({"_about": "how unsure the learned labeler is of each song not reviewed yet (review the high ones first)",
                   **dict(sorted(doubt.items(), key=lambda kv: -kv[1]))}, fh, indent=1)
    log(f"[labeler] trained on {len(rev)} reviewed songs ({len(yy)} bars) in {time.time() - t0:.0f} s; {len(doubt)} songs ranked for review")
    return {"songs": len(rev), "bars": int(len(yy))}


def evaluate(lib, log=print) -> None:
    """Leaving one song out: the rules against the labeler (bar by bar, and smoothed into sections), all reviewed
    songs and by the day they were reviewed (songs reviewed after the rules were last tuned are the fair test)."""
    songs = load_all(lib)
    pcas = _pca(songs)
    rev = [s for s in songs if (s["y"] >= 0).any()]
    names = sorted({str(s["name"]).lower() for s in rev})
    log(f"{len(rev)} reviewed songs ({len(songs)} with features)")
    M = {s["tid"]: matrix(s, pcas) for s in rev}
    Y, R, P, S, W, G = [], [], [], [], [], []
    added = {k: v.get("added", "?") for k, v in truth(lib.dir).items()}
    for k, nm in enumerate(names):
        tr = [s for s in rev if str(s["name"]).lower() != nm]
        model = Model().fit(np.concatenate([M[s["tid"]][s["y"] >= 0] for s in tr]), np.concatenate([s["y"][s["y"] >= 0] for s in tr]))
        if k % 20 == 0:
            log(f"  {k}/{len(names)} songs scored")
        for s in (s for s in rev if str(s["name"]).lower() == nm):
            m = s["y"] >= 0
            pr = model.proba(M[s["tid"]])
            Y.append(s["y"][m]); R.append(s["rule"][m]); P.append(pr.argmax(1)[m]); S.append(smooth(pr)[m])
            W.append((s["spans"][:, 1] - s["spans"][:, 0])[m]); G.append(np.full(int(m.sum()), added.get(s["tid"], "?")))
    y, r, p, sm, w, g = map(np.concatenate, (Y, R, P, S, W, G))

    def acc(q, mask):
        return f"types {np.average((q == y)[mask], weights=w[mask]):.1%}  drop/not-drop {np.average(((q == 3) == (y == 3))[mask], weights=w[mask]):.1%}"
    for title, mask in [("all reviewed songs", np.ones(len(y), bool))] + [(f"reviewed {d}", g == d) for d in sorted(set(g))]:
        log(title)
        log(f"  the rules:                 {acc(r, mask)}")
        log(f"  the labeler, bar by bar:   {acc(p, mask)}")
        log(f"  the labeler, as sections:  {acc(sm, mask)}")
