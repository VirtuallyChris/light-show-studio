r"""Claude reviews songs by itself: the songs nobody has reviewed, the most doubted first, read bar by bar (stem
levels, drum hits, SongFormer, the rules, the labeler, the words) and given sections in the user's style.

    .venv\Scripts\python tools\claude_review.py prep 10 batch.json batch.txt    the next 10 songs, as text to read
    .venv\Scripts\python tools\claude_review.py keep batch.json labels.json     the sections given, kept as reviews

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


def prep(n: int, batch_path: str, text_path: str) -> None:
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    m = L._load()
    if not m:
        raise SystemExit("no labeler trained yet")
    reviewed = set(L.truth(lib.dir))
    try:
        with open(os.path.join(lib.dir, "review_order.json"), encoding="utf-8") as fh:
            order = [k for k in json.load(fh) if not k.startswith("_")]
    except (OSError, ValueError):
        order = []
    review = Review(lib)
    playable = {s["id"] for s in review.every_song()}                 # a kept copy, one of each twin
    picks = []
    for tid in order:
        if tid in reviewed or tid not in playable or any(t in reviewed for t, _ in lib.twins(tid)):
            continue
        s = L.song_bars(lib, tid)
        if s is None:
            continue
        picks.append(s)
        if len(picks) >= n:
            break
    T, lines, batch = L.TYPES, [], {}
    for k, s in enumerate(picks, 1):
        P = m["model"].proba(L.matrix(s, m["pcas"]))
        sm = L.smooth(P)
        b = s["base"]
        lyl = (lib.lyrics(s["tid"]) or {}).get("lines") or []
        a = (lib.learned(s["tid"]) or {}).get("analysis") or {}
        t = review._head(s["tid"]) or {}
        label = f"{', '.join(t.get('artists') or [])} - {t.get('name')}"
        lines.append(f"\n=== SONG {k}: {label}  ({len(s['spans'])} bars, {float(a.get('duration') or s['spans'][-1][1]):.0f} s, "
                     f"bpm {a.get('bpm', 0):.0f}, genre {(a.get('genre') or {}).get('genres')}) ===")
        lines.append("bar  time  | energy bass drums voc other | kick snare | songformer | rules     | labeler (smoothed; top probs) | words")
        for i, (b0, b1) in enumerate(s["spans"]):
            lv = b[i, 0:6]
            sf = L.SF[int(np.argmax(b[i, 30:38]))] if b[i, 30:38].sum() > 0 else "-"
            top = np.argsort(-P[i])[:2]
            words = " / ".join(x["text"] for x in lyl if b0 <= x["t"] < b1)[:60]
            lines.append(f"{i:3d} {int(b0 // 60)}:{b0 % 60:04.1f} | {lv[0]:.2f} {lv[1]:.2f} {lv[3]:.2f} {lv[4]:.2f} {lv[5]:.2f} | "
                         f"{int(b[i, 52]):2d} {int(b[i, 51]):2d} | {sf:10s} | {T[int(s['rule'][i])]:9s} | {T[sm[i]]:9s} "
                         f"{T[top[0]][:5]} {P[i, top[0]]:.2f} {T[top[1]][:5]} {P[i, top[1]]:.2f} | {words}")
        batch[str(k)] = {"tid": s["tid"], "label": label, "spans": s["spans"].tolist(),
                         "duration": float(a.get("duration") or s["spans"][-1][1])}
    with open(batch_path, "w", encoding="utf-8") as fh:
        json.dump(batch, fh)
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"{len(picks)} songs: " + "; ".join(v["label"] for v in batch.values()))


def keep(batch_path: str, labels_path: str) -> None:
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    batch = json.load(open(batch_path, encoding="utf-8"))
    labels = json.load(open(labels_path, encoding="utf-8"))
    review = Review(lib)
    L.retrain_soon = lambda *a, **k: False                            # (trained once, below, not once a song)
    import lightshow.studio.labeler as _lab
    _lab.retrain_soon = L.retrain_soon
    done = 0
    for k, song in batch.items():
        runs = labels.get(k)
        if not runs:
            continue
        spans, secs = song["spans"], []
        for (start, kind), nxt in zip(runs, runs[1:] + [[len(spans), None]]):
            t0 = 0.0 if start == 0 else float(spans[start][0])
            t1 = song["duration"] if nxt[0] >= len(spans) else float(spans[nxt[0]][0])
            if t1 > t0:
                secs.append({"type": kind, "start": t0, "end": t1})
        review.keep(song["tid"], secs, ok=False, by="claude")
        done += 1
    print(f"kept {done} reviews by Claude")
    L.train(lib)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prep":
        prep(int(sys.argv[2]), sys.argv[3], sys.argv[4])
    elif cmd == "keep":
        keep(sys.argv[2], sys.argv[3])
