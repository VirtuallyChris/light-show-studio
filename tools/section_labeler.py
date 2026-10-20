"""A section labeler learned from the songs the user set right by ear (the studio's review, section_truth.json).

    .venv\\Scripts\\python tools\\section_labeler.py eval      score it against the rules, leaving one song out at a time
    .venv\\Scripts\\python tools\\section_labeler.py train     fit it on every reviewed song: shows/_jam/labeler.joblib
    .venv\\Scripts\\python tools\\section_labeler.py order     the songs to review next: where the labeler and the rules
                                                          disagree most (shows/_jam/review_order.json; the studio's
                                                          review goes through them in that order)

Every bar of a song is described by:
  - its levels (loudness, bass, drums, voice, the other instruments), against the song's and the bars around it;
  - SongFormer's labels and three of its label heads (dataset prefixes 5, 6 and 0), its boundary strength;
  - the rules' own label (analysis.settle_sections), the song map (parts, tension), drum events, its place;
  - SongFormer's audio embeddings (MuQ + MusicFM, PCA 64, raw and against the song), and how much it repeats;
  - the lyrics: sung or not, how often the words sung here come back, the hook line, first time heard.
A gradient-boosted classifier (XGBoost, on the graphics card) labels each bar intro / verse / buildup / drop /
breakdown / outro. The per-bar
features are cached (shows/_jam/features/bars); after the rules change, add --fresh.

The embeddings and heads come from tools\\songformer_features.py (the model environment, the graphics card):

    .venv-songformer\\Scripts\\python tools\\songformer_features.py

2026-10-19, 84 reviewed songs, leaving one song out (time share right, types / drop-not-drop):
  all 84:            the rules 78.7% / 86.2%   this labeler 83.7% / 91.5%
  the first 48:      the rules 85.9% / 92.1%   this labeler 86.2% / 92.7%   (the rules were tuned on some)
  the 35 reviewed next (the fair test; picked where the labeler and the rules disagreed most):
                     the rules 68.5% / 78.1%   this labeler 80.1% / 89.7%
  SongFormer alone 65.1% / 80.2% and All-In-One alone 65.7% / 80.7% (on the first 49).
"""

from __future__ import annotations

import json
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lightshow.studio.analysis import _bar_levels, settle_sections  # noqa: E402
from lightshow.studio.jam import JamLibrary  # noqa: E402

LIB = JamLibrary(os.path.join(ROOT, "shows"))
FEAT = os.path.join(LIB.dir, "features")                     # the SongFormer features (songformer_features.py)
CACHE = os.path.join(LIB.dir, "features", "bars")             # per-bar features, one file a song
TYPES = ["intro", "verse", "buildup", "drop", "breakdown", "outro"]
SF = ["intro", "verse", "pre-chorus", "chorus", "bridge", "inst", "outro", "silence"]
K = ("e", "bass", "dp", "drum", "voc", "other")
NPC = 64


def truth() -> dict:
    try:
        with open(os.path.join(LIB.dir, "section_truth.json"), encoding="utf-8") as fh:
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
        with open(os.path.join(LIB.dir, "lyrics", tid + ".json"), encoding="utf-8") as fh:
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


def _sf(tid, spans):
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


def bars(tid: str):
    """One song's per-bar features (cached), the rules' labels, the user's labels (-1 where not reviewed), bar spans."""
    path = os.path.join(CACHE, tid + ".npz")
    if os.path.exists(path):
        z = np.load(path)
        out = {k: z[k] for k in z.files}
        out["y"] = _user_labels(tid, out["spans"])             # (read fresh: the user reviews more songs)
        return out
    doc = LIB.learned(tid)
    if not doc:
        return None
    a = doc["analysis"]
    a.pop("sections_truth", None)                               # the rules' own answer, not the user's
    settle_sections(a)
    dur = float(a["duration"])
    bl = _bar_levels(a, 0.0, dur)
    spans = [(b0, b1) for b0, b1, _ in bl]
    emb, heads = _sf(tid, spans)
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
    out = {"base": np.array(base, float), "emb": emb, "heads": heads, "lyr": _lyrics(tid, spans), "rule": np.array(rule),
           "spans": np.array(spans), "name": np.array(doc["track"]["name"])}
    os.makedirs(CACHE, exist_ok=True)
    np.savez_compressed(path, **out)
    out["y"] = _user_labels(tid, out["spans"])
    return out


_TRUTH: dict | None = None


def _user_labels(tid, spans):
    global _TRUTH
    if _TRUTH is None:
        _TRUTH = truth()
    t = _TRUTH.get(tid)
    return np.array([TYPES.index(_label_at(t["sections"], 0.5 * (b0 + b1))) if t and _label_at(t["sections"], 0.5 * (b0 + b1)) in TYPES
                     else -1 for b0, b1 in spans])


def _pca(songs):
    from sklearn.decomposition import PCA
    R = np.concatenate([s["emb"][::2] for s in songs])
    C = np.concatenate([(s["emb"] - s["emb"].mean(0))[::2] for s in songs])
    return PCA(NPC, random_state=0).fit(R), PCA(NPC, random_state=0).fit(C)


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


