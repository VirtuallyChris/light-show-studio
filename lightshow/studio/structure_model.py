"""SongFormer's part in finding sections: it decides exactly when each one starts.

Two detectors, each good at one thing, measured against the user's own
corrections:

  * the loudness detector (analysis.find_structure) is good at *what* a part is:
    drop, breakdown, buildup. 93-98% of each song labelled as the user would.
  * SongFormer (ASLP Lab) is good at *where* a part starts, to a tenth of a
    second, but it thinks in pop terms (verse, chorus, bridge) and its labels
    translate poorly: 65-87%.

So the loudness detector's sections are kept, and where a buildup or a drop
starts, the boundary moves to SongFormer's nearest one if it is within a few
seconds, then onto the bar line. Only those: the loudness detector's timing
errors are all at lifts, where the level barely changes, while at a quiet
breakdown or intro the drop in level is an exact signal of its own (snapping
those too moved a correct breakdown 3.8 s early). On the user's corrected songs that gave 96-98% labels with the drops
exactly where the user put them. An earlier version did the reverse (SongFormer's
labels, loudness timing) and placed drops a couple of bars early.
"""

from __future__ import annotations

import json
import os

from .model_worker import ModelWorker

CACHE_SUFFIX = ".songformer.json"
WORKER = ModelWorker("lightshow.studio.songformer_infer", "segments")

def available() -> bool:
    return WORKER.available()


def run_model(audio_path: str, cache_dir: str | None = None) -> tuple[list[dict] | None, str]:
    """SongFormer's segments for a file, and why not if there are none. Cached next to the analysis."""
    audio_path = os.path.abspath(audio_path)
    cache = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        st = os.stat(audio_path)
        cache = os.path.join(cache_dir, f"{os.path.basename(audio_path)}.{st.st_size}.{int(st.st_mtime)}{CACHE_SUFFIX}")
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as fh:
                segs = json.load(fh).get("segments")
            if segs:
                return segs, ""
    if not available():
        return None, "SongFormer is not installed (the model environment is missing)"
    segs = WORKER.ask_file(audio_path)
    if not segs:
        return None, f"SongFormer failed: {WORKER.last_error or 'no answer'}"
    if cache:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"segments": segs}, fh)
    return segs, ""


FEATURES = ModelWorker("lightshow.studio.songformer_infer", "features")   # (the same SongFormer, already loaded)


def save_features(audio_path: str, out_dir: str, name: str) -> dict | None:
    """SongFormer's embeddings and label heads for a song file, written to out_dir as <name>.emb2.npy / .npz /
    .json, for the learned section labeler (labeler.py). None when the model environment cannot run."""
    if not available():
        return None
    return FEATURES._ask({"path": os.path.abspath(audio_path), "features_out": os.path.abspath(out_dir), "name": name})


def run_audio(y, sr: int) -> list[dict] | None:
    """SongFormer on audio held in memory (Jam mode). Nothing is written to disk."""
    return WORKER.ask_audio(y, sr) if available() else None


REACH_S = 4.5          # how far a boundary may move to meet SongFormer's


def time_with_songformer(sections: list[dict], drops: list[float], segments: list[dict] | None,
                         beats: list[float], phase: int, duration: float, reach: float = REACH_S) -> tuple[list[dict], list[float], int]:
    """Move each buildup and drop start to SongFormer's nearest boundary, then onto the bar line.

    Returns the sections, the drop markers (a marker on a boundary moves with it)
    and how many boundaries moved. Labels are never changed here.
    """
    import bisect

    heard = sorted(float(s["start"]) for s in (segments or [])[1:])
    bars = [beats[i] for i in range(phase, len(beats), 4)]
    if not heard or len(bars) < 2 or len(sections) < 2:
        return sections, drops, 0

    def to_bar(t: float) -> float:
        i = bisect.bisect_left(bars, t)
        return min((bars[j] for j in (i - 1, i) if 0 <= j < len(bars)), key=lambda b: abs(b - t))

    out = [dict(s) for s in sections]
    moved_from, moved = {}, 0
    for i in range(1, len(out)):
        if out[i]["type"] not in ("buildup", "drop"):
            continue
        t = out[i]["start"]
        near = min(heard, key=lambda b: abs(b - t))
        if abs(near - t) > reach:
            continue
        target = round(to_bar(near), 3)
        # never cross or swallow a neighbour: at least a second either side
        nxt = sections[i + 1]["start"] if i + 1 < len(sections) else duration
        if target <= out[i - 1]["start"] + 1.0 or target >= nxt - 1.0 or abs(target - t) < 1e-3:
            continue
        moved_from[round(t, 3)] = target
        out[i]["start"] = target
        out[i - 1]["end"] = target
        moved += 1
    new_drops = sorted({moved_from.get(round(d, 3), d) for d in drops})
    return out, new_drops, moved
