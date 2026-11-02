r"""The learned section labeler (lightshow/studio/labeler.py), by hand:

    .venv\Scripts\python tools\section_labeler.py eval      the rules against the labeler, leaving one song out at a time
    .venv\Scripts\python tools\section_labeler.py train     fit it on every reviewed song (shows/_party/labeler.joblib) and
                                                          rank the songs to review next (review_order.json)
    .venv\Scripts\python tools\section_labeler.py export    this home's trained labeler becomes the one the app ships
                                                          (lightshow/studio/data/section_labeler, for everyone)

The studio retrains it by itself as songs are reviewed (review.py). SongFormer's features for the songs learned
before they were saved as each song is learned: .venv-songformer\Scripts\python tools\songformer_features.py
After the rules change, add --fresh (the per-bar features cache the rules' labels).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lightshow.appdirs import data_dir  # noqa: E402
from lightshow.studio import labeler  # noqa: E402
from lightshow.studio.party import PartyLibrary  # noqa: E402

if __name__ == "__main__":
    lib = PartyLibrary(os.path.join(data_dir(), "shows"))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if "--fresh" in sys.argv and os.path.isdir(labeler.CACHE):
        for f in os.listdir(labeler.CACHE):
            os.remove(os.path.join(labeler.CACHE, f))
    if cmd == "export":
        import joblib
        from lightshow.studio import labeler_bundle
        print(labeler_bundle.export(joblib.load(labeler.MODEL)))
    else:
        {"eval": labeler.evaluate, "train": labeler.train}[cmd](lib)
