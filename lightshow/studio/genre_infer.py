"""Genre worker: MAEST (MTG, trained on the music styles Discogs gives 3.3 million releases), with MIT's
Audio Spectrogram Transformer (AudioSet) as the fallback.

AudioSet's music labels are coarse (one "Rock music", one "Pop music"), and AST put pop and rock songs
under dance music: Hastings' Take Mine came out "Dubstep, Pop music", Madeon's poppy French house as
drum and bass, Your Neighbors' indie rock as "Electronic dance music". MAEST knows 519 styles in their
genres ("Rock---Pop Rock", "Electronic---Electro House", "Pop---Indie Pop"). On 60 songs of the user's
with a known style and their own drive (Oct 2026) the show style came out right for 52 with MAEST
against 49 with AST (7 of AST's 11 misses too hard, as the party heard: Madeon as rave, Take Mine as
dance), and 40 of 44 against 38 on the Jam songs not used to place MAEST's styles on the studio's
scale (genre.DISCOGS_TIER). Its weak spot: Discogs has no future bass or melodic dubstep, so those can
come out softer. Of the two MAEST checkpoints tried this one (519 styles) did best; the 400-style one
called loud rock bands dance. (MAEST's licence is non-commercial.)

Run by models_host.py (model "genre_infer"); the request and the reply, as the host passes them:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                    a file on disk
               {"samples": N, "sr": 16000}                    followed by N float32 samples
                                                              (raw audio from memory: Jam mode
                                                              never writes captured audio to disk)
    reply    one JSON line
               {"labels": {"Rock---Pop Rock": 0.59, ...}, "windows": 6, "model": "maest", "seconds": 0.6}
               or {"error": "..."}

MAEST's labels are "Genre---Style"; AST's (the fallback) are AudioSet names ("Dubstep", "Pop music")
with its music moods.
"""

from __future__ import annotations

import sys

SR = 16000

MAEST = "mtg-upf/discogs-maest-30s-pw-129e-519l"
MAEST_REVISION = "6c35f32a350f74351870937d5ae0bae1d898d1df"   # pinned: its feature extractor is the repo's own code
MAEST_WINDOW_S = 30.0                                          # what it was trained on
MAEST_WINDOWS = 6
MAEST_KEEP = 0.02                                              # styles scored under this are left out of the reply

MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"             # the fallback
WINDOW_S = 10.24          # what AST was trained on
MAX_WINDOWS = 8

# AudioSet's music genres and music moods, as named in AST's label list
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


def windows(y, sr: int, window_s: float = WINDOW_S, most: int = MAX_WINDOWS):
    """Up to `most` windows spread across the song, leaving out the very start and end."""
    import numpy as np

    n = int(window_s * sr)
    if len(y) <= n:
        return [np.pad(y, (0, n - len(y)))]
    lo, hi = int(0.05 * len(y)), int(0.95 * len(y)) - n
    if hi <= lo:
        lo, hi = 0, len(y) - n
    count = int(min(most, max(1, (hi - lo) // n + 1)))
    starts = np.linspace(lo, hi, count).astype(int)
    return [y[s:s + n] for s in starts]


def load_model():
    """MAEST, or AST where MAEST cannot load: ("maest" | "ast", extractor, model, keep, device)."""
    import torch
    from transformers import ASTFeatureExtractor, ASTForAudioClassification, AutoFeatureExtractor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        extractor = AutoFeatureExtractor.from_pretrained(MAEST, revision=MAEST_REVISION, trust_remote_code=True)
        model = ASTForAudioClassification.from_pretrained(MAEST, revision=MAEST_REVISION).to(device).eval()
        return "maest", extractor, model, list(model.config.id2label.items()), device
    except Exception as e:  # noqa: BLE001 - the older model still tells rock from dubstep, mostly
        print(f"[genre] MAEST would not load ({type(e).__name__}: {e}); using AST", file=sys.stderr, flush=True)
    extractor = ASTFeatureExtractor.from_pretrained(MODEL)
    model = ASTForAudioClassification.from_pretrained(MODEL).to(device).eval()
    keep = [(i, name) for i, name in model.config.id2label.items() if name in MUSIC_LABELS]
    return "ast", extractor, model, keep, device


def classify(state, y) -> dict:
    """Labels for mono audio at SR: {"labels": {...}, "windows": n, "model": "maest" | "ast"}."""
    import numpy as np
    import torch

    kind, extractor, model, keep, device = state
    if len(y) < SR * 3:
        raise ValueError("not enough audio to tell the genre")
    wins = windows(y, SR, MAEST_WINDOW_S, MAEST_WINDOWS) if kind == "maest" else windows(y, SR)
    feats = extractor([w.astype(np.float32) for w in wins], sampling_rate=SR, return_tensors="pt")
    with torch.inference_mode():
        probs = torch.sigmoid(model(**{k: v.to(device) for k, v in feats.items()}).logits).mean(dim=0).cpu().numpy()
    if kind == "maest":
        labels = {name: round(float(probs[i]), 4) for i, name in keep if probs[i] >= MAEST_KEEP}
    else:
        labels = {name: round(float(probs[i]), 4) for i, name in keep}
    return {"labels": labels, "windows": len(wins), "model": kind}
