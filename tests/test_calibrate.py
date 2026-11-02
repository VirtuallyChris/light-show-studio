"""`lightshow calibrate` (calibrate.py main): what it measures lands in the studio's own config.json. No speakers or
microphone here: the casting and the measuring are stood in for."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from lightshow.studio import calibrate


class SaveTheOffsetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "config.json")

    def calibrate(self, *argv, measured=(120, 130)):
        results = [{"offset_ms": ms, "spread_ms": 5, "n": 6} for ms in measured]
        with mock.patch.dict(os.environ, {"LSS_DATA_DIR": self.tmp.name}), \
                mock.patch("lightshow.studio.cast.CastManager"), \
                mock.patch("lightshow.govee_lan.local_ip", return_value="127.0.0.1"), \
                mock.patch.object(calibrate, "write_calibration_file"), \
                mock.patch.object(calibrate, "measure", side_effect=results), \
                mock.patch.object(calibrate.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            return calibrate.main(["Everywhere", "--runs", str(len(measured)), *argv])

    def test_the_offset_is_saved_beside_what_the_config_already_holds(self):
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump({"light_offset_ms": 30, "cast_offsets": {"Kitchen": 400}}, fh)
        self.assertEqual(self.calibrate(), 0)
        with open(self.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["cast_offset_ms"], 125)                     # the median of the runs
        self.assertEqual(cfg["cast_offsets"], {"Kitchen": 400, "Everywhere": 125})
        self.assertEqual(cfg["light_offset_ms"], 30)

    def test_no_config_yet_is_fine(self):
        self.assertEqual(self.calibrate(measured=(200,)), 0)
        with open(self.config, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cast_offsets"], {"Everywhere": 200})

    def test_no_save_writes_nothing(self):
        self.assertEqual(self.calibrate("--no-save"), 0)
        self.assertFalse(os.path.exists(self.config))


if __name__ == "__main__":
    unittest.main()
