"""SongFormer (ASLP-lab) worker: where a song's sections are and what they are.

Run by models_host.py (model "songformer_infer"), inside the model environment (Python 3.12, CUDA
PyTorch); the studio never imports it. The request and the reply, as the host passes them:

    request  one JSON line, either
               {"path": "C:/.../song.mp3"}                  a file on disk (always a full path)
               {"samples": N, "sr": 24000}                  followed by N float32 samples
                                                            (audio from memory: Jam mode never
                                                            writes captured audio to disk)
    reply    one JSON line
               {"segments": [{"start": 0.0, "end": 15.2, "label": "verse"}, ...], "seconds": 4.1}
               or {"error": "..."}

"""

from __future__ import annotations

import os
import sys

REPO = "ASLP-lab/SongFormer"
SR = 24000


def load_model(device: str = "auto"):
    from huggingface_hub import snapshot_download
    from transformers import AutoModel

    from . import devices

    if device == "auto":
        device = devices.device_for("songformer_infer")
    local_dir = snapshot_download(repo_id=REPO, repo_type="model", ignore_patterns=["SongFormer.pt", "SongFormer.safetensors"])
    # The remote code imports its sibling files as top-level modules (model, musicfm, ...)
    # and finds its config through the working directory, so the working directory moves.
    # That is why every path handed to this worker must be a full path.
    sys.path.insert(0, local_dir)
    os.chdir(local_dir)
    os.environ["SONGFORMER_LOCAL_DIR"] = local_dir
    # model.py imports `msaf` (evaluation metrics only, never used at inference); msaf's
    # old dependency pins fight modern numpy/librosa, so satisfy the import with a stub
    # (next to this file from source; a packaged app's folder is read-only, so its data folder)
    stubs = os.path.join(os.environ.get("LSS_DATA_DIR") or os.path.dirname(os.path.abspath(__file__)), "_stubs")
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


# ---------------------------------------------------------------- the learned section labeler's inputs

FEATURE_HEADS = (("sf5", 5), ("sf0", 0), ("sf6", 6))     # SongFormer's head with three of its dataset prefixes
POOL = 12                                                   # 25 fps embeddings averaged to 0.48 s steps


def _head_logits(net, embd, ds):
    import torch
    with torch.no_grad():
        x = net.mixed_win_downsample(embd)
        x = net.input_norm(x)
        x = net.down_sample_conv(x)
        x = x + net.dataset_class_prefix(torch.tensor([ds], device=embd.device)).unsqueeze(1)
        x = net.transformer(x=x, src_key_padding_mask=None)
        return net.function_head(x)[0], net.boundary_head(x).squeeze(-1)[0]


def features(model, y, out_dir: str, name: str) -> dict:
    """SongFormer's audio embeddings (MuQ and MusicFM, 30 s and 420 s context, layer 10: 4096 numbers 25 times a
    second, averaged to 0.48 s steps) and three of its label heads, for the learned section labeler (labeler.py):
    written to out_dir/<name>.emb2.npy, .npz and .json. A long song that does not fit on the card is done again
    with a 150 s window. Returns the .json's contents."""
    import json
    import time

    import numpy as np
    import torch
    from dataset.label2id import DATASET_ID_ALLOWED_LABEL_IDS, ID_TO_LABEL

    dev = next(model.parameters()).device
    audio = torch.tensor(np.asarray(y, dtype=np.float32)).to(dev)
    ec = {"cuda": lambda: torch.cuda.empty_cache(), "mps": lambda: torch.mps.empty_cache()}.get(dev.type, lambda: None)
    t0 = time.time()
    for win in (420, 150):
        try:
            chunks, i = [], 0
            with torch.no_grad():
                while i * SR < audio.shape[-1]:
                    seg = audio[i * SR:min((i + win) * SR, audio.shape[-1])]
                    muq_long = model.muq(seg.unsqueeze(0), output_hidden_states=True)["hidden_states"][10]
                    ec()
                    fm_long = model.musicfm.get_predictions(seg.unsqueeze(0))[1][10]
                    m30, f30 = [], []
                    for s30 in range(i, i + win, 30):
                        a, b = s30 * SR, min((s30 + 30) * SR, audio.shape[-1], (i + win) * SR)
                        if a >= audio.shape[-1]:
                            break
                        if b - a <= 1024:
                            continue
                        m30.append(model.muq(audio[a:b].unsqueeze(0), output_hidden_states=True)["hidden_states"][10])
                        ec()
                        f30.append(model.musicfm.get_predictions(audio[a:b].unsqueeze(0))[1][10])
                        ec()
                    embs = [torch.cat(f30, 1), torch.cat(m30, 1), fm_long, muq_long]
                    n = min(e.shape[1] for e in embs)
                    chunks.append(torch.cat([e[:, :n] for e in embs], -1))
                    i += win
            break
        except torch.OutOfMemoryError:
            ec()
            if win == 150:
                raise
    meta = {"duration": len(y) / SR, "t_embed": time.time() - t0, "window": win, "emb2_fps": 25.0 / POOL}
    arrays = {}
    for name_h, ds in FEATURE_HEADS:
        fl, bl = [], []
        for embd in chunks:
            f, b = _head_logits(model.songformer, embd, ds)
            fl.append(f.float().cpu().numpy())
            bl.append(b.float().cpu().numpy())
        f, b = np.concatenate(fl), np.concatenate(bl)
        allowed = DATASET_ID_ALLOWED_LABEL_IDS[ds]
        f = f[:, allowed]
        prob = np.exp(f - f.max(1, keepdims=True))
        arrays[f"{name_h}_func"] = (prob / prob.sum(1, keepdims=True)).astype(np.float32)
        arrays[f"{name_h}_bound"] = (1 / (1 + np.exp(-b))).astype(np.float32)
        meta[name_h] = {"fps": 8.333, "labels": [ID_TO_LABEL[k] for k in allowed]}
    emb = np.concatenate([e[0].float().cpu().numpy() for e in chunks])
    n = emb.shape[0] // POOL * POOL
    os.makedirs(out_dir, exist_ok=True)
    np.save(os.path.join(out_dir, f"{name}.emb2.npy"), emb[:n].reshape(-1, POOL, emb.shape[1]).mean(1).astype(np.float16))
    np.savez_compressed(os.path.join(out_dir, f"{name}.npz"), **arrays)
    with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    ec()
    return meta
