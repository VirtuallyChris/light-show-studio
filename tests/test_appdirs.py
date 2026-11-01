import os
import sys
import tempfile
import unittest
from unittest import mock

from lightshow import appdirs
from lightshow import __main__ as entry


class DataDirTest(unittest.TestCase):
    def test_per_system_folders(self):
        self.assertEqual(appdirs.user_data_dir("darwin", "/Users/u"),
                         os.path.join("/Users/u", "Library", "Application Support", "Light Show Studio"))
        self.assertEqual(appdirs.user_data_dir("linux", "/home/u", {}),
                         os.path.join("/home/u", ".local", "share", "light-show-studio"))
        self.assertEqual(appdirs.user_data_dir("linux", "/home/u", {"XDG_DATA_HOME": "/data"}),
                         os.path.join("/data", "light-show-studio"))

    def test_from_source_the_project_folder(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LSS_DATA_DIR", None)
            self.assertEqual(appdirs.data_dir(), appdirs.SOURCE_ROOT)

    def test_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "data")
            with mock.patch.dict(os.environ, {"LSS_DATA_DIR": target}):
                self.assertEqual(appdirs.data_dir(), target)
            self.assertTrue(os.path.isdir(target))


class SelfCommandTest(unittest.TestCase):
    def test_source_runs_python_m(self):
        self.assertEqual(appdirs.self_command("bulbshow", "--port", "1", frozen=False, exe="py"), ["py", "-m", "bulbshow", "--port", "1"])

    def test_packaged_runs_itself(self):
        self.assertEqual(appdirs.self_command("bulbshow", "--port", "1", frozen=True, exe="app"),
                         ["app", "--run-module", "bulbshow", "--port", "1"])


class PopenOutsideTest(unittest.TestCase):
    def test_from_source_it_is_plain_popen(self):
        with mock.patch.object(appdirs.subprocess, "Popen", return_value="proc") as popen:
            self.assertEqual(appdirs.popen_outside(["x"], cwd="y"), "proc")
        popen.assert_called_once_with(["x"], cwd="y")


class PreparePackagedTest(unittest.TestCase):
    def test_brings_its_certificates_and_tells_the_models_where_the_data_is(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LSS_DATA_DIR": d}):
            os.environ.pop("SSL_CERT_FILE", None)
            entry.prepare_packaged()
            self.assertTrue(os.path.exists(os.environ["SSL_CERT_FILE"]))
            self.assertEqual(os.environ["LSS_DATA_DIR"], d)


class DispatchTest(unittest.TestCase):
    def test_run_module(self):
        with mock.patch("runpy.run_module") as run, mock.patch.object(sys, "argv", ["x"]):
            self.assertEqual(entry.dispatch(["--run-module", "bulbshow", "--no-browser"]), 0)
            run.assert_called_once_with("bulbshow", run_name="__main__", alter_sys=True)
            self.assertEqual(sys.argv, ["bulbshow", "--no-browser"])

    def test_packaged_double_click_opens_the_studio(self):
        with mock.patch("lightshow.cli.main", return_value=0) as main, mock.patch.object(sys, "frozen", True, create=True),                 mock.patch.object(entry, "prepare_packaged"):       # (it sets the environment for everything after)
            entry.dispatch([])
            main.assert_called_once_with(["studio"])

    def test_cli_otherwise(self):
        with mock.patch("lightshow.cli.main", return_value=0) as main:
            entry.dispatch(["devices"])
            main.assert_called_once_with(["devices"])


if __name__ == "__main__":
    unittest.main()
