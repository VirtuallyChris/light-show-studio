r"""Claude reviews songs by itself: the songs nobody has reviewed, the most doubted first, read bar by bar (stem
levels, drum hits, SongFormer, the rules, the labeler, the words) and given sections in the user's style.

    .venv\Scripts\python tools\claude_review.py prep 10 batch.json batch.txt    the next 10 songs, as text to read
    .venv\Scripts\python tools\claude_review.py keep batch.json labels.json     the sections given, kept as reviews
    .venv\Scripts\python tools\claude_review.py propose batch.json labels.json  or as suggestions the user approves (M, Done)

labels.json: {"<n>": [[first bar, "type"], ...]} for each song n of the batch (a run of bars per section). The
reviews are kept marked by="claude" (review.py): the labeler learns from them at CLAUDE_WEIGHT, never scores against
them, and the user's own review of a song replaces Claude's. After keeping, the labeler is trained again.
"""

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lightshow.studio import labeler as L  # noqa: E402
from lightshow.studio.jam import JamLibrary  # noqa: E402
from lightshow.studio.review import Review  # noqa: E402


GUIDE = 2                     # the labeling guide's version: redo picks Claude's reviews made under an older one
GUIDES = os.path.join(ROOT, "shows", "_jam", "claude_guide.json")     # track id -> the guide its review followed
TRIALS = os.path.join(ROOT, "shows", "_jam", "features", "trials", "nn_data.npz")


def _guides() -> dict:
    try:
        with open(GUIDES, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def prep(n: int, batch_path: str, text_path: str, redo: bool = False) -> None:
    """The next n songs nobody has reviewed, the most doubted first; redo: Claude's own reviews made under an older
    guide come after them, to be labeled again."""
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    m = L._load()
    if not m:
        raise SystemExit("no labeler trained yet")
    truth = L.truth(lib.dir)
    guides = _guides()
    mine = {k for k, v in truth.items() if isinstance(v, dict) and v.get("by") == "claude"}
    redoable = {k for k in mine if guides.get(k, 1) < GUIDE} if redo else set()
    reviewed = set(truth) - redoable
    try:
        with open(os.path.join(lib.dir, "review_order.json"), encoding="utf-8") as fh:
            order = [k for k in json.load(fh) if not k.startswith("_")]
    except (OSError, ValueError):
        order = []
    order += sorted(redoable - set(order))
    review = Review(lib)
    playable = {s["id"] for s in review.every_song()}                 # a kept copy, one of each twin
    picks = []
    for tid in order:
        if tid in reviewed or tid not in playable or any(t in reviewed for t, _ in lib.twins(tid)):
            continue
        s = L.song_bars(lib, tid)
        if s is None:
            continue
        picks.append((s, m["model"].proba(L.matrix(s, m["pcas"]))))
        if len(picks) >= n:
            break
    _write(lib, review, picks, batch_path, text_path)


def blind(n: int, seed: int, batch_path: str, text_path: str) -> None:
    """n of the user's own reviewed songs, to be labeled without seeing the user's answer: the labeler column is the
    trees trained without that song (the trials' folds, nn_export.py), so nothing in the table knows the answer."""
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    z = np.load(TRIALS)
    oof = {str(z[f"id{i}"]): z[f"T{i}"] for i in range(int(z["n"])) if not int(z[f"c{i}"])}
    truth = L.truth(lib.dir)
    ids = sorted(k for k in oof if isinstance(truth.get(k), dict) and truth[k].get("by") != "claude")
    review = Review(lib)
    picks = []
    for tid in np.random.default_rng(seed).permutation(ids):
        s = L.song_bars(lib, str(tid))
        if s is not None and len(oof[str(tid)]) == len(s["spans"]):
            picks.append((s, oof[str(tid)]))
        if len(picks) >= n:
            break
    _write(lib, review, picks, batch_path, text_path)


def _write(lib, review, picks, batch_path: str, text_path: str) -> None:
    T, lines, batch = L.TYPES, [], {}
    for k, (s, P) in enumerate(picks, 1):
        sm = L.smooth(P)
        b = s["base"]
        loud = np.quantile(b[:, 0], 0.9) + 1e-6                          # the song's loud level: its drops sit near it
        lyl = (lib.lyrics(s["tid"]) or {}).get("lines") or []
        a = (lib.learned(s["tid"]) or {}).get("analysis") or {}
        t = review._head(s["tid"]) or {}
        label = f"{', '.join(t.get('artists') or [])} - {t.get('name')}"
        lines.append(f"\n=== SONG {k}: {label}  ({len(s['spans'])} bars, {float(a.get('duration') or s['spans'][-1][1]):.0f} s, "
                     f"bpm {a.get('bpm', 0):.0f}, genre {(a.get('genre') or {}).get('genres')}) ===")
        lines.append("bar  time  | energy loud% bass drums voc other | kick snare | songformer | rules     | labeler (smoothed; top probs) | words")
        for i, (b0, b1) in enumerate(s["spans"]):
            lv = b[i, 0:6]
            sf = L.SF[int(np.argmax(b[i, 30:38]))] if b[i, 30:38].sum() > 0 else "-"
            top = np.argsort(-P[i])[:2]
            words = " / ".join(x["text"] for x in lyl if b0 <= x["t"] < b1)[:60]
            lines.append(f"{i:3d} {int(b0 // 60)}:{b0 % 60:04.1f} | {lv[0]:.2f} {min(999, 100 * lv[0] / loud):4.0f} {lv[1]:.2f} {lv[3]:.2f} "
                         f"{lv[4]:.2f} {lv[5]:.2f} | {int(b[i, 52]):2d} {int(b[i, 51]):2d} | {sf:10s} | {T[int(s['rule'][i])]:9s} | "
                         f"{T[sm[i]]:9s} {T[top[0]][:5]} {P[i, top[0]]:.2f} {T[top[1]][:5]} {P[i, top[1]]:.2f} | {words}")
        batch[str(k)] = {"tid": s["tid"], "label": label, "spans": s["spans"].tolist(),
                         "duration": float(a.get("duration") or s["spans"][-1][1])}
    with open(batch_path, "w", encoding="utf-8") as fh:
        json.dump(batch, fh)
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"{len(picks)} songs: " + "; ".join(v["label"] for v in batch.values()))


