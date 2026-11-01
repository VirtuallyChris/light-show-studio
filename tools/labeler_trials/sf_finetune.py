"""SongFormer's own section layers fine-tuned on the user's labels (run with .venv-songformer, on the card).

Its transformer and heads (trained on thousands of songs) start as they are; a new label head for the studio's six
labels starts from its own rows (drop<-chorus, verse<-verse, buildup<-pre-chorus, breakdown<-bridge, intro, outro);
everything learns at a gentle rate (the new head faster). Input: the saved embeddings (0.48 s steps) repeated back
to SongFormer's 25 frames a second. 10 folds by song (the trees' and the GRU's), scored per bar, smoothed into
sections, and blended with the trees. ADD_CLAUDE=1: Claude's reviews join every fold's training (never a fold's
test, nor one of the same name as it); the scores are on the user's songs alone.
"""
import copy
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shows", "_jam", "features", "trials")
os.makedirs(HERE, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from lightshow.studio import songformer_infer as sfi  # noqa: E402
from lightshow.studio.labeler import smooth  # noqa: E402

torch.manual_seed(0); np.random.seed(0)
EPOCHS = int(os.environ.get("EPOCHS", 12))
LR = float(os.environ.get("LR", 3e-5))
ADD = bool(os.environ.get("ADD_CLAUDE"))
FREEZE = os.environ.get("FREEZE", "")                 # "low": keep the input layers as they are
model, dev = sfi.load_model("cuda")
base = model.songformer.float()
del model
torch.cuda.empty_cache()
TO_SF = [0, 1, 26, 2, 3, 5]                          # intro, verse, buildup<-pre-chorus, drop<-chorus, breakdown<-bridge, outro
FPS_OUT = 25.0 / 3

z = np.load(os.path.join(HERE, "nn_data.npz"))
n = int(z["n"])
S = []
for i in range(n):
    tid = str(z[f"id{i}"])
    e2 = np.load(os.path.join(REPO, "shows", "_jam", "features", tid + ".emb2.npy"))
    S.append({"E": e2, "y": z[f"y{i}"], "sp": z[f"sp{i}"], "r": z[f"r{i}"], "w": z[f"w{i}"], "f": int(z[f"f{i}"]),
              "d": str(z[f"d{i}"]), "T": z[f"T{i}"], "c": int(z[f"c{i}"])})


def frame_labels(s, n_out):
    """The user's label at each output frame (8.33 a second), -1 where none."""
    t = (np.arange(n_out) + 0.5) / FPS_OUT
    idx = np.searchsorted(s["sp"][:, 0], t, side="right") - 1
    ok = (idx >= 0) & (idx < len(s["y"])) & (t < s["sp"][np.clip(idx, 0, len(s["y"]) - 1), 1])
    out = np.full(n_out, -1)
    out[ok] = s["y"][idx[ok]]
    return out


class Tuned(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = copy.deepcopy(base)
        old = self.net.function_head.net[0]
        self.head = nn.Linear(old.in_features, 6)
        with torch.no_grad():
            self.head.weight.copy_(old.weight[TO_SF]); self.head.bias.copy_(old.bias[TO_SF])
        if FREEZE == "low":
            for m in (self.net.mixed_win_downsample, self.net.input_norm, self.net.down_sample_conv):
                for p in m.parameters():
                    p.requires_grad = False

    def forward(self, embd):
        net = self.net
        x = net.mixed_win_downsample(embd)
        x = net.input_norm(x)
        x = net.down_sample_conv(x)
        x = x + net.dataset_class_prefix(torch.tensor([5], device=embd.device)).unsqueeze(1)
        x = net.transformer(x=x, src_key_padding_mask=None)
        return self.head(x)[0]


def embd(s):
    e = torch.tensor(s["E"].astype(np.float32), device=dev)
    return e.repeat_interleave(12, dim=0).unsqueeze(0)          # back to 25 frames a second


def train(tr):
    m = Tuned().to(dev)
    y_all = np.concatenate([s["y"][s["y"] >= 0] for s in tr])
    cw = torch.tensor(len(y_all) / (6 * np.maximum(1, np.bincount(y_all, minlength=6))), dtype=torch.float32, device=dev)
    head_p = list(m.head.parameters())
    rest = [p for n_, p in m.named_parameters() if not n_.startswith("head") and p.requires_grad]
    opt = torch.optim.AdamW([{"params": rest, "lr": LR}, {"params": head_p, "lr": 1e-3}], weight_decay=1e-2)
    lossf = nn.CrossEntropyLoss(weight=cw, ignore_index=-1)
    for ep in range(EPOCHS):
        m.train()
        opt.zero_grad()
        for k, i in enumerate(np.random.permutation(len(tr))):
            s = tr[i]
            out = m(embd(s))
            y = torch.tensor(frame_labels(s, out.shape[0]), device=dev)
            loss = lossf(out, y) / 4
            loss.backward()
            if k % 4 == 3:
                nn.utils.clip_grad_norm_(m.parameters(), 1.0); opt.step(); opt.zero_grad()
        opt.step(); opt.zero_grad()
    m.eval()
    return m


def bar_probs(m, s):
    with torch.no_grad():
        p = torch.softmax(m(embd(s)), -1).cpu().numpy()
    t = (np.arange(len(p)) + 0.5) / FPS_OUT
    out = np.zeros((len(s["y"]), 6))
    for b, (b0, b1) in enumerate(s["sp"]):
        sel = (t >= b0) & (t < b1)
        out[b] = p[sel].mean(0) if sel.any() else p[min(len(p) - 1, int(b0 * FPS_OUT))]
    return out


if os.environ.get("SMOKE"):                            # one song through it: time, memory, frames
    m = Tuned().to(dev); s = S[0]; t0 = time.time()
    out = m(embd(s)); yy = torch.tensor(frame_labels(s, out.shape[0]), device=dev)
    nn.functional.cross_entropy(out, yy, ignore_index=-1).backward(); torch.cuda.synchronize()
    print("frames out", tuple(out.shape), "labeled", int((yy >= 0).sum()), f"fwd+bwd {time.time() - t0:.2f}s",
          f"peak GPU {torch.cuda.max_memory_allocated() / 2**20:.0f} MB")
    p = bar_probs(m, s)
    print("untrained on this song, agreement:", round(float((p.argmax(1) == s["y"])[s["y"] >= 0].mean()), 3))
    sys.exit(0)
t0 = time.time()
for k in range(10):
    tr = [s for s in S if s["f"] != k and (ADD or not s["c"])]
    m = train(tr)
    for s in S:
        if s["f"] == k and not s["c"]:
            s["N"] = bar_probs(m, s)
    del m
    torch.cuda.empty_cache()
    print(f"fold {k} done ({time.time() - t0:.0f} s)", flush=True)

C = sum(s["c"] for s in S)
S = [s for s in S if not s["c"]]                                 # scored on the user's songs alone
y = np.concatenate([s["y"] for s in S]); w = np.concatenate([s["w"] for s in S]); d = np.concatenate([np.full(len(s["y"]), s["d"]) for s in S])
mask = y >= 0


def score(name, preds):
    p = np.concatenate(preds)
    out = []
    for title, mm in [("all", mask)] + [(dd, mask & (d == dd)) for dd in ("2026-10-19", "2026-10-21")]:
        out.append(f"{title}: {np.average((p == y)[mm], weights=w[mm]):.1%} / {np.average(((p == 3) == (y == 3))[mm], weights=w[mm]):.1%}")
    print(f"  {name:40s} " + "   ".join(out), flush=True)


print(f"10 folds by song, {len(S)} of the user's songs" + (f" + {C} of Claude's in training" if ADD else "") + f" (types / drop-not-drop); epochs {EPOCHS}, lr {LR}, freeze {FREEZE or 'none'}")
score("the rules", [s["r"] for s in S])
score("the trees, as sections", [smooth(s["T"]) for s in S])
score("SongFormer fine-tuned, bars", [s["N"].argmax(1) for s in S])
score("SongFormer fine-tuned, as sections", [smooth(s["N"]) for s in S])
for a in (0.3, 0.5, 0.7):
    score(f"blend {a:.1f} SongFormer + {1 - a:.1f} trees, sections", [smooth(a * s["N"] + (1 - a) * s["T"]) for s in S])
