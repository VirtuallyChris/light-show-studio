r"""The learned section labeler (lightshow/studio/labeler.py), by hand:

    .venv\Scripts\python tools\section_labeler.py eval      the rules against the labeler, leaving one song out at a time
    .venv\Scripts\python tools\section_labeler.py train     fit it on every reviewed song (shows/_jam/labeler.joblib) and
                                                          rank the songs to review next (review_order.json)

The studio retrains it by itself as songs are reviewed (review.py). SongFormer's features for the songs learned
before they were saved as each song is learned: .venv-songformer\Scripts\python tools\songformer_features.py
After the rules change, add --fresh (the per-bar features cache the rules' labels).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lightshow.studio import labeler  # noqa: E402
from lightshow.studio.jam import JamLibrary  # noqa: E402

if __name__ == "__main__":
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if "--fresh" in sys.argv and os.path.isdir(labeler.CACHE):
        for f in os.listdir(labeler.CACHE):
            os.remove(os.path.join(labeler.CACHE, f))
    {"eval": labeler.evaluate, "train": labeler.train}[cmd](lib)