def _bars(song: dict, runs: list) -> np.ndarray:
    """A run list ([[first bar, type], ...]) as a type index per bar."""
    out = np.zeros(len(song["spans"]), int)
    for (start, kind), nxt in zip(runs, runs[1:] + [[len(song["spans"]), None]]):
        out[start:nxt[0]] = L.TYPES.index(kind)
    return out


def _sections(song: dict, runs: list) -> list:
    """A run list as sections on the song's bars."""
    spans, secs = song["spans"], []
    for (start, kind), nxt in zip(runs, runs[1:] + [[len(spans), None]]):
        t0 = 0.0 if start == 0 else float(spans[start][0])
        t1 = song["duration"] if nxt[0] >= len(spans) else float(spans[nxt[0]][0])
        if t1 > t0:
            secs.append({"type": kind, "start": t0, "end": t1})
    return secs


def score(batch_path: str, labels_path: str) -> None:
    """A blind batch's labels against the user's own, bar by bar (weighted by bar length), beside the trees'."""
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    z = np.load(TRIALS)
    oof = {str(z[f"id{i}"]): z[f"T{i}"] for i in range(int(z["n"]))}
    batch = json.load(open(batch_path, encoding="utf-8"))
    labels = json.load(open(labels_path, encoding="utf-8"))
    Y, C, Tr, W = [], [], [], []
    for k, song in batch.items():
        if k not in labels:
            continue
        s = L.song_bars(lib, song["tid"])
        y, m = s["y"], s["y"] >= 0
        c, tr, w = _bars(song, labels[k]), L.smooth(oof[song["tid"]]), s["spans"][:, 1] - s["spans"][:, 0]
        print(f"  {song['label'][:50]:50s} Claude {np.average((c == y)[m], weights=w[m]):.0%}   trees {np.average((tr == y)[m], weights=w[m]):.0%}")
        Y.append(y[m]); C.append(c[m]); Tr.append(tr[m]); W.append(w[m])
    y, c, tr, w = map(np.concatenate, (Y, C, Tr, W))
    for who, q in (("Claude", c), ("the trees", tr)):
        print(f"{who:10s} types {np.average(q == y, weights=w):.1%}  drop/not-drop {np.average((q == 3) == (y == 3), weights=w):.1%}")


def keep(batch_path: str, labels_path: str) -> None:
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    batch = json.load(open(batch_path, encoding="utf-8"))
    labels = json.load(open(labels_path, encoding="utf-8"))
    review = Review(lib)
    guides = _guides()
    L.retrain_soon = lambda *a, **k: False                            # (trained once, below, not once a song)
    done = 0
    for k, song in batch.items():
        runs = labels.get(k)
        if not runs:
            continue
        review.keep(song["tid"], _sections(song, runs), ok=False, by="claude")
        guides[song["tid"]] = GUIDE
        done += 1
    with open(GUIDES + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(guides, fh)
    os.replace(GUIDES + ".tmp", GUIDES)
    print(f"kept {done} reviews by Claude")
    L.train(lib)


def propose(batch_path: str, labels_path: str) -> None:
    """The sections given, as suggestions for the user to judge in review mode (review.py proposals), not reviews."""
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    batch = json.load(open(batch_path, encoding="utf-8"))
    labels = json.load(open(labels_path, encoding="utf-8"))
    review, done = Review(lib), 0
    for k, song in batch.items():
        runs = labels.get(k)
        if not runs:
            continue
        review.propose(song["tid"], _sections(song, runs))
        done += 1
    print(f"suggested sections for {done} songs (review mode shows them first)")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prep":
        prep(int(sys.argv[2]), sys.argv[3], sys.argv[4], redo="--redo" in sys.argv)
    elif cmd == "blind":
        blind(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])
    elif cmd == "score":
        score(sys.argv[2], sys.argv[3])
    elif cmd == "keep":
        keep(sys.argv[2], sys.argv[3])
    elif cmd == "propose":
        propose(sys.argv[2], sys.argv[3])
