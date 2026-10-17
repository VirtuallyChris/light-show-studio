"""ADTOF in the analysis: the kicks and snares, heard by a learned drum transcriber in the separated
drum track (see drums_infer.py for the model and why it replaced events.find_drums, which stays the
fallback wherever the model cannot run).
"""

from __future__ import annotations

import numpy as np

from .model_worker import ModelWorker

WORKER = ModelWorker("lightshow.studio.drums_infer", "hits", start_timeout=600, request_timeout=120)


def available() -> bool:
    return WORKER.available()


def hits(stems: dict | None) -> dict | None:
    """The drum stem's hits, {drum: [(time, strength 0..1), ...]}, strength against the song's own strong
    hits (as events.find_drums gives them); None if the model cannot run. Audio from memory only."""
    if not stems or stems.get("drums") is None:
        return None
    r = WORKER.ask_audio(np.asarray(stems["drums"], dtype=np.float32), int(stems.get("sr") or 22050))
    if not r:
        return None
    out = {}
    for drum, hs in r.items():
        hs = [(float(t), float(v)) for t, v in hs]
        ref = float(np.percentile([v for _, v in hs], 95)) if hs else 1.0
        out[drum] = [(t, float(np.clip(v / (ref + 1e-9), 0.0, 1.0))) for t, v in hs]
    return out
