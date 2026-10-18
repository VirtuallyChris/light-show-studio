"""Demucs (Meta) worker: a song pulled apart into drums, bass, vocals and the rest.

The drums and the bass are what the lights follow, and in a full mix they share every frequency
band with the vocals, synths and guitars. Separated, a kick is the only thing thumping in the drum
track and the bass line is on its own, so the drums can be found far more surely and a drop shows
up as the bass coming back.

Run by models_host.py (model "stems_infer"), inside the model environment (CUDA PyTorch). The request
and the reply, as the host passes them:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                        a file on disk (a full path)
               {"samples": N, "sr": 44100, "channels": 2}         followed by N*channels float32
                                                                  samples, interleaved (Jam mode:
                                                                  audio from memory, never a file)
    reply    one JSON line
               {"stems": {"n": n, "sr": 22050, "order": ["drums", "bass", "other"]},
                "levels": {"vocals": [...], "other": [...]}, "fps": 30, "seconds": 3.2}
             followed by the drum, bass and "other" (synths, guitars, keys) tracks, each n float32
             samples (mono, 22050 Hz),
             or {"error": "..."} with nothing after it.

"""

from __future__ import annotations


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
