"""Genre worker: MIT's Audio Spectrogram Transformer, fine-tuned on AudioSet.

Runs inside the separate model environment (Python 3.12, CUDA PyTorch), as a
long-lived process so the model loads once. The studio talks to it over
stdin/stdout, one request at a time:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                    a file on disk
               {"samples": N, "sr": 16000}                    followed by N float32 samples
                                                              (raw audio from memory: Jam mode
                                                              never writes captured audio to disk)
    reply    one JSON line
               {"labels": {"Dubstep": 0.91, ...}, "windows": 8, "seconds": 0.6}
               or {"error": "..."}

The first line the worker prints is {"ready": true, "device": "cuda"} once the
model is loaded. Only music genre and music mood labels are returned; AudioSet
also knows about dogs and doorbells, which are no use here.

    python -m lightshow.studio.genre_infer
"""

from __future__ import annotations

import json
import os
import sys
import time

MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
SR = 16000
WINDOW_S = 10.24          # what the model was trained on
MAX_WINDOWS = 8

# AudioSet's music genres and music moods, as named in the model's label list
MUSIC_LABELS = {
    "Pop music", "Hip hop music", "Beatboxing", "Rock music", "Heavy metal", "Punk rock", "Grunge", "Progressive rock",
    "Rock and roll", "Psychedelic rock", "Rhythm and blues", "Soul music", "Reggae", "Country", "Swing music", "Bluegrass",
    "Funk", "Folk music", "Middle Eastern music", "Jazz", "Disco", "Classical music", "Opera", "Electronic music",
    "House music", "Techno", "Dubstep", "Drum and bass", "Electronica", "Electronic dance music", "Ambient music",
    "Trance music", "Music of Latin America", "Salsa music", "Flamenco", "Blues", "Music for children", "New-age music",
    "Vocal music", "A capella", "Music of Africa", "Afrobeat", "Christian music", "Gospel music", "Music of Asia",
    "Carnatic music", "Music of Bollywood", "Ska", "Traditional music", "Independent music", "Video game music",
    "Christmas music", "Dance music", "Wedding music", "Lullaby",
    "Happy music", "Funny music", "Sad music", "Tender music", "Exciting music", "Angry music", "Scary music",
}


def windows(y, sr: int):
    """Up to eight 10-second windows spread across the song, leaving out the very start and end."""
    import numpy as np

    n = int(WINDOW_S * sr)
    if len(y) <= n:
        return [np.pad(y, (0, n - len(y)))]
    lo, hi = int(0.05 * len(y)), int(0.95 * len(y)) - n
    if hi <= lo:
        lo, hi = 0, len(y) - n
    count = int(min(MAX_WINDOWS, max(1, (hi - lo) // n + 1)))
    starts = np.linspace(lo, hi, count).astype(int)
    return [y[s:s + n] for s in starts]


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr                               # libraries may print; only replies go to the real stdout
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

    def reply(obj):
        real_stdout.write(json.dumps(obj) + "\n")
        real_stdout.flush()

    try:
        import librosa
        import numpy as np
        import torch
        from transformers import ASTFeatureExtractor, ASTForAudioClassification

        device = "cuda" if torch.cuda.is_available() else "cpu"
        extractor = ASTFeatureExtractor.from_pretrained(MODEL)
        model = ASTForAudioClassification.from_pretrained(MODEL).to(device).eval()
        keep = [(i, name) for i, name in model.config.id2label.items() if name in MUSIC_LABELS]
    except Exception as e:  # noqa: BLE001
        reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    reply({"ready": True, "device": device, "labels": len(keep)})

    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        t0 = time.time()
        try:
            req = json.loads(line)
            if "samples" in req:
                raw = stdin.read(int(req["samples"]) * 4)
                y = np.frombuffer(raw, dtype=np.float32)
                if int(req.get("sr", SR)) != SR:
                    y = librosa.resample(y, orig_sr=int(req["sr"]), target_sr=SR)
            else:
                y, _ = librosa.load(req["path"], sr=SR, mono=True)
            if len(y) < SR * 3:
                raise ValueError("not enough audio to tell the genre")
            wins = windows(y, SR)
            feats = extractor([w.astype(np.float32) for w in wins], sampling_rate=SR, return_tensors="pt")
            with torch.inference_mode():
                probs = torch.sigmoid(model(**{k: v.to(device) for k, v in feats.items()}).logits).mean(dim=0).cpu().numpy()
            labels = {name: round(float(probs[i]), 4) for i, name in keep}
            reply({"labels": labels, "windows": len(wins), "seconds": round(time.time() - t0, 2)})
        except Exception as e:  # noqa: BLE001 - one bad request must not end the worker
            reply({"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())
