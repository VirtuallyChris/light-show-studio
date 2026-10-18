"""SongFormer (ASLP-lab) worker: where a song's sections are and what they are.

Run by models_host.py (model "songformer_infer"), inside the model environment (Python 3.12, CUDA
PyTorch); the studio never imports it. The request and the reply, as the host passes them:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                  a file on disk (always a full path)
               {"samples": N, "sr": 24000}                  followed by N float32 samples
                                                            (audio from memory: Jam mode never
                                                            writes captured audio to disk)
    reply    one JSON line
               {"segments": [{"start": 0.0, "end": 15.2, "label": "verse"}, ...], "seconds": 4.1}
               or {"error": "..."}

"""

from __future__ import annotations

import os
import sys

REPO = "ASLP-lab/SongFormer"
SR = 24000


def load_model(device: str = "auto"):
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModel

    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    local_dir = snapshot_download(repo_id=REPO, repo_type="model", ignore_patterns=["SongFormer.pt", "SongFormer.safetensors"])
    # The remote code imports its sibling files as top-level modules (model, musicfm, ...)
    # and finds its config through the working directory, so the working directory moves.
    # That is why every path handed to this worker must be a full path.
    sys.path.insert(0, local_dir)
    os.chdir(local_dir)
    os.environ["SONGFORMER_LOCAL_DIR"] = local_dir
    # model.py imports `msaf` (evaluation metrics only, never used at inference); msaf's
    # old dependency pins fight modern numpy/librosa, so satisfy the import with a stub
    stubs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_stubs")
    msaf_dir = os.path.join(stubs, "msaf")
    os.makedirs(msaf_dir, exist_ok=True)
    stub_files = {
        "__init__.py": "# stub: evaluation-only dependency of SongFormer, not needed for inference\n",
        "eval.py": "def compute_results(*a, **k):\n    raise NotImplementedError('msaf stub: evaluation is not available')\n",
    }
    for fn, body in stub_files.items():
        fp = os.path.join(msaf_dir, fn)
        if not os.path.exists(fp):
            with open(fp, "w", encoding="utf-8") as fh:
                fh.write(body)
    sys.path.append(stubs)
    model = AutoModel.from_pretrained(local_dir, trust_remote_code=True)
    model.to(device).eval()
    return model, device


def segments(model, y) -> list[dict]:
    import numpy as np
    import torch

    with torch.no_grad():
        result = model(np.asarray(y, dtype=np.float32))
    return [{"start": float(s["start"]), "end": float(s["end"]), "label": str(s["label"])} for s in result]
