"""ADTOF (Zehren et al.) in the model environment: the drum hits in the separated drum track.

A learned drum transcriber (a small CNN + GRU, trained on real songs with crowdsourced drum charts),
in the PyTorch port (adtof_pytorch, xavriley/ADTOF-pytorch: no TensorFlow, no madmom). It hears five
drums: kick, snare, hi-hat, toms and cymbals, each hit with the time it starts.

It replaced the studio's own kick and snare finder (events.find_drums, still the fallback), which
looked for a band that repeats in a steady pattern: on 79 drum songs of the user's (Oct 2026, 57
learned in Jam mode) the model agreed with a second learned transcriber (ADT_STR, 2026, built and
trained differently) at a median F 0.90 on kicks (the old finder 0.84) and 0.81 on snares (0.55),
better snares on 75 songs and worse on none, and on the attack itself where the old hits came
17 ms (kicks) and 34 ms (snares) late, up to a 16th note.
It found the drums in quiet verses the old finder skipped (Hastings' Take Mine: no kick for its first
32 s) and did not take a kick for a snare (ELEY's Fire).

Run by models_host.py (model "drums_infer"): the request is the drum stem as samples, the reply
{"hits": {"kick": [[t, strength], ...], "snare": ..., "hat": ..., "tom": ..., "cymbal": ...}}, times in
seconds, strength the model's confidence at the hit (0..1).
"""

from __future__ import annotations

SR = 44100                    # what the model was trained on (its spectrogram: 100 frames a second)
NAMES = {35: "kick", 38: "snare", 42: "hat", 47: "tom", 49: "cymbal"}


def load_model(device: str = "auto"):
    from adtof_pytorch import calculate_n_bins, create_frame_rnn_model, get_default_weights_path, load_pytorch_weights
    from adtof_pytorch.audio import create_adtof_processor

    from . import devices

    if device == "auto":
        device = devices.device_for("drums_infer")
    model = load_pytorch_weights(create_frame_rnn_model(calculate_n_bins()), get_default_weights_path(), strict=True)
    return model.eval().to(device), create_adtof_processor(), device


def find(state, y) -> dict:
    """The hits in mono audio at SR: {drum: [[time, strength], ...]}."""
    import numpy as np
    import torch
    from adtof_pytorch.post_processing import FRAME_RNN_THRESHOLDS, LABELS_5, PeakPicker

    model, proc, device = state
    spec = proc.apply_filterbank(proc.compute_stft(np.asarray(y, dtype=np.float32))).T[None, :, :, None]
    with torch.inference_mode():
        act = model(torch.from_numpy(np.ascontiguousarray(spec)).float().to(device)).cpu().numpy()
    picked = PeakPicker(FRAME_RNN_THRESHOLDS, proc.fps).pick(act, LABELS_5)[0]
    out = {}
    for c, pitch in enumerate(LABELS_5):
        a = act[0, :, c]
        out[NAMES[pitch]] = [[round(float(t), 3), round(float(a[min(len(a) - 1, int(round(t * proc.fps)))]), 3)]
                             for t in picked[pitch]]
    return out
