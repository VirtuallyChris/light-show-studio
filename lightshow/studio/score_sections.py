"""How well the section rules place the drops the user confirmed by ear.

    python -m lightshow.studio.score_sections

The truth is shows/_jam/section_truth.json: for each learned song (by its Spotify id), the drop starts the
user heard ("first_only": only the first drop is known) and when it was added. A drop start counts as right
when the rules put a drop within one bar of it. The rules (analysis.settle_sections) are run on each song's
learned analysis twice: as they are, and without SongFormer's labels (the second opinion), to see what the
vote adds. A rule tuned on a song scores well on it by construction: the songs added after a rule was
written are the fair test of it.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import numpy as np

from .analysis import settle_sections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JAM = os.path.join(ROOT, "shows", "_jam")


def score_song(analysis: dict, truth: dict) -> tuple[int, int, list[float]]:
    """(right, out of, the drop starts the rules give) for one song."""
    bars = analysis.get("downbeats") or []
    bar = float(np.median(np.diff(bars))) if len(bars) > 2 else 240.0 / float(analysis.get("bpm") or 120.0)
    a = copy.deepcopy(analysis)
    settle_sections(a)
    got = [s["start"] for s in a["sections"] if s.get("type") == "drop"]
    want = [float(t) for t in truth["drops"]]
    if truth.get("first_only"):
        return int(bool(got) and abs(got[0] - want[0]) <= bar), 1, got
    return sum(any(abs(g - t) <= bar for g in got) for t in want), len(want), got


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")       # (names like Alexandra Căpitănescu on a Windows console)
    try:
        with open(os.path.join(JAM, "section_truth.json"), encoding="utf-8") as fh:
            truth = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except (OSError, ValueError) as e:
        print(f"no truth to score against ({e})")
        return 1
    totals = {"rules": [0, 0], "without SongFormer's vote": [0, 0]}
    for tid, t in truth.items():
        try:
            with open(os.path.join(JAM, "learned", tid + ".json"), encoding="utf-8") as fh:
                a = json.load(fh)["analysis"]
        except (OSError, ValueError, KeyError):
            print(f"{t.get('name', tid)}: not learned")
            continue
        right, n, got = score_song(a, t)
        bare = {k: v for k, v in a.items() if k != "songformer"}
        right2, _, got2 = score_song(bare, t)
        totals["rules"][0] += right; totals["rules"][1] += n
        totals["without SongFormer's vote"][0] += right2; totals["without SongFormer's vote"][1] += n
        print(f"{t.get('name', tid)} (added {t.get('added', '?')}): {right}/{n}  drops at {[round(x, 1) for x in got]}"
              f"{'' if a.get('songformer') else '  (no SongFormer labels yet)'}"
              f"{'' if got2 == got else f'  | without the vote {right2}/{n} {[round(x, 1) for x in got2]}'}")
    for k, (r, n) in totals.items():
        print(f"{k}: {r}/{n} drop starts right")
    return 0


if __name__ == "__main__":
    sys.exit(main())
