"""Where the models run (devices.py), the model environment's layout (model_worker.py) and its installer
(model_setup.py), with stand-in torch, XGBoost, CTranslate2, downloads and pip: no GPU, no network."""
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

from lightshow.studio import devices
from lightshow.studio import model_setup as ms
from lightshow.studio import model_worker as mw
from lightshow.studio import models_host as mh


def fake_torch(cuda=False, mps=False, hip=None):
    return types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: cuda),
                                 backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps)),
                                 version=types.SimpleNamespace(hip=hip))


def fake_directml(count=1):
    return types.SimpleNamespace(device_count=lambda: count, device=lambda: "privateuseone:0")


class Fresh(unittest.TestCase):
    """devices' memory of what it found and pinned, cleared around each test."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("LSS_MODELS_DEVICE", None)
        os.environ.pop("LSS_MODELS_VARIANT", None)
        devices._FOUND.clear()
        devices._PINNED.clear()
        devices._XGB.clear()

    def tearDown(self):
        self.env.stop()
        devices._FOUND.clear()
        devices._PINNED.clear()
        devices._XGB.clear()

    def modules(self, **mods):
        """sys.modules with these stand-ins (None: the module cannot be imported)."""
        return mock.patch.dict(sys.modules, mods)


class TestBestDevice(Fresh):
    def test_order_cuda_then_mps_then_directml_then_cpu(self):
        cases = [(fake_torch(cuda=True, mps=True), fake_directml(), "cuda"),
                 (fake_torch(mps=True), fake_directml(), "mps"),
                 (fake_torch(), fake_directml(), "directml"),
                 (fake_torch(), fake_directml(count=0), "cpu"),
                 (fake_torch(), None, "cpu")]
        for torch, dml, want in cases:
            devices._FOUND.clear()
            with self.modules(torch=torch, torch_directml=dml):
                self.assertEqual(devices.best_device(), want)

    def test_no_torch_at_all_is_the_cpu(self):
        with self.modules(torch=None):
            self.assertEqual(devices.best_device("genre_infer"), "cpu")

    def test_torch_device_names(self):
        self.assertEqual(devices.torch_device("cuda"), "cuda:0")
        self.assertEqual(devices.torch_device("mps"), "mps")
        self.assertEqual(devices.torch_device("cpu"), "cpu")
        with self.modules(torch_directml=fake_directml()):
            self.assertEqual(devices.torch_device("directml"), "privateuseone:0")

    def test_known_bad_models_stay_on_the_cpu(self):
        with self.modules(torch=fake_torch(), torch_directml=fake_directml()):
            self.assertEqual(devices.best_device("stems_infer"), "cpu")
            self.assertEqual(devices.best_device("lyrics.demucs"), "cpu")
            self.assertEqual(devices.best_device("genre_infer"), "directml")
            self.assertEqual(devices.best_device("lyrics"), "directml")

    def test_a_pinned_model_and_its_parts_run_on_the_cpu(self):
        with self.modules(torch=fake_torch(mps=True)):
            self.assertTrue(devices.pin_to_cpu("lyrics"))
            self.assertEqual(devices.best_device("lyrics"), "cpu")
            self.assertEqual(devices.best_device("lyrics.demucs"), "cpu")
            self.assertEqual(devices.best_device("drums_infer"), "mps")
            self.assertFalse(devices.pin_to_cpu("lyrics"))          # (already there: no change)

    def test_the_environment_overrides_the_choice(self):
        os.environ["LSS_MODELS_DEVICE"] = "cpu"
        with self.modules(torch=fake_torch(cuda=True)):
            self.assertEqual(devices.best_device("songformer_infer"), "cpu")

    def test_falling_back_is_for_cards_other_than_nvidia(self):
        for torch, want in ((fake_torch(cuda=True), False), (fake_torch(cuda=True, hip="7.2"), True),
                            (fake_torch(mps=True), True), (fake_torch(), False)):
            devices._FOUND.clear()
            with self.modules(torch=torch, torch_directml=None):
                self.assertEqual(devices.may_fall_back(), want)

    def test_whisper_on_nvidia_only_else_cpu_int8(self):
        ct2 = types.SimpleNamespace(get_cuda_device_count=lambda: 1)
        with self.modules(torch=fake_torch(cuda=True), ctranslate2=ct2):
            self.assertEqual(devices.whisper_device(), ("cuda", "float16"))
        ct2.get_cuda_device_count = lambda: 0                        # a ROCm PyTorch says cuda; CTranslate2 sees no card
        with self.modules(torch=fake_torch(cuda=True, hip="7.2"), ctranslate2=ct2):
            self.assertEqual(devices.whisper_device(), ("cpu", "int8"))
        devices._FOUND.clear()
        with self.modules(torch=fake_torch(mps=True), ctranslate2=ct2):
            self.assertEqual(devices.whisper_device(), ("cpu", "int8"))


class TestXGBoost(Fresh):
    def xgb(self, cuda):
        return types.SimpleNamespace(build_info=lambda: {"USE_CUDA": cuda})

    def test_cuda_only_with_a_cuda_build_and_a_card(self):
        for built, cards, want in ((True, 1, "cuda"), (True, 0, "cpu"), (False, 1, "cpu"), ("OFF", 1, "cpu"), ("ON", 1, "cuda")):
            devices._XGB.clear()
            with self.modules(xgboost=self.xgb(built)), mock.patch.object(devices, "nvidia_gpus", return_value=cards):
                self.assertEqual(devices.xgboost_device(), want, (built, cards))

    def test_no_xgboost_is_the_cpu(self):
        with self.modules(xgboost=None):
            self.assertEqual(devices.xgboost_device(), "cpu")

    def test_the_labeler_trains_on_the_cpu_without_a_card(self):
        from lightshow.studio import labeler
        made = {}

        class XGBClassifier:
            def __init__(self, **kw):
                made.update(kw)

        xgb = types.SimpleNamespace(XGBClassifier=XGBClassifier, build_info=lambda: {"USE_CUDA": True})
        with self.modules(xgboost=xgb), mock.patch.object(devices, "nvidia_gpus", return_value=0):
            labeler.Model()
        self.assertEqual(made["device"], "cpu")


class TestModelFallsBackToCpu(Fresh):
    """A model whose warm-up (or load) fails on its card is loaded again on the CPU, except on NVIDIA."""

    def run_model(self, torch, warm_fails_on):
        loads = []

        def load():
            loads.append(devices.best_device("fake"))
            return loads[-1]

        def handle(state, req, payload):
            if state == warm_fails_on:
                raise RuntimeError("operator not implemented for this backend")
            return {}, b""

        with self.modules(torch=torch, torch_directml=None), \
                mock.patch.dict(mh.LOADERS, {"fake": load}), mock.patch.dict(mh.HANDLERS, {"fake": handle}), \
                mock.patch.object(mh, "_log"), mock.patch.object(mh.CARD, "set_cap"):
            m = mh.Model.__new__(mh.Model)                           # (no thread of its own: _load called here)
            m.name, m.state, m.load_error, m.phase = "fake", None, "", "loading"
            self.assertTrue(m._load())
            return m, loads

    def test_apple_moves_a_failing_model_to_the_cpu(self):
        m, loads = self.run_model(fake_torch(mps=True), warm_fails_on="mps")
        self.assertEqual(loads, ["mps", "cpu"])
        self.assertEqual((m.state, m.phase), ("cpu", "ready"))

    def test_nvidia_keeps_the_model_on_the_card(self):
        m, loads = self.run_model(fake_torch(cuda=True), warm_fails_on="cuda")
        self.assertEqual(loads, ["cuda"])
        self.assertEqual(m.phase, "ready")

    def test_a_load_failing_on_the_card_is_tried_on_the_cpu(self):
        calls = []

        def load():
            calls.append(devices.best_device("fake"))
            if calls[-1] != "cpu":
                raise RuntimeError("no kernel image for this device")
            return "cpu"

        with self.modules(torch=fake_torch(cuda=True, hip="7.2"), torch_directml=None), \
                mock.patch.dict(mh.LOADERS, {"fake": load}), mock.patch.dict(mh.HANDLERS, {"fake": lambda *a: ({}, b"")}), \
                mock.patch.object(mh, "_log"), mock.patch.object(mh.CARD, "set_cap"):
            m = mh.Model.__new__(mh.Model)
            m.name, m.state, m.load_error, m.phase = "fake", None, "", "loading"
            self.assertTrue(m._load())
        self.assertEqual(calls, ["cuda", "cpu"])

    def test_a_missing_package_is_not_retried(self):
        def load():
            raise ImportError("No module named 'demucs'")

        with self.modules(torch=fake_torch(mps=True)), mock.patch.dict(mh.LOADERS, {"fake": load}), \
                mock.patch.object(mh, "_log"):
            m = mh.Model.__new__(mh.Model)
            m.name, m.state, m.load_error, m.phase = "fake", None, "", "loading"
            self.assertFalse(m._load())
        self.assertEqual(m.phase, "failed")
        self.assertFalse(devices.pinned("fake"))


class TestEnvLayouts(unittest.TestCase):
    def test_venv_and_standalone_pythons_per_os(self):
        self.assertEqual(mw.venv_python("C:\\v", "win32"), "C:\\v\\Scripts\\python.exe")
        self.assertEqual(mw.venv_python("/v", "linux"), "/v/bin/python")
        self.assertEqual(mw.venv_python("/v", "darwin"), "/v/bin/python")
        self.assertEqual(mw.standalone_python("C:\\p", "win32"), "C:\\p\\python.exe")
        self.assertEqual(mw.standalone_python("/p", "linux"), "/p/bin/python3")

    def test_the_hand_made_env_is_looked_for_first(self):
        for plat in ("win32", "linux", "darwin"):
            first, second = mw.candidates(plat)
            self.assertIn(".venv-songformer", first)
            self.assertTrue(second.startswith(mw.models_env_dir()), second)

    def test_the_installer_folder_is_the_project_from_source_and_app_data_when_frozen(self):
        self.assertEqual(mw.models_env_dir(), os.path.join(mw.ROOT, "models-env"))
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(mw, "local_appdata", return_value="/data"):
            self.assertEqual(mw.models_env_dir(), os.path.join("/data", "LightShowStudio", "models-env"))

    def test_base_python_windows_layout(self):
        with tempfile.TemporaryDirectory() as d:
            home, venv = os.path.join(d, "Python312"), os.path.join(d, "venv")
            os.makedirs(home)
            open(os.path.join(home, "python.exe"), "w").close()
            os.makedirs(os.path.join(venv, "Lib", "site-packages"))
            with open(os.path.join(venv, "pyvenv.cfg"), "w") as fh:
                fh.write(f"home = {home}\ninclude-system-site-packages = false\nversion = 3.12.10\n")
            got = mw._base_python(os.path.join(venv, "Scripts", "python.exe"), "win32")
            self.assertEqual(got, (os.path.join(home, "python.exe"), os.path.join(venv, "Lib", "site-packages")))

    def test_base_python_posix_layout(self):
        with tempfile.TemporaryDirectory() as d:
            d = d.replace("\\", "/")
            home, venv = d + "/usr/bin", d + "/venv"
            os.makedirs(home)
            open(home + "/python3.12", "w").close()
            os.makedirs(venv + "/lib/python3.12/site-packages")
            with open(venv + "/pyvenv.cfg", "w") as fh:
                fh.write(f"home = {home}\nversion_info = 3.12.7.final.0\n")
            got = mw._base_python(venv + "/bin/python", "linux")
            self.assertEqual(got, (home + "/python3.12", venv + "/lib/python3.12/site-packages"))

    def test_a_standalone_python_has_no_base_to_find(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(mw._base_python(os.path.join(d, "python", "python.exe"), "win32"))


class TestVariant(Fresh):
    def test_each_kind_of_machine(self):
        cases = [("Darwin", "arm64", set(), "mps"),
                 ("Darwin", "x86_64", set(), None),
                 ("Windows", "AMD64", {"nvidia", "intel"}, "cuda"),
                 ("Linux", "x86_64", {"nvidia"}, "cuda"),
                 ("Linux", "x86_64", {"amd"}, "rocm"),
                 ("Windows", "AMD64", {"amd"}, "directml"),
                 ("Windows", "AMD64", {"intel-arc", "intel"}, "directml"),
                 ("Windows", "AMD64", {"intel"}, "cpu"),
                 ("Windows", "ARM64", set(), None),
                 ("Linux", "aarch64", {"amd"}, "cpu"),
                 ("Linux", "x86_64", set(), "cpu"),
                 ("FreeBSD", "amd64", set(), None)]
        for system, machine, vendors, want in cases:
            got, why = ms.detect_variant(system, machine, vendors)
            self.assertEqual(got, want, (system, machine, vendors, why))
            self.assertTrue(why)

    def test_the_environment_overrides_the_choice(self):
        os.environ["LSS_MODELS_VARIANT"] = "cpu"
        self.assertEqual(ms.detect_variant("Linux", "x86_64", {"nvidia"})[0], "cpu")

    def test_every_variant_has_a_requirements_file_with_torch(self):
        for v in ms.VARIANTS:
            with open(ms.requirements_file(v), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("torch==", text, v)
            self.assertIn("-r requirements-models-common.txt", text, v)

    def test_the_python_download_for_each_platform(self):
        for system, machine in (("Windows", "AMD64"), ("Darwin", "arm64"), ("Linux", "x86_64"), ("Linux", "aarch64")):
            triple = ms.python_triple(system, machine)
            self.assertIn(triple, ms.PBS_SHA256)
            self.assertTrue(ms.python_url(triple).endswith(f"{triple}-install_only.tar.gz"))
        self.assertIsNone(ms.python_triple("Darwin", "x86_64"))


class FakeInstaller(ms.Installer):
    """The installer with a made-up Python archive, pip and warm-up."""

    def __init__(self, pip_code=0, damaged=False):
        super().__init__()
        self.pip_code, self.damaged, self.cmds, self.warmed = pip_code, damaged, [], 0
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            name = "python/python.exe" if sys.platform == "win32" else "python/bin/python3"
            info = tarfile.TarInfo(name)
            info.size = 4
            tar.addfile(info, io.BytesIO(b"fake"))
        self.archive = buf.getvalue()

    def fetch(self, url, dest, progress):
        with open(dest, "wb") as fh:
            fh.write(self.archive + (b"x" if self.damaged else b""))
        progress(len(self.archive), len(self.archive))

    def run(self, cmd, on_line):
        self.cmds.append(cmd)
        for line in ("Collecting torch==2.14.0+cu126", "Collecting demucs==4.1.0", "Installing collected packages: torch, demucs"):
            on_line(line)
        return self.pip_code

    def warm(self):
        self.warmed += 1


class TestInstaller(Fresh):
    def install(self, inst, variant="cuda"):
        with tempfile.TemporaryDirectory() as d:
            sha = {ms.python_triple(): hashlib.sha256(inst.archive).hexdigest()}
            with mock.patch.object(ms, "models_env_dir", return_value=d), mock.patch.dict(ms.PBS_SHA256, sha), \
                    mock.patch.object(ms.model_worker, "note"):
                self.assertTrue(inst.start(variant))
                inst.thread.join(10)
                st = inst.status()
                left = sorted(os.listdir(d))
        return st, left

    def test_a_good_install_ends_with_python_in_place(self):
        inst = FakeInstaller()
        st, left = self.install(inst)
        self.assertEqual((st["state"], st["variant"], st["progress"], st["error"]), ("done", "cuda", 1.0, ""))
        self.assertEqual(left, ["python"])                             # (the archive and the work folder gone)
        cmd = inst.cmds[0]
        self.assertTrue(cmd[0].endswith(ms.standalone_python("python.new")), cmd[0])   # (pip ran in the work folder)
        self.assertEqual(cmd[-1], ms.requirements_file("cuda"))
        self.assertEqual(inst.warmed, 1)

    def test_a_damaged_download_stops_before_pip(self):
        inst = FakeInstaller(damaged=True)
        st, left = self.install(inst)
        self.assertEqual(st["state"], "failed")
        self.assertIn("checksum", st["error"])
        self.assertEqual((inst.cmds, left), ([], []))

    def test_pip_failing_leaves_no_half_installed_python(self):
        inst = FakeInstaller(pip_code=1)
        st, left = self.install(inst, "directml")
        self.assertEqual((st["state"], st["phase"]), ("failed", "packages"))
        self.assertIn("Installing collected packages", st["error"])
        self.assertEqual(left, [])
        self.assertEqual(inst.warmed, 0)

    def test_an_unknown_variant_is_refused(self):
        inst = FakeInstaller()
        self.assertFalse(inst.start("vulkan"))
        self.assertEqual(inst.state, "failed")

    def test_the_api(self):
        with mock.patch.object(ms, "INSTALLER", FakeInstaller()) as inst, mock.patch.object(inst, "start", return_value=True):
            reply, code = ms.post({"action": "install", "variant": "cpu"})
            self.assertEqual(code, 202)
            inst.start.assert_called_once_with("cpu")
            self.assertEqual(ms.post({"action": "dance"})[1], 400)
            self.assertIn("installed", ms.status())


if __name__ == "__main__":
    unittest.main()
