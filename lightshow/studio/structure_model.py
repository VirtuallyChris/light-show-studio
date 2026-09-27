"""Where a song's sections are: SongFormer (ASLP-lab), and nothing else.

SongFormer listens to the whole song and labels its parts the way a pop
songwriter would: intro, verse, pre-chorus, chorus, bridge, instrumental,
outro. Those map directly onto the studio's labels, snapped to bar lines:

    pre-chorus -> buildup      chorus -> drop       bridge -> breakdown
    intro -> intro             verse / instrumental -> verse
    outro -> outro             silence -> intro at the start, outro at the end

A drop marker goes where each drop section starts. There is deliberately no
second opinion from loudness: an earlier version refined these with an energy
detector, and it was removed at the user's request. If SongFormer cannot run,
the song gets one plain section and the reason is shown, so the failure is
never silent.
"""

from __future__ import annotations

import bisect
import json
import os

from .model_worker import ROOT, VENV_PY, ModelWorker  # noqa: F401 - VENV_PY/ROOT are imported from here elsewhere

CACHE_SUFFIX = ".songformer.json"
LABEL_MAP = {
    "intro": "intro", "verse": "verse", "pre-chorus": "buildup", "chorus": "drop",
    "bridge": "breakdown", "inst": "verse", "outro": "outro", "silence": "intro",
}
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


def run_audio(y, sr: int) -> list[dict] | None:
    """SongFormer on audio held in memory (Jam mode). Nothing is written to disk."""
    return WORKER.ask_audio(y, sr) if available() else None


def sections_from_segments(segments: list[dict], beats: list[float], phase: int, duration: float) -> tuple[list[dict], list[float]]:
    """SongFormer's segments as studio sections on the bar grid, plus a drop marker at each drop."""
    bars = [beats[i] for i in range(phase, len(beats), 4)]
    n_bars = len(bars)
    if n_bars < 2 or not segments:
        return [{"start": 0.0, "end": duration, "type": "verse", "bars": n_bars}], []

    def snap(t: float) -> int:
        i = bisect.bisect_left(bars, t)
        return min((j for j in (i - 1, i) if 0 <= j < n_bars), key=lambda j: abs(bars[j] - t))

    def at(b: int) -> float:
        return bars[b] if b < n_bars else duration

    pieces = []                                          # [first bar, end bar, label, SongFormer label]
    for k, sg in enumerate(segments):
        label = LABEL_MAP.get(sg["label"], "verse")
        if sg["label"] == "silence" and k == len(segments) - 1 and k > 0:
            label = "outro"
        b0, b1 = snap(sg["start"]), snap(sg["end"])
        if b1 <= b0:
            continue
        if pieces and pieces[-1][2] == label:            # two choruses in a row are one drop
            pieces[-1][1] = b1
        else:
            pieces.append([b0, b1, label, sg["label"]])
    if not pieces:
        return [{"start": 0.0, "end": duration, "type": "verse", "bars": n_bars}], []
    pieces[0][0] = 0
    for k in range(1, len(pieces)):
        pieces[k][0] = pieces[k - 1][1]
    pieces[-1][1] = n_bars
    pieces = [p for p in pieces if p[1] > p[0]]

    sections, drops = [], []
    for k, (b0, b1, label, raw) in enumerate(pieces):
        start = 0.0 if k == 0 else round(at(b0), 3)
        sections.append({"start": start, "end": round(at(b1), 3), "type": label, "bars": b1 - b0, "model": raw})
        if label == "drop":
            drops.append(start)
    return sections, drops


def boundary_times(segments: list[dict] | None) -> list[float]:
    """Where SongFormer heard one part give way to another (for finding the bar lines)."""
    return [float(s["start"]) for s in (segments or [])[1:]]
