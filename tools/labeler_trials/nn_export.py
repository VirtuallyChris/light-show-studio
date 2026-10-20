"""Export the reviewed songs' per-bar data for the neural-net trial (run with the studio's .venv): the trees' feature
matrix, the raw SongFormer embedding per bar, labels, the rules' labels, bar lengths; 10 folds by song; and the
trees' (XGBoost) out-of-fold probabilities on those same folds, to compare and to blend."""
import os
import sys

import numpy as np

ROOT = r"C:\Users\chris\Documents\light-show-studio"
sys.path.insert(0, ROOT)
from lightshow.studio import labeler  # noqa: E402
from lightshow.studio.jam import JamLibrary  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shows", "_jam", "features", "trials", "nn_data.npz")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
lib = JamLibrary(os.path.join(ROOT, "shows"))
songs = labeler.load_all(lib)
pcas = labeler._pca(songs)
rev = [s for s in songs if (s["y"] >= 0).any()]
names = sorted({str(s["name"]).lower() for s in rev})
rng = np.random.default_rng(0)
perm = rng.permutation(len(names))
fold_of_name = {names[i]: int(k % 10) for k, i in enumerate(perm)}
added = {k: v.get("added", "?") for k, v in labeler.truth(lib.dir).items()}
data = {"n": np.array(len(rev))}
for i, s in enumerate(rev):
    data[f"X{i}"] = labeler.matrix(s, pcas)
    data[f"E{i}"] = s["emb"].astype(np.float16)
    data[f"y{i}"] = s["y"]; data[f"r{i}"] = s["rule"]; data[f"w{i}"] = s["spans"][:, 1] - s["spans"][:, 0]
    data[f"f{i}"] = np.array(fold_of_name[str(s["name"]).lower()]); data[f"d{i}"] = np.array(added.get(s["tid"], "?"))
    data[f"id{i}"] = np.array(s["tid"]); data[f"sp{i}"] = s["spans"]
# the trees on the same folds
for k in range(10):
    tr = [s for s in rev if fold_of_name[str(s["name"]).lower()] != k]
    M = labeler.Model().fit(np.concatenate([labeler.matrix(s, pcas)[s["y"] >= 0] for s in tr]), np.concatenate([s["y"][s["y"] >= 0] for s in tr]))
    for i, s in enumerate(rev):
        if fold_of_name[str(s["name"]).lower()] == k:
            data[f"T{i}"] = M.proba(data[f"X{i}"])
    print("trees, fold", k, flush=True)
np.savez_compressed(OUT, **data)
print("exported", len(rev), "songs to", OUT)
