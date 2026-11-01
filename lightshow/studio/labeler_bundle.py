"""The section labeler that comes with the app: the model trained on the author's reviewed songs, kept in portable files
(XGBoost's own format and the PCA numbers as plain arrays) so it loads on any machine and any library version.

A home's own model (labeler.MODEL, trained from its own reviews) is used instead once it exists; it is only trained
once that home has reviewed more songs than this one was trained on (labeler.retrain_soon), so a handful of reviews
never replaces it with a weaker one.

    lightshow/studio/data/section_labeler/
        model.ubj     the XGBoost booster (multi:softprob over labeler.TYPES)
        pca.npz       the two PCAs' means and components (raw and centered embeddings)
        meta.json     {"classes", "songs", "bars", "trained", "features"}
"""

from __future__ import annotations

import json
import os

import numpy as np

BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "section_labeler")


class PCA:
    """sklearn's PCA.transform without sklearn (whiten off): (X - mean) @ components.T."""

    def __init__(self, mean: np.ndarray, components: np.ndarray):
        self.mean, self.components = mean.astype(np.float32), components.astype(np.float32)

    def transform(self, X):
        return (np.asarray(X, np.float32) - self.mean) @ self.components.T


class Model:
    """The booster's class probabilities, laid out over all of labeler.TYPES like labeler.Model.proba."""

    def __init__(self, booster, classes: list[int], n_types: int):
        self.booster, self.classes, self.n_types = booster, np.asarray(classes, int), n_types

    def proba(self, X):
        import xgboost as xgb
        p = self.booster.predict(xgb.DMatrix(np.asarray(X, np.float32)))
        P = np.zeros((len(X), self.n_types))
        P[:, self.classes] = p.reshape(len(X), -1)
        return P


def export(trained: dict, dst: str = BUNDLED) -> dict:
    """Write a trained labeler (as labeler.train saves it: {model, pcas, songs, bars, trained}) as the bundled one."""
    os.makedirs(dst, exist_ok=True)
    booster = trained["model"].clf.get_booster()
    booster.save_model(os.path.join(dst, "model.ubj"))
    r, c = trained["pcas"]
    np.savez_compressed(os.path.join(dst, "pca.npz"), r_mean=r.mean_, r_comp=r.components_, c_mean=c.mean_, c_comp=c.components_)
    meta = {"classes": [int(x) for x in trained["model"].classes_], "songs": int(trained["songs"]), "bars": int(trained["bars"]),
            "trained": trained["trained"], "features": int(booster.num_features())}
    with open(os.path.join(dst, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    return meta


def load(n_types: int, src: str = BUNDLED) -> dict | None:
    """The bundled labeler as labeler._load gives a trained one ({model, pcas, songs, ...}), or None if it is missing."""
    try:
        with open(os.path.join(src, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        import xgboost as xgb
        booster = xgb.Booster()
        booster.load_model(os.path.join(src, "model.ubj"))
        z = np.load(os.path.join(src, "pca.npz"))
    except (OSError, ValueError, ImportError):
        return None
    return {**meta, "bundled": True, "model": Model(booster, meta["classes"], n_types),
            "pcas": (PCA(z["r_mean"], z["r_comp"]), PCA(z["c_mean"], z["c_comp"]))}
