"""Beat This (CPJKU) worker: where a song's beats and downbeats are.

Runs inside the separate model environment (Python 3.12, CUDA PyTorch), as a long-lived process so the
model loads once, one request at a time over stdin/stdout, like songformer_infer:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                  a file on disk (always a full path)
               {"samples": N, "sr": 22050}                  followed by N float32 mono samples
                                                            (Jam mode never writes audio to disk)
    reply    one JSON line
               {"beats": {"beats": [...], "downbeats": [...]}, "seconds": 1.2}   (times in seconds)
               or {"error": "..."}

The first line printed is {"ready": true, "device": "cuda:0"} once the model is loaded (its checkpoint
comes from the authors' server the first time, then from the torch hub cache). From a terminal:

    python -m lightshow.studio.beats_infer <full path to audio file>
"""

from __future__ import annotations

import json
import os
import sys
import time

SR = 22050


def load_model(device: str = "auto"):
    import torch
    from beat_this.inference import Audio2Beats

    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    return Audio2Beats(checkpoint_path="final0", device=device, dbn=False), device


def find(model, y) -> dict:
    import numpy as np
    import torch

    with torch.no_grad():
        beats, downbeats = model(np.asarray(y, dtype=np.float32), SR)
    return {"beats": [round(float(t), 4) for t in beats], "downbeats": [round(float(t), 4) for t in downbeats]}


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr                              # libraries print; only replies go to the real stdout

    def reply(obj):
        real_stdout.write(json.dumps(obj) + "\n")
        real_stdout.flush()

    one_off = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("--")]
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
            reply({"beats": find(model, y), "device": device, "seconds": round(time.time() - t0, 1)})
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
            if len(y) < SR * 5:
                raise ValueError("not enough audio to find beats in")
            reply({"beats": find(model, y), "seconds": round(time.time() - t0, 2)})
        except Exception as e:  # noqa: BLE001 - one bad request must not end the worker
            reply({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())
