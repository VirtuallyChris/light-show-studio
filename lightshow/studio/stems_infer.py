"""Demucs (Meta) worker: a song pulled apart into drums, bass, vocals and the rest.

The drums and the bass are what the lights follow, and in a full mix they share every frequency
band with the vocals, synths and guitars. Separated, a kick is the only thing thumping in the drum
track and the bass line is on its own, so the drums can be found far more surely and a drop shows
up as the bass coming back.

Runs inside the separate model environment (CUDA PyTorch), as a long-lived process so the model
loads once, one request at a time over stdin/stdout:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                        a file on disk (a full path)
               {"samples": N, "sr": 44100, "channels": 2}         followed by N*channels float32
                                                                  samples, interleaved (Jam mode:
                                                                  audio from memory, never a file)
    reply    one JSON line
               {"stems": {"n": n, "sr": 22050, "order": ["drums", "bass"]},
                "levels": {"vocals": [...], "other": [...]}, "fps": 30, "seconds": 3.2}
             followed by the drum track then the bass track, each n float32 samples (mono, 22050 Hz),
             or {"error": "..."} with nothing after it.

The first line printed is {"ready": true, "device": "cuda:0"} once the model is loaded.
For a one-off run from a terminal (prints timings and levels only):

    python -m lightshow.studio.stems_infer <full path to audio file>
"""

from __future__ import annotations

import json
import os
import sys
import time

MODEL = "htdemucs"
OUT_SR = 22050
FPS = 30


def load_model():
    import torch
    from demucs.pretrained import get_model

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    model = get_model(MODEL)
    model.to(device).eval()
    return model, device


def separate(model, device, y, sr):
    """y: (channels, n) float32 at sr. Returns {source: mono float32 at OUT_SR}, levels at FPS."""
    import librosa
    import numpy as np
    import torch
    from demucs.apply import apply_model

    if y.ndim == 1:
        y = np.stack([y, y])
    if y.shape[0] == 1:
        y = np.concatenate([y, y])
    if sr != model.samplerate:
        y = librosa.resample(y, orig_sr=sr, target_sr=model.samplerate)
    wav = torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32))
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        out = apply_model(model, ((wav - mean) / std)[None], device=device, split=True, overlap=0.25, shifts=0, progress=False)[0]
    out = out * std + mean
    stems = {}
    for name, s in zip(model.sources, out):
        mono = s.mean(0).cpu().numpy().astype(np.float32)
        stems[name] = librosa.resample(mono, orig_sr=model.samplerate, target_sr=OUT_SR).astype(np.float32)
    hop = OUT_SR // FPS
    levels = {}
    for name in ("vocals", "other", "bass", "drums"):
        x = stems[name]
        n = len(x) // hop
        rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
        levels[name] = [round(float(v), 4) for v in rms]
    return stems, levels


def main() -> int:
    real_stdout = sys.stdout.buffer
    sys.stdout = sys.stderr                              # libraries print; only replies go to the real stdout

    def reply(obj, payload: bytes = b""):
        real_stdout.write((json.dumps(obj) + "\n").encode() + payload)
        real_stdout.flush()

    one_off = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith("--")]
    try:
        import librosa
        import numpy as np

        model, device = load_model()
    except Exception as e:  # noqa: BLE001
        reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1

    def load(path):
        y, sr = librosa.load(path, sr=None, mono=False)
        return y, sr

    if one_off:
        t0 = time.time()
        y, sr = load(one_off[0])
        stems, levels = separate(model, device, y, sr)
        print(json.dumps({"device": device, "seconds": round(time.time() - t0, 1), "n": len(stems["drums"])}), file=sys.stderr)
        return 0

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
                ch = int(req.get("channels", 1))
                y = np.frombuffer(stdin.read(int(req["samples"]) * ch * 4), dtype=np.float32)
                y = y.reshape(-1, ch).T if ch > 1 else y
                sr = int(req.get("sr", 44100))
            else:
                path = req["path"]
                if not os.path.isabs(path):
                    raise ValueError(f"a full path is needed, got {path!r}")
                y, sr = load(path)
            stems, levels = separate(model, device, np.asarray(y, dtype=np.float32), sr)
            d, b = stems["drums"], stems["bass"]
            reply({"stems": {"n": len(d), "sr": OUT_SR, "order": ["drums", "bass"]}, "levels": {k: levels[k] for k in ("vocals", "other")},
                   "fps": FPS, "seconds": round(time.time() - t0, 2)}, d.tobytes() + b.tobytes())
        except Exception as e:  # noqa: BLE001 - one bad request must not end the worker
            reply({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())