class _XGB:
    """XGBoost on the graphics card (300 rounds on ~7,000 bars in ~5 s; scikit-learn's own boosting took ~6 min
    on this machine, most of it in the system's thread scheduling), each label weighted as if equally common."""

    def __init__(self):
        from xgboost import XGBClassifier
        self.clf = XGBClassifier(n_estimators=300, learning_rate=0.05, max_depth=6, subsample=0.9, colsample_bytree=0.6,
                                 reg_lambda=1.0, tree_method="hist", device="cuda", n_jobs=8, random_state=0)

    def fit(self, X, y):
        from sklearn.utils.class_weight import compute_sample_weight
        self.classes_ = np.unique(y)
        remap = {c: i for i, c in enumerate(self.classes_)}           # (XGBoost wants the labels it saw as 0..n-1)
        self.clf.fit(X, np.array([remap[v] for v in y]), sample_weight=compute_sample_weight("balanced", y))
        return self

    def predict_proba(self, X):
        self.clf.set_params(device="cpu")                            # (a few hundred bars: no copy to the card needed)
        try:
            return self.clf.predict_proba(X)
        finally:
            self.clf.set_params(device="cuda")


def _model():
    return _XGB()


def load_all():
    tids = [f[:-9] for f in os.listdir(FEAT) if f.endswith(".emb2.npy")] if os.path.isdir(FEAT) else []
    songs = []
    for i, tid in enumerate(tids):
        s = bars(tid)
        if s is not None:
            s["tid"] = tid
            songs.append(s)
    return songs


def evaluate():
    songs = load_all()
    pcas = _pca(songs)
    rev = [s for s in songs if (s["y"] >= 0).any()]
    names = sorted({str(s["name"]).lower() for s in rev})
    print(f"{len(rev)} reviewed songs ({len(songs)} with features)", flush=True)
    M = {s["tid"]: matrix(s, pcas) for s in rev}                 # each song's features once (the PCA uses no labels)
    Y, R, P, W = [], [], [], []
    for k, nm in enumerate(names):
        tr = [s for s in rev if str(s["name"]).lower() != nm]
        X = np.concatenate([M[s["tid"]][s["y"] >= 0] for s in tr]); yy = np.concatenate([s["y"][s["y"] >= 0] for s in tr])
        clf = _model().fit(X, yy)
        if k % 10 == 0:
            print(f"  {k}/{len(names)} songs scored", flush=True)
        for s in (s for s in rev if str(s["name"]).lower() == nm):
            m = s["y"] >= 0
            pr = np.zeros((len(s["y"]), 6)); pr[:, clf.classes_] = clf.predict_proba(M[s["tid"]])
            Y.append(s["y"][m]); R.append(s["rule"][m]); P.append(pr.argmax(1)[m]); W.append((s["spans"][:, 1] - s["spans"][:, 0])[m])
    added = {k: v.get("added", "?") for k, v in truth().items()}
    G = np.concatenate([[added.get(s["tid"], "?")] * int((s["y"] >= 0).sum()) for nm in names for s in rev if str(s["name"]).lower() == nm])
    y, r, p, w = map(np.concatenate, (Y, R, P, W))
    def acc(q, m=None):
        m = np.ones(len(y), bool) if m is None else m
        return f"types {np.average((q == y)[m], weights=w[m]):.1%}  drop/not-drop {np.average(((q == 3) == (y == 3))[m], weights=w[m]):.1%}"
    print("all reviewed songs")
    print("  the rules:   ", acc(r))
    print("  the labeler: ", acc(p))
    for day in sorted(set(G)):                                  # songs reviewed after the rules were last tuned are the fair test
        m = G == day
        n = len({str(s["name"]).lower() for s in rev if added.get(s["tid"]) == day})
        print(f"reviewed {day} ({n} songs)")
        print("  the rules:   ", acc(r, m))
        print("  the labeler: ", acc(p, m))


def train():
    import joblib
    songs = load_all()
    pcas = _pca(songs)
    rev = [s for s in songs if (s["y"] >= 0).any()]
    X = np.concatenate([matrix(s, pcas)[s["y"] >= 0] for s in rev]); yy = np.concatenate([s["y"][s["y"] >= 0] for s in rev])
    clf = _model().fit(X, yy)
    path = os.path.join(LIB.dir, "labeler.joblib")
    joblib.dump({"model": clf, "pcas": pcas, "types": TYPES, "songs": len(rev)}, path)
    print(f"trained on {len(rev)} songs, {len(yy)} bars: {path}")


def order():
    """Train on every reviewed song, then rank the others by how much of each the labeler and the rules disagree on
    (drop against not-drop counted double): the user's next reviews teach the most there."""
    songs = load_all()
    pcas = _pca(songs)
    rev = [s for s in songs if (s["y"] >= 0).any()]
    X = np.concatenate([matrix(s, pcas)[s["y"] >= 0] for s in rev]); yy = np.concatenate([s["y"][s["y"] >= 0] for s in rev])
    clf = _model().fit(X, yy)
    doubt = {}
    for s in songs:
        if (s["y"] >= 0).any():
            continue
        p = clf.classes_[clf.predict_proba(matrix(s, pcas)).argmax(1)]
        w = s["spans"][:, 1] - s["spans"][:, 0]
        types = float(np.average(p != s["rule"], weights=w))
        drops = float(np.average((p == 3) != (s["rule"] == 3), weights=w))
        doubt[s["tid"]] = round(0.5 * types + drops, 4)
    path = os.path.join(LIB.dir, "review_order.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"_about": "how much the learned labeler and the rules disagree on each unreviewed song (review the high ones first)",
                   **dict(sorted(doubt.items(), key=lambda kv: -kv[1]))}, fh, indent=1)
    names = {s["tid"]: str(s["name"]) for s in songs}
    print(f"{len(doubt)} songs ranked: {path}")
    for tid, d in sorted(doubt.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {d:.2f}  {names[tid]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if "--fresh" in sys.argv and os.path.isdir(CACHE):
        for f in os.listdir(CACHE):
            os.remove(os.path.join(CACHE, f))
    {"eval": evaluate, "train": train, "order": order}[cmd]()
