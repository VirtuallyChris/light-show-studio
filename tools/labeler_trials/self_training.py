r"""Self-training trial: can the labeler teach itself from the songs nobody reviewed?

Each round the model labels every unreviewed song (smoothed into sections), keeps the bars it is surest of as
"pseudo-labels" (less weight than the user's), and is trained again on the user's labels plus those. Scored only on
the user's labels, 10 folds by song: the held-out songs are never seen, neither are their twins, and their
pseudo-labels come from models trained without them.

    .venv\Scripts\python tools\labeler_trials\self_training.py
"""

import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from lightshow.studio import labeler as L  # noqa: E402
from lightshow.studio.party import PartyLibrary  # noqa: E402

ROUNDS = int(os.environ.get("ROUNDS", 3))
CONFIGS = [  # (min confidence to keep a pseudo-labeled bar, weight of a pseudo bar against a user's bar)
    (0.7, 0.3), (0.7, 1.0), (0.85, 0.5), (0.5, 0.3),
]


def fit(X, y, w):
    from sklearn.utils.class_weight import compute_sample_weight
    from xgboost import XGBClassifier
    classes = np.unique(y)
    remap = {c: i for i, c in enumerate(classes)}
    clf = XGBClassifier(n_estimators=300, learning_rate=0.05, max_depth=6, subsample=0.9, colsample_bytree=0.6,
                        reg_lambda=1.0, tree_method="hist", device="cuda", n_jobs=8, random_state=0)
    clf.fit(X, np.array([remap[v] for v in y]), sample_weight=compute_sample_weight("balanced", y) * w)
    clf.set_params(device="cpu")

    def proba(Xq):
        P = np.zeros((len(Xq), len(L.TYPES)))
        P[:, classes] = clf.predict_proba(Xq)
        return P
    return proba


def main():
    t0 = time.time()
    lib = PartyLibrary(os.path.join(ROOT, "shows"))
    songs = L.load_all(lib)
    pcas = L._pca(songs)
    M = {s["tid"]: L.matrix(s, pcas) for s in songs}
    rev = [s for s in songs if (s["y"] >= 0).any()]
    unrev = [s for s in songs if not (s["y"] >= 0).any()]
    names = sorted({str(s["name"]).lower() for s in rev})
    rng = np.random.default_rng(0)
    fold_of = {n: int(k % 10) for k, n in enumerate(rng.permutation(names))}
    added = {k: v.get("added", "?") for k, v in L.truth(lib.dir).items()}
    twins = {s["tid"]: {t for t, _ in lib.twins(s["tid"])} for s in songs}
    print(f"{len(rev)} reviewed, {len(unrev)} unreviewed songs with features; {ROUNDS} rounds, configs {CONFIGS}", flush=True)

    # results[config][round] = list of (y, pred, w, day) per held-out song
    res = {c: {r: [] for r in range(ROUNDS + 1)} for c in CONFIGS}
    for k in range(10):
        held = [s for s in rev if fold_of[str(s["name"]).lower()] == k]
        tr = [s for s in rev if fold_of[str(s["name"]).lower()] != k]
        held_ids = {s["tid"] for s in held}
        held_names = {str(s["name"]).lower() for s in held}
        pool = [s for s in unrev if str(s["name"]).lower() not in held_names and not (twins.get(s["tid"], set()) & held_ids)]
        Xh = np.concatenate([M[s["tid"]][s["y"] >= 0] for s in tr]); yh = np.concatenate([s["y"][s["y"] >= 0] for s in tr])
        base = fit(Xh, yh, np.ones(len(yh)))
        for c in CONFIGS:
            model = base
            for r in range(ROUNDS + 1):
                for s in held:
                    m = s["y"] >= 0
                    res[c][r].append((s["y"][m], L.smooth(model(M[s["tid"]]))[m], (s["spans"][:, 1] - s["spans"][:, 0])[m],
                                      added.get(s["tid"], "?")))
                if r == ROUNDS:
                    break
                tau, wp = c
                Xs, ys, ws = [Xh], [yh], [np.ones(len(yh))]
                for s in pool:
                    P = model(M[s["tid"]])
                    lab = L.smooth(P)
                    conf = P[np.arange(len(lab)), lab]
                    keep = conf >= tau
                    if keep.any():
                        Xs.append(M[s["tid"]][keep]); ys.append(lab[keep]); ws.append(np.full(int(keep.sum()), wp))
                model = fit(np.concatenate(Xs), np.concatenate(ys), np.concatenate(ws))
        print(f"fold {k} done ({time.time() - t0:.0f} s)", flush=True)

    def score(rows, day=None):
        rows = [x for x in rows if day is None or x[3] == day]
        y = np.concatenate([x[0] for x in rows]); p = np.concatenate([x[1] for x in rows]); w = np.concatenate([x[2] for x in rows])
        return np.average(p == y, weights=w), np.average((p == 3) == (y == 3), weights=w)

    out = {}
    print("\n10 folds by song, smoothed into sections (types / drop-not-drop); round 0 = the user's labels only")
    for c in CONFIGS:
        for r in range(ROUNDS + 1):
            a, d = score(res[c][r]); a2, d2 = score(res[c][r], "2026-10-21")
            print(f"  keep >= {c[0]:.2f}, weight {c[1]:.1f}, round {r}:  all {a:.1%} / {d:.1%}   newest day {a2:.1%} / {d2:.1%}")
            out[f"{c[0]}_{c[1]}_{r}"] = [round(a, 4), round(d, 4), round(a2, 4), round(d2, 4)]
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "self_training_results.json"), "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
