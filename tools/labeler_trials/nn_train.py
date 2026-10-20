"""The neural-net trial (run with .venv-songformer: PyTorch on the card). A bidirectional GRU over a song's bars, each bar
SongFormer's 4096-number embedding (learned down to 128) plus the trees' features; 10 folds by song (the same as the
trees'); scored against the trees and the rules, bar by bar and smoothed into sections, and blended with the trees."""
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shows", "_jam", "features", "trials")
os.makedirs(HERE, exist_ok=True)
sys.path.insert(0, r"C:\Users\chris\Documents\light-show-studio")
from lightshow.studio.labeler import smooth  # noqa: E402

torch.manual_seed(0); np.random.seed(0)
dev = "cuda"
z = np.load(os.path.join(HERE, "nn_data.npz"))
n = int(z["n"])
S = [{k: z[f"{k}{i}"] for k in ("X", "E", "y", "r", "w", "f", "d", "T")} for i in range(n)]
for s in S:
    s["X"] = np.nan_to_num(s["X"].astype(np.float32), nan=0.0)
    s["E"] = s["E"].astype(np.float32)
    s["E"] = (s["E"] - s["E"].mean(0)) / (s["E"].std(0) + 1e-3) * 0.5 + s["E"] * 0       # (each song's embedding against itself)
D_X, D_E = S[0]["X"].shape[1], S[0]["E"].shape[1]
EPOCHS = int(os.environ.get("EPOCHS", 60))
HID = int(os.environ.get("HID", 128))


class Net(nn.Module):
    def __init__(self):
        super().__init__()
        self.e = nn.Sequential(nn.Dropout(0.2), nn.Linear(D_E, HID), nn.GELU())
        self.x = nn.Sequential(nn.Dropout(0.1), nn.Linear(D_X, HID), nn.GELU())
        self.gru = nn.GRU(2 * HID, HID, num_layers=2, batch_first=True, bidirectional=True, dropout=0.3)
        self.out = nn.Sequential(nn.Dropout(0.3), nn.Linear(2 * HID, 6))

    def forward(self, x, e):
        h, _ = self.gru(torch.cat([self.x(x), self.e(e)], -1))
        return self.out(h)


def batch(songs, mu, sd):
    L = max(len(s["y"]) for s in songs)
    X = np.zeros((len(songs), L, D_X), np.float32); E = np.zeros((len(songs), L, D_E), np.float32); Y = np.full((len(songs), L), -1)
    for i, s in enumerate(songs):
        m = len(s["y"])
        X[i, :m] = (s["X"] - mu) / sd; E[i, :m] = s["E"]; Y[i, :m] = s["y"]
    return torch.tensor(X, device=dev), torch.tensor(E, device=dev), torch.tensor(Y, device=dev)


def train(tr):
    mu = np.concatenate([s["X"] for s in tr]).mean(0); sd = np.concatenate([s["X"] for s in tr]).std(0) + 1e-3
    y_all = np.concatenate([s["y"][s["y"] >= 0] for s in tr])
    cw = torch.tensor(len(y_all) / (6 * np.maximum(1, np.bincount(y_all, minlength=6))), dtype=torch.float32, device=dev)
    net = Net().to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-2)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=2e-3, total_steps=EPOCHS * ((len(tr) + 7) // 8))
    lossf = nn.CrossEntropyLoss(weight=cw, ignore_index=-1)
    for ep in range(EPOCHS):
        net.train()
        order = np.random.permutation(len(tr))
        for b in range(0, len(tr), 8):
            X, E, Y = batch([tr[i] for i in order[b:b + 8]], mu, sd)
            loss = lossf(net(X, E).reshape(-1, 6), Y.reshape(-1))
            opt.zero_grad(); loss.backward(); nn.utils.clip_grad_norm_(net.parameters(), 1.0); opt.step(); sched.step()
    net.eval()
    return net, mu, sd


t0 = time.time()
for s in S:
    s["N"] = None
for k in range(10):
    tr = [s for s in S if int(s["f"]) != k]
    te = [s for s in S if int(s["f"]) == k]
    net, mu, sd = train(tr)
    with torch.no_grad():
        for s in te:
            X, E, _ = batch([s], mu, sd)
            s["N"] = torch.softmax(net(X, E)[0], -1).cpu().numpy()
    print(f"fold {k} done ({time.time() - t0:.0f} s)", flush=True)

y = np.concatenate([s["y"] for s in S]); w = np.concatenate([s["w"] for s in S]); d = np.concatenate([np.full(len(s["y"]), str(s["d"])) for s in S])
m = y >= 0


def score(name, preds):
    p = np.concatenate(preds)
    out = []
    for title, mask in (("all", m), ("2026-10-19", m & (d == "2026-10-19"))):
        out.append(f"{title}: {np.average((p == y)[mask], weights=w[mask]):.1%} / {np.average(((p == 3) == (y == 3))[mask], weights=w[mask]):.1%}")
    print(f"  {name:34s} " + "   ".join(out), flush=True)


print(f"10 folds by song, {n} songs (types / drop-not-drop)")
score("the rules", [s["r"] for s in S])
score("the trees (XGBoost), bars", [s["T"].argmax(1) for s in S])
score("the trees, as sections", [smooth(s["T"]) for s in S])
score("the neural net (GRU), bars", [s["N"].argmax(1) for s in S])
score("the neural net, as sections", [smooth(s["N"]) for s in S])
for a in (0.3, 0.5, 0.7):
    score(f"blend {a:.1f} net + {1 - a:.1f} trees, as sections", [smooth(a * s["N"] + (1 - a) * s["T"]) for s in S])
