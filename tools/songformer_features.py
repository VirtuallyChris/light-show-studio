r"""SongFormer's audio embeddings (MuQ + MusicFM, averaged to 0.48 s steps) and three of its label heads, for every
learned Jam song with a kept copy, into shows/_jam/features/<id>.npz / .emb2.npy / .json (section_labeler.py reads
them). The studio's model environment, on the graphics card (about 3 s a song; a long song retries with a 150 s window):

    .venv-songformer\Scripts\python tools\songformer_features.py [ids...]
"""
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
JAM = os.path.join(REPO, "shows", "_jam")
out_dir, device, threads = os.path.join(JAM, "features"), "cuda", 4
ids = sys.argv[1:] or sorted({f[:-5] for f in os.listdir(os.path.join(JAM, "copies")) if f.endswith(".flac")}
                             & {f[:-5] for f in os.listdir(os.path.join(JAM, "learned")) if f.endswith(".json")})
os.makedirs(out_dir, exist_ok=True)

import numpy as np
import torch

torch.set_num_threads(threads)
from lightshow.studio import songformer_infer as sfi

t0 = time.time()
model, dev = sfi.load_model(device)
if dev.startswith("cuda"):
    torch.cuda.set_per_process_memory_fraction(0.75)
ec = (lambda: torch.cuda.empty_cache()) if dev.startswith("cuda") else (lambda: None)
print(f"loaded on {dev} in {time.time()-t0:.1f}s", flush=True)
import librosa
from dataset.label2id import DATASET_ID_ALLOWED_LABEL_IDS, ID_TO_LABEL

HEADS = [("sf5", model.songformer, 5, 8.333), ("sf0", model.songformer, 0, 8.333), ("sf6", model.songformer, 6, 8.333)]
ALLOWED = dict(DATASET_ID_ALLOWED_LABEL_IDS)


SR = 24000
WIN = {}
for tid in ids:
  try:
      if os.path.exists(os.path.join(out_dir, f"{tid}.npz")) and "--redo" not in os.environ.get("SFH", ""):
          meta = json.load(open(os.path.join(out_dir, f"{tid}.json")))
          if meta.get("peak_gpu_mb") and meta["t_embed"] < 30:
              continue
      ec()
      path = os.path.join(REPO, "shows", "_jam", "copies", f"{tid}.flac")
      t0 = time.time()
      y, _ = librosa.load(path, sr=SR)
      audio = torch.tensor(y).to(dev)
      t_load = time.time() - t0
      if dev.startswith("cuda"):
          torch.cuda.reset_peak_memory_stats()
      t0 = time.time()
      chunks = []
      win = hop = WIN.get(tid, 420)
      i = 0
      with torch.no_grad():
          while i * SR < audio.shape[-1]:
              seg = audio[i * SR:min((i + win) * SR, audio.shape[-1])]
              muq420 = model.muq(seg.unsqueeze(0), output_hidden_states=True)["hidden_states"][10]
              ec()
              fm420 = model.musicfm.get_predictions(seg.unsqueeze(0))[1][10]
              m30, f30 = [], []
              for s30 in range(i, i + hop, 30):
                  a, b = s30 * SR, min((s30 + 30) * SR, audio.shape[-1], (i + hop) * SR)
                  if a >= audio.shape[-1]:
                      break
                  if b - a <= 1024:
                      continue
                  m30.append(model.muq(audio[a:b].unsqueeze(0), output_hidden_states=True)["hidden_states"][10])
                  ec()
                  f30.append(model.musicfm.get_predictions(audio[a:b].unsqueeze(0))[1][10])
                  ec()
              embs = [torch.cat(f30, 1), torch.cat(m30, 1), fm420, muq420]
              n = min(e.shape[1] for e in embs)
              chunks.append((i, torch.cat([e[:, :n] for e in embs], -1)))
              i += hop
      t_emb = time.time() - t0
      t0 = time.time()
      res = {"duration": len(y) / SR, "t_load": t_load, "t_embed": t_emb}
      arrays = {}
      for name, net, ds, fps in HEADS:
          fl, bl = [], []
          for i, embd in chunks:
              f, b = sfi._head_logits(net, embd, ds)
              fl.append(f.float().cpu().numpy())
              bl.append(b.float().cpu().numpy())
          f = np.concatenate(fl)
          b = np.concatenate(bl)
          allowed = ALLOWED[ds]
          f = f[:, allowed]
          prob = np.exp(f - f.max(1, keepdims=True))
          prob /= prob.sum(1, keepdims=True)
          arrays[f"{name}_bound"] = 1 / (1 + np.exp(-b))
          arrays[f"{name}_func"] = prob.astype(np.float32)
          labels = [ID_TO_LABEL[k] for k in allowed]
          res[name] = {"fps": fps, "labels": labels}
      res["t_heads"] = time.time() - t0
      if dev.startswith("cuda"):
          res["peak_gpu_mb"] = torch.cuda.max_memory_allocated() / 2**20
      np.savez_compressed(os.path.join(out_dir, f"{tid}.npz"), **arrays)
      emb = np.concatenate([e[0].float().cpu().numpy() for _, e in chunks])          # frames x 4096 at 25 fps
      k = 12                                                                            # ~0.5 s steps (12 frames of 40 ms)
      n = emb.shape[0] // k * k
      pooled = emb[:n].reshape(-1, k, emb.shape[1]).mean(1)
      np.save(os.path.join(out_dir, f"{tid}.emb2.npy"), pooled.astype(np.float16))
      res["emb2_fps"] = 25.0 / k
      with open(os.path.join(out_dir, f"{tid}.json"), "w") as fh:
          json.dump(res, fh)
      print(tid, {k: (round(v, 1) if isinstance(v, float) else '') for k, v in res.items() if k.startswith('t_') or k == 'peak_gpu_mb'}, flush=True)

  except torch.OutOfMemoryError:
    ec()
    if WIN.get(tid, 420) > 150:
        WIN[tid] = 150
        ids.append(tid)
        print(tid, "out of memory: again with a 150 s window", flush=True)
    else:
        print(tid, "out of memory: skipped", flush=True)
