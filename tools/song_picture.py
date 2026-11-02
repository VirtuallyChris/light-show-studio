r"""A song as a picture, to be looked at while labeling its sections: the full spectrogram (every pitch over time), the
waveform's loudness, the stems bar by bar (bass, drums, vocals, other), the kick and snare hits, a grid of numbered bars,
and (with --labels) the user's own sections drawn over it in color, with the labeler's guess made without that song
(the trials' folds, labeler_trials/nn_export.py) beneath. From the kept copy (shows/_party/copies).

    .venv\Scripts\python tools\song_picture.py <out dir> [--labels] <track id>... | --reviewed
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lightshow.studio import labeler as L  # noqa: E402
from lightshow.studio.party import PartyLibrary  # noqa: E402

COLORS = {"intro": "#9e9e9e", "verse": "#4f8fe0", "buildup": "#f2c230", "drop": "#e0412f", "breakdown": "#9c5fd6",
          "outro": "#555555"}
SR, HOP = 22050, 512


_OOF: dict = {}


def _oof() -> dict:
    if not _OOF:
        try:
            z = np.load(os.path.join(ROOT, "shows", "_party", "features", "trials", "nn_data.npz"))
            _OOF.update({str(z[f"id{i}"]): z[f"T{i}"] for i in range(int(z["n"])) if not int(z[f"c{i}"])})
        except (OSError, KeyError):
            _OOF["-"] = None
    return _OOF


def picture(lib, tid: str, out: str, labels: bool) -> bool:
    import librosa
    copy = os.path.join(lib.dir, "copies", tid + ".flac")
    s = L.song_bars(lib, tid)
    if s is None or not os.path.exists(copy):
        return False
    y, _ = librosa.load(copy, sr=SR, mono=True)
    dur = len(y) / SR
    mel = librosa.power_to_db(librosa.feature.melspectrogram(y=y, sr=SR, hop_length=HOP, n_mels=128, fmax=11000), ref=np.max)
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    spans, b = s["spans"], s["base"]
    doc = lib.learned(tid) or {}
    t = doc.get("track") or {}
    title = f"{', '.join(t.get('artists') or [])} - {t.get('name')}  ({len(spans)} bars, {dur:.0f} s)"

    fig, ax = plt.subplots(5, 1, figsize=(max(14, dur * 0.085), 9.5), sharex=True,
                           gridspec_kw={"height_ratios": [4, 1.1, 2.2, 0.9, 1.4 if labels else 0.25]})
    ax[0].imshow(mel, aspect="auto", origin="lower", extent=[0, len(rms) * HOP / SR, 0, 128], cmap="magma")
    ax[0].set_ylabel("pitch (mel)")
    ax[0].set_title(title, loc="left", fontsize=11)
    tt = np.arange(len(rms)) * HOP / SR
    ax[1].fill_between(tt, -rms, rms, color="#444")
    ax[1].set_ylabel("wave")
    ax[1].set_yticks([])
    lanes = np.stack([b[:, 1], b[:, 3], b[:, 4], b[:, 5], b[:, 0] / (np.quantile(b[:, 0], 0.9) + 1e-6)])
    for r, row in enumerate(lanes):
        for i, (b0, b1) in enumerate(spans):
            ax[2].add_patch(plt.Rectangle((b0, 4 - r), b1 - b0, 0.9, color=plt.cm.viridis(min(1.0, float(row[i])))))
    ax[2].set_ylim(0, 5)
    ax[2].set_yticks([4.45, 3.45, 2.45, 1.45, 0.45])
    ax[2].set_yticklabels(["bass", "drums", "vocals", "other", "loud%"])
    mid = spans.mean(1)
    w = (spans[:, 1] - spans[:, 0]) * 0.4
    ax[3].bar(mid - w / 2, b[:, 52], width=w, color="#e08a2f", label="kick")
    ax[3].bar(mid + w / 2, b[:, 51], width=w, color="#2fb5e0", label="snare")
    ax[3].set_ylabel("hits")
    ax[3].legend(loc="upper right", fontsize=7, ncol=2)
    if labels:
        tr = L.truth(lib.dir).get(tid) or {}
        for sec in tr.get("sections") or []:
            c = COLORS.get(sec["type"], "#fff")
            ax[4].add_patch(plt.Rectangle((sec["start"], 0), sec["end"] - sec["start"], 1, color=c))
            ax[4].text((sec["start"] + sec["end"]) / 2, 0.5, sec["type"], ha="center", va="center", fontsize=8, color="white")
            for a in ax[:4]:
                a.axvline(sec["start"], color="white" if a is ax[0] else "black", lw=1.2, alpha=0.8)
        oof = _oof().get(tid)
        if oof is not None and len(oof) == len(spans):
            ax[4].axhline(0, color="black", lw=1)
            for i, k in enumerate(L.smooth(oof)):
                ax[4].add_patch(plt.Rectangle((spans[i][0], -0.6), spans[i][1] - spans[i][0], 0.55, color=COLORS[L.TYPES[k]]))
        ax[4].set_ylabel("yours /\nlabeler")
    ax[4].set_ylim(-0.6 if labels else 0, 1)
    ax[4].set_yticks([])
    for i, (b0, _) in enumerate(spans):                       # the bar grid, numbered every 4 bars
        if i % 4 == 0:
            ax[0].axvline(b0, color="#00ffcc", lw=0.5, alpha=0.5)
            ax[0].text(b0, 129, str(i), fontsize=7, color="#006655", ha="center", va="bottom")
            for a in ax[1:4]:
                a.axvline(b0, color="#999", lw=0.4, alpha=0.5)
    ax[-1].set_xlim(0, dur)
    ax[-1].set_xlabel("seconds (bar numbers along the top)")
    fig.tight_layout()
    fig.savefig(out, dpi=72)
    plt.close(fig)
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    out_dir, labels = args[0], "--labels" in args
    ids = [a for a in args[1:] if not a.startswith("--")]
    lib = PartyLibrary(os.path.join(ROOT, "shows"))
    if "--reviewed" in args:
        ids = sorted(k for k, v in L.truth(lib.dir).items() if isinstance(v, dict) and v.get("by") != "claude")
    os.makedirs(out_dir, exist_ok=True)
    made = sum(picture(lib, tid, os.path.join(out_dir, tid + ".png"), labels) for tid in ids)
    print(f"{made} pictures in {out_dir}")
