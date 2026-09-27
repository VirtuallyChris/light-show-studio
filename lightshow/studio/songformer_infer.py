"""SongFormer (ASLP-lab) worker: where a song's sections are and what they are.

Runs inside the separate model environment (Python 3.12, CUDA PyTorch). The
studio never imports it; it runs it as a long-lived process so the model loads
once, and talks to it over stdin/stdout one request at a time:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                  a file on disk (always a full path)
               {"samples": N, "sr": 24000}                  followed by N float32 samples
                                                            (audio from memory: Jam mode never
                                                            writes captured audio to disk)
    reply    one JSON line
               {"segments": [{"start": 0.0, "end": 15.2, "label": "verse"}, ...], "seconds": 4.1}
               or {"error": "..."}

The first line printed is {"ready": true, "device": "cuda:0"} once the model is
loaded. For a one-off run from a terminal:

    python -m lightshow.studio.songformer_infer <full path to audio file>
"""

from __future__ import annotations

import json
import os
import sys
import time

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


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr                              # libraries print; only replies go to the real stdout
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

    def reply(obj):
        real_stdout.write(json.dumps(obj) + "\n")
        real_stdout.flush()

    one_off = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("--")]   # resolved before the model moves the cwd
    t0 = time.time()
    try:
        import librosa
        import numpy as np

        model, device = load_model()
    except Exception as e:  # noqa: BLE001
        reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1

    if one_off:                                          # terminal use: one file, one answer
        try:
            y, _ = librosa.load(one_off[0], sr=SR, mono=True)
            reply({"segments": segments(model, y), "device": device, "seconds": round(time.time() - t0, 1)})
            return 0
        except Exception as e:  # noqa: BLE001
            reply({"error": f"{type(e).__name__}: {e}"})
            return 1

    reply({"ready": True, "device": device})
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        t0 = time.time()
        try:
            req = json.loads(line)
            if "samples" in req:
                y = np.frombuffer(stdin.read(int(req["samples"]) * 4), dtype=np.float32)
                if int(req.get("sr", SR)) != SR:
                    y = librosa.resample(y, orig_sr=int(req["sr"]), target_sr=SR)
            else:
                path = req["path"]
                if not os.path.isabs(path):
                    raise ValueError(f"a full path is needed, got {path!r}")
                y, _ = librosa.load(path, sr=SR, mono=True)
            if len(y) < SR * 8:
                raise ValueError("not enough audio to find sections in")
            reply({"segments": segments(model, y), "seconds": round(time.time() - t0, 2)})
        except Exception as e:  # noqa: BLE001 - one bad request must not end the worker
            reply({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())
