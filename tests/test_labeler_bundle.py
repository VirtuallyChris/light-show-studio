"""The section labeler that comes with the app (labeler_bundle): exported in portable files, loaded back with the same
answers, and used whenever a home has no labeler of its own. A tiny model on made-up numbers: no songs, no GPU."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from lightshow.studio import labeler, labeler_bundle


def tiny_trained():
    from sklearn.decomposition import PCA
    rng = np.random.default_rng(0)
    E = rng.normal(size=(200, 12))
    pcas = (PCA(4, random_state=0).fit(E), PCA(4, random_state=0).fit(E - E.mean(0)))
    X = rng.normal(size=(300, 7)).astype(np.float32)
    y = rng.integers(0, len(labeler.TYPES), 300)
    with mock.patch("lightshow.studio.devices.xgboost_device", return_value="cpu", create=True):
        m = labeler.Model()
        m.clf.set_params(n_estimators=10)
        m.fit(X, y)
    return {"model": m, "pcas": pcas, "songs": 140, "bars": 300, "trained": "2026-10-24 22:12"}, X, E


class TestBundledLabeler(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_exported_and_loaded_it_answers_the_same(self):
        trained, X, E = tiny_trained()
        meta = labeler_bundle.export(trained, self.dir)
        self.assertEqual((meta["songs"], meta["features"]), (140, 7))
        back = labeler_bundle.load(len(labeler.TYPES), self.dir)
        np.testing.assert_allclose(back["model"].proba(X), trained["model"].proba(X), atol=1e-5)
        for a, b in zip(back["pcas"], trained["pcas"]):
            np.testing.assert_allclose(a.transform(E), b.transform(E), atol=1e-4)

    def test_a_home_with_no_labeler_of_its_own_uses_the_bundled_one(self):
        trained, X, _ = tiny_trained()
        labeler_bundle.export(trained, self.dir)
        with mock.patch.object(labeler, "MODEL", os.path.join(self.dir, "none.joblib")), \
                mock.patch.object(labeler_bundle, "BUNDLED", self.dir), mock.patch.dict(labeler._BUNDLED, clear=True):
            m = labeler._load()
            self.assertTrue(m["bundled"])
            self.assertEqual(labeler.trained_on(), 140)       # its own model only once it has reviewed more than that

    def test_missing_files_are_no_model(self):
        self.assertIsNone(labeler_bundle.load(len(labeler.TYPES), os.path.join(self.dir, "nothing")))

    def test_the_app_ships_one(self):
        meta = labeler_bundle.load(len(labeler.TYPES))
        self.assertIsNotNone(meta, "lightshow/studio/data/section_labeler is missing")
        self.assertGreaterEqual(meta["songs"], 100)

    def test_a_library_that_will_not_load_only_means_no_labeler(self):
        class LibraryNotFound(Exception):                # (XGBoost's own, which is no ImportError or ValueError)
            pass
        broken = mock.Mock()
        broken.Booster.side_effect = LibraryNotFound("no xgboost.dll")
        with mock.patch.dict("sys.modules", {"xgboost": broken}):
            self.assertIsNone(labeler_bundle.load(5))


if __name__ == "__main__":
    unittest.main()
