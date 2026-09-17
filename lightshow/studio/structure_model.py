"""Optional neural structure analysis (SongFormer) for the studio.

If the side environment `.venv-songformer` exists, `analyze_with_model` runs
SongFormer on the song and converts its pop-style labels (intro, verse,
pre-chorus, chorus, bridge, inst, outro, silence) into the studio's dance
labels (intro, verse, buildup, drop, breakdown, outro), snapped to the
studio's own beat grid. Anything that goes wrong returns None and the
built-in detector is used instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The model environment lives on the local disk (a network share is far too slow for
# PyTorch's thousands of files); a copy inside the project is honoured too.
_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "lanlightshow", "venv-songformer", "Scripts", "python.exe"),
    os.path.join(ROOT, ".venv-songformer", "Scripts", "python.exe"),
]
VENV_PY = next((c for c in _CANDIDATES if os.path.exists(c)), _CANDIDATES[0])
CACHE_SUFFIX = ".songformer.json"

LABEL_MAP = {
    "intro": "intro", "verse": "verse", "pre-chorus": "buildup", "chorus": "drop",
    "bridge": "breakdown", "inst": "verse", "outro": "outro", "silence": "intro",
}


def available() -> bool:
    return os.path.exists(VENV_PY)


def run_model(audio_path: str, cache_dir: str | None = None, timeout: float = 900.0) -> list[dict] | None:
    """Raw SongFormer segments for a file (cached next to the analysis cache)."""
    if not available():
        return None
    cache = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        st = os.stat(audio_path)
        cache = os.path.join(cache_dir, f"{os.path.basename(audio_path)}.{st.st_size}.{int(st.st_mtime)}{CACHE_SUFFIX}")
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh).get("segments")
    try:
        proc = subprocess.run([VENV_PY, "-m", "lightshow.studio.songformer_infer", audio_path], cwd=ROOT,
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        data = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {"error": proc.stderr[-800:]}
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        print(f"[songformer] failed: {e}", file=sys.stderr)
        return None
    if "error" in data:
        print(f"[songformer] {data['error']}", file=sys.stderr)
        return None
    if cache:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    return data.get("segments")


def sections_from_model(segments: list[dict], analysis: dict, builtin_drops: list[float] | None = None,
                        builtin_sections: list[dict] | None = None) -> tuple[list[dict], list[float]] | None:
    """Map model segments onto the studio's beat grid and label scheme, merged with
    the built-in detector's drop instants.

    The model is good at *what* a section is; the energy detector is good at the
    exact bar the bass slams in. So: model boundaries and labels, plus every
    built-in drop instant as an extra boundary. Inside a chorus, the part before
    the bass drop becomes the buildup (or a breakdown if it is quiet); a built-in
    drop inside a non-chorus segment that is loud from there on becomes a drop too.
    """
    if not segments:
        return None
    beats = analysis["beats"]
    phase = analysis.get("downbeat_phase", 0)
    duration = analysis["duration"]
    energy = analysis["energy"]
    fps = analysis["feature_fps"]
    import bisect
    import numpy as np

    bars = [beats[i] for i in range(phase, len(beats), 4)]
    n_bars = len(bars)
    if n_bars < 4:
        return None

    def snap_bar(t: float) -> int:
        i = bisect.bisect_left(bars, t)
        cands = [j for j in (i - 1, i) if 0 <= j < n_bars]
        return min(cands, key=lambda j: abs(bars[j] - t))

    def bar_time(b: int) -> float:
        return bars[b] if b < n_bars else duration

    def loud(b0: int, b1: int) -> float:
        i0, i1 = int(bar_time(b0) * fps), max(int(bar_time(b0) * fps) + 1, int(bar_time(b1) * fps))
        seg = energy[i0:i1]
        return float(np.mean(seg)) if len(seg) else 0.0

    # model segments on the bar grid
    model = []
    for sg in segments:
        b0, b1 = snap_bar(sg["start"]), snap_bar(sg["end"])
        if b1 <= b0:
            continue
        model.append([b0, b1, sg["label"]])
    if not model:
        return None
    model[0][0] = 0
    for k in range(1, len(model)):
        model[k][0] = model[k - 1][1]
    model[-1][1] = n_bars

    # extra boundaries: the built-in drop instants (bar-snapped), only where they are loud
    peak = max(loud(b0, b1) for b0, b1, _ in model) or 1.0
    drop_bars = set()
    for d in builtin_drops or []:
        b = snap_bar(d)
        if 0 < b < n_bars - 1 and loud(b, min(n_bars, b + 4)) / peak >= 0.7:
            drop_bars.add(b)

    pieces = []   # [b0, b1, label, model_label, is_drop_start]
    for b0, b1, ml in model:
        cuts = sorted({b0} | {b for b in drop_bars if b0 < b < b1} | {b1})
        for c0, c1 in zip(cuts[:-1], cuts[1:]):
            pieces.append([c0, c1, LABEL_MAP.get(ml, "verse"), ml, c0 in drop_bars])

    # label rules ------------------------------------------------------------------
    def bar_loud(b):
        return loud(b, b + 1) / peak

    def builtin_label(b0, b1):
        """majority built-in label over a bar range (for long chorus lead-ins)"""
        if not builtin_sections:
            return "verse"
        t0, t1 = bar_time(b0), bar_time(b1)
        best, best_ov = "verse", 0.0
        for sg in builtin_sections:
            ov = max(0.0, min(t1, sg["end"]) - max(t0, sg["start"]))
            if ov > best_ov:
                best, best_ov = sg["type"], ov
        return best if best in ("verse", "buildup", "breakdown") else "verse"

    # consecutive chorus pieces are one chorus: find where its bass actually drops
    k = 0
    while k < len(pieces):
        if pieces[k][3] != "chorus":
            k += 1
            continue
        j = k
        while j + 1 < len(pieces) and pieces[j + 1][3] == "chorus":
            j += 1
        run = pieces[k:j + 1]
        r0, r1 = run[0][0], run[-1][1]
        # the bass-drop bar: the first built-in drop inside the chorus, else the first bar in
        # the opening 16 where the level steps up clearly, else the chorus start itself
        cut = next((pc[0] for pc in run if pc[4] and pc[0] > r0), None)
        from_detector = cut is not None
        if cut is None:
            for bb in range(r0 + 1, min(r1, r0 + 17)):
                before = np.mean([bar_loud(x) for x in range(r0, bb)])
                after = np.mean([bar_loud(x) for x in range(bb, min(r1, bb + 4))])
                if after - before >= 0.25 and after >= 0.7:
                    cut = bb
                    break
        lead_len = (cut - r0) if cut else 0
        lead_loud = np.mean([bar_loud(x) for x in range(r0, cut)]) if cut else 1.0
        after_loud = np.mean([bar_loud(x) for x in range(cut, min(r1, cut + 8))]) if cut else 1.0
        # a detector drop already proved a bass slam, so a modest step in level is enough; an
        # energy-found cut must be clearly louder after. Long lead-ins (> 16 bars) are handed to
        # the built-in detector's labels bar by bar (they are usually breakdown/verse/buildup).
        # a detector cut more than 8 bars into a chorus means the chorus already dropped and the
        # bass merely re-enters later: the bars before it keep the built-in detector's labels
        opening = np.mean([bar_loud(x) for x in range(r0, r0 + max(1, lead_len // 2))]) if cut else 1.0
        long_leadin = cut is not None and from_detector and lead_len > 8 and opening < 0.72   # a subdued opening: not the drop yet
        if os.environ.get("SF_DEBUG") and cut is not None:
            print(f"[merge] chorus {bar_time(r0):.1f}-{bar_time(r1):.1f}: cut {bar_time(cut):.1f} from_detector={from_detector} lead_len={lead_len} opening={opening:.2f} lead={lead_loud:.2f} after={after_loud:.2f}", file=sys.stderr)
        real_leadin = cut is not None and lead_len <= (8 if from_detector else 16) and (lead_loud <= after_loud - (0.08 if from_detector else 0.2))
        if real_leadin or long_leadin:
            # split at the cut (and, for long lead-ins, at the built-in detector's section edges)
            cuts = {cut}
            if long_leadin and builtin_sections:
                for sg in builtin_sections:
                    bb = snap_bar(sg["start"])
                    if r0 < bb < cut:
                        cuts.add(bb)
            for c in sorted(cuts):
                for idx in range(k, j + 1):
                    b0, b1, lab, ml, at = pieces[idx]
                    if b0 < c < b1:
                        pieces[idx] = [b0, c, lab, ml, at]
                        pieces.insert(idx + 1, [c, b1, lab, ml, c == cut])
                        j += 1
                        break
            run = pieces[k:j + 1]
        for pc in run:
            if long_leadin and pc[0] < cut:
                pc[2] = builtin_label(pc[0], pc[1])
                pc[4] = False
            elif real_leadin and pc[0] < cut:
                pc[2] = "breakdown" if lead_loud < 0.45 else "buildup"
                pc[4] = False
            else:
                pc[2] = "drop" if loud(pc[0], pc[1]) / peak >= 0.5 else "verse"
                pc[4] = bool((real_leadin or long_leadin) and pc[0] == cut)
        k = j + 1
    for kk, (b0, b1, lab, ml, at_drop) in enumerate(pieces):
        rel = loud(b0, b1) / peak
        if ml == "chorus":
            continue
        if at_drop and rel >= 0.75:
            pieces[kk][2] = "drop"            # the detector heard a drop the model did not label
        elif ml in ("bridge", "inst"):
            pieces[kk][2] = "breakdown" if rel < 0.55 else "verse"
    # merge same-label non-drop neighbours first so a verse split by the model is carved as one
    tmp = []
    for pc in pieces:
        if tmp and tmp[-1][2] == pc[2] and pc[2] != "drop":
            tmp[-1][1] = pc[1]
        else:
            tmp.append(pc)
    pieces = tmp
    # buildup: carve up to 8 bars off a plain verse that leads straight into a drop
    k = 0
    while k < len(pieces) - 1:
        b0, b1, lab, ml, _ = pieces[k]
        if pieces[k + 1][2] == "drop" and lab == "verse" and b1 - b0 > 8:
            pieces[k][1] = b1 - 8
            pieces.insert(k + 1, [b1 - 8, b1, "buildup", "carved", False])
            k += 1
        k += 1
    # merge same-label neighbours (drops only merge when the second is not a fresh bass drop)
    merged = []
    for b0, b1, lab, ml, at_drop in pieces:
        if merged and merged[-1][2] == lab and not (lab == "drop" and at_drop):
            merged[-1][1] = b1
        else:
            merged.append([b0, b1, lab, ml, at_drop])

    if len(merged) > 1 and merged[-1][3] == "silence":
        merged[-1][2] = "outro"
    secs, drops = [], []
    for k, (b0, b1, lab, ml, at_drop) in enumerate(merged):
        start = 0.0 if k == 0 else round(bar_time(b0), 3)
        secs.append({"start": start, "end": round(bar_time(b1), 3), "type": lab, "energy": round(loud(b0, b1) / peak, 3),
                     "bars": b1 - b0, "model": ml})
        if lab == "drop":
            drops.append(start)
    return secs, drops
