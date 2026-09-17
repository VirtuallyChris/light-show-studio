"""Run SongFormer (ASLP-lab) on one audio file and print its segments as JSON.

This runs inside the separate `.venv-songformer` environment (Python 3.12,
CUDA PyTorch). The studio calls it as a subprocess and never imports it.

    python -m lightshow.studio.songformer_infer <audio file> [--device cuda|cpu]

Output (stdout, one JSON object):
    {"segments": [{"start": 0.0, "end": 15.2, "label": "verse"}, ...], "device": "cuda", "seconds": 4.1}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO = "ASLP-lab/SongFormer"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    # keep every library quiet: only the JSON goes to stdout
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    t0 = time.time()
    try:
        import numpy as np
        import torch
        import librosa
        from huggingface_hub import snapshot_download
        from transformers import AutoModel

        device = args.device
        if device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        local_dir = snapshot_download(repo_id=REPO, repo_type="model", ignore_patterns=["SongFormer.pt", "SongFormer.safetensors"])
        # the remote code imports its sibling files as top-level modules (model, musicfm, ...)
        sys.path.insert(0, local_dir)
        os.chdir(local_dir)
        os.environ["SONGFORMER_LOCAL_DIR"] = local_dir   # the remote code locates its config/stat files through this
        # model.py imports `msaf` (evaluation metrics only, never used at inference); msaf's
        # old dependency pins fight modern numpy/librosa, so satisfy the import with a stub
        stubs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_stubs")
        msaf_dir = os.path.join(stubs, "msaf"); os.makedirs(msaf_dir, exist_ok=True)
        stub_files = {
            '__init__.py': '# stub: evaluation-only dependency of SongFormer, not needed for inference' + chr(10),
            'eval.py': 'def compute_results(*a, **k):' + chr(10) + "    raise NotImplementedError('msaf stub: evaluation is not available')" + chr(10),
        }
        for fn, body in stub_files.items():
            fp = os.path.join(msaf_dir, fn)
            if not os.path.exists(fp):
                with open(fp, 'w', encoding='utf-8') as fh:
                    fh.write(body)
        sys.path.append(stubs)
        model = AutoModel.from_pretrained(local_dir, trust_remote_code=True)
        model.to(device).eval()
        y, _ = librosa.load(args.audio, sr=24000, mono=True)
        with torch.no_grad():
            result = model(np.asarray(y, dtype=np.float32))
        segs = [{"start": float(s["start"]), "end": float(s["end"]), "label": str(s["label"])} for s in result]
        out = {"segments": segs, "device": device, "seconds": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        out = {"error": f"{type(e).__name__}: {e}", "seconds": round(time.time() - t0, 1)}
    sys.stdout = real_stdout
    print(json.dumps(out))
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    raise SystemExit(main())
