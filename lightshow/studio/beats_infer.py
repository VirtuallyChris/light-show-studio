"""Beat This (CPJKU) worker: where a song's beats and downbeats are.

Run by models_host.py (model "beats_infer"), inside the model environment (Python 3.12, CUDA PyTorch).
The request and the reply, as the host passes them:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                  a file on disk (always a full path)
               {"samples": N, "sr": 22050}                  followed by N float32 mono samples
                                                            (Jam mode never writes audio to disk)
    reply    one JSON line
               {"beats": {"beats": [...], "downbeats": [...]}, "seconds": 1.2}   (times in seconds)
               or {"error": "..."}

Its checkpoint comes from the authors' server the first time, then from the torch hub cache.
"""

from __future__ import annotations


SR = 22050


def load_model(device: str = "auto"):
    from beat_this.inference import Audio2Beats

    from . import devices

    if device == "auto":
        device = devices.device_for("beats_infer")
    return Audio2Beats(checkpoint_path="final0", device=device, dbn=False), device


def find(model, y) -> dict:
    import numpy as np
    import torch

    with torch.no_grad():
        beats, downbeats = model(np.asarray(y, dtype=np.float32), SR)
    return {"beats": [round(float(t), 4) for t in beats], "downbeats": [round(float(t), 4) for t in downbeats]}
