"""The AI models' environment, installed from inside the app on first run.

The models (SongFormer, Demucs, the genre model, Beat This, ADTOF, Whisper) need PyTorch, which the studio's
own Python does not carry: they run in a model environment of their own (model_worker.py). Made by hand,
that is .venv-songformer (README). Made here, it is a standalone Python 3.12 (python-build-standalone,
relocatable, nothing installed system-wide) in model_worker.models_env_dir(), with the packages of the
requirements file for this machine:

    cuda      an NVIDIA card (Windows, Linux)                 PyTorch for CUDA 12.6
    rocm      an AMD card on Linux                            PyTorch for ROCm 7.2
    directml  an AMD (or Intel Arc) card on Windows           PyTorch 2.4 + torch-directml (best effort)
    mps       a Mac with Apple Silicon                        PyTorch from PyPI (Metal)
    cpu       anything else                                   PyTorch for the CPU (slow, fine for learn ahead)

Three phases: Python (download, checksum, unpack), packages (pip, a few GB) and models (the host starts and
loads every model, which downloads their weights). It all goes into a folder beside the final one and is
moved into place only once pip has finished, so the studio never finds a half-installed environment.

status() and post(body) are the API the app's setup screen calls (GET and POST /api/models/setup).
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request

from . import model_worker
from .model_worker import ROOT, models_env_dir, standalone_python

VARIANTS = ("cuda", "rocm", "directml", "mps", "cpu")

# python-build-standalone (github.com/astral-sh/python-build-standalone), pinned, with each file's SHA-256
# from the release's SHA256SUMS
PBS_TAG = "20261013"
PY_VERSION = "3.12.15"
PBS_SHA256 = {
    "x86_64-pc-windows-msvc": "bbc902d4d187ac4914915bf538196a064b49109a33a104965b5164ced5b80a1e",
    "aarch64-apple-darwin": "ba32036ad8f4c434d97a1ee7feef8b7edc0fac16777639b97e39c74a108e4440",
    "x86_64-unknown-linux-gnu": "5d546fc004fc2435fa142eb8f42ed8de5eb4725963dc22f7e6671014a80eb560",
    "aarch64-unknown-linux-gnu": "34a43010cc05982d8b5debbad7d09f7b1200117ebf018f7acfd656895e408205",
}
PBS_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
           "{tag}/cpython-{ver}+{tag}-{triple}-install_only.tar.gz")

EXPECTED_PACKAGES = 120          # about how many packages pip collects (for the progress bar only)


# -- this machine ------------------------------------------------------------------------------------------

def _machine(machine: str) -> str:
    m = machine.lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)


def python_triple(system: str | None = None, machine: str | None = None) -> str | None:
    """The python-build-standalone build for this OS and processor, or None where there is none we use."""
    system = system or platform.system()
    machine = _machine(machine or platform.machine())
    triple = {("Windows", "x86_64"): "x86_64-pc-windows-msvc",
              ("Darwin", "aarch64"): "aarch64-apple-darwin",
              ("Linux", "x86_64"): "x86_64-unknown-linux-gnu", ("Linux", "aarch64"): "aarch64-unknown-linux-gnu"}
    return triple.get((system, machine))


def python_url(triple: str) -> str:
    return PBS_URL.format(tag=PBS_TAG, ver=PY_VERSION, triple=triple)


def gpu_vendors(system: str | None = None) -> set[str]:
    """The graphics cards' makers: "nvidia", "amd", "intel" and "intel-arc" (a card of Intel's own, not
    the graphics built into the processor). Asked of the registry (Windows) or /sys (Linux)."""
    system = system or platform.system()
    found: set[str] = set()
    vendor_ids = {"10de": "nvidia", "1002": "amd", "8086": "intel"}
    if system == "Windows":
        try:
            import winreg
            key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"  # display adapters
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                            dev = str(winreg.QueryValueEx(sub, "MatchingDeviceId")[0]).lower()
                            desc = str(winreg.QueryValueEx(sub, "DriverDesc")[0])
                    except OSError:
                        continue
                    v = next((name for vid, name in vendor_ids.items() if f"ven_{vid}" in dev), "")
                    if v:
                        found.add("intel-arc" if v == "intel" and "arc" in desc.lower().split() else v)
        except OSError:
            pass
    elif system == "Linux":
        import glob
        for path in glob.glob("/sys/class/drm/card*/device/vendor"):
            try:
                with open(path, encoding="ascii") as fh:
                    v = vendor_ids.get(fh.read().strip().lower().removeprefix("0x"))
            except OSError:
                continue
            if v:
                found.add(v)
        if os.path.exists("/dev/kfd"):                   # the ROCm kernel driver
            found.add("amd")
    return found


def detect_variant(system: str | None = None, machine: str | None = None,
                   vendors: set[str] | None = None) -> tuple[str | None, str]:
    """(the requirements variant for this machine, or None where the models cannot run; why, in words)."""
    want = os.environ.get("LSS_MODELS_VARIANT", "").strip().lower()
    if want in VARIANTS:
        return want, "chosen by LSS_MODELS_VARIANT"
    system = system or platform.system()
    machine = _machine(machine or platform.machine())
    if system == "Darwin":
        if machine == "aarch64":
            return "mps", "Apple Silicon (Metal)"
        return None, "PyTorch no longer makes builds for Intel Macs"
    if system == "Windows" and machine == "aarch64":
        return None, "the models' packages have no builds for Windows on ARM"
    if python_triple(system, machine) is None:
        return None, f"no Python build for {system} on {machine}"
    if vendors is None:
        vendors = gpu_vendors(system)
    if "nvidia" in vendors and machine == "x86_64":
        return "cuda", "an NVIDIA card (CUDA)"
    if system == "Linux" and machine == "x86_64" and "amd" in vendors:
        return "rocm", "an AMD card (ROCm)"
    if system == "Windows" and machine == "x86_64" and vendors & {"amd", "intel-arc"}:
        return "directml", "an AMD or Intel card (DirectML, best effort)"
    return "cpu", "no graphics card PyTorch can use here: the CPU (slower)"


def requirements_file(variant: str) -> str:
    return os.path.join(ROOT, f"requirements-models-{variant}.txt")


def installed() -> bool:
    """True when a model environment is there (made by hand or by the installer)."""
    return os.path.exists(model_worker.venv_py())


# -- the installer -----------------------------------------------------------------------------------------

class Cancelled(Exception):
    pass


class Installer:
    """One install at a time, on a thread of its own; status() is where it stands."""

    PHASES = {"python": (0.0, 0.08), "packages": (0.08, 0.84), "songs": (0.84, 0.92), "models": (0.92, 1.0)}   # share of the bar

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_flag = threading.Event()
        self.detected: tuple[str | None, str] | None = None
        self._set_idle()

    def _set_idle(self) -> None:
        self.state = "idle"                  # idle | running | done | failed | cancelled
        self.variant, self.why = None, ""
        self.phase, self.step, self.line, self.error = "", "", "", ""
        self.songs_error = ""                # song learning would not install; the models still work
        self.fraction = 0.0                  # of the current phase
        self.started = self.finished = 0.0

    # (stand-ins for tests: the network and pip)
    def fetch(self, url: str, dest: str, progress) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "LightShowStudio"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
            total, got = int(r.headers.get("Content-Length") or 0), 0
            while True:
                self._check()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                progress(got, total)

    def run(self, cmd: list[str], on_line) -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = model_worker.popen_outside(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=model_worker.child_env(),
                                     text=True, encoding="utf-8", errors="replace", creationflags=flags)
        for line in self.proc.stdout:
            on_line(line.rstrip())
        return self.proc.wait()

    def warm(self) -> None:
        if not model_worker.HOST.connect():
            raise RuntimeError(f"the models would not start: {model_worker.HOST.error or 'see models.log'}")
        model_worker.HOST.warm_all()        # the weights download as each model loads; /api/models shows it

    def status(self) -> dict:
        lo, hi = self.PHASES.get(self.phase, (0.0, 0.0))
        progress = 1.0 if self.state == "done" else lo + (hi - lo) * min(1.0, self.fraction)
        variant, why = (self.variant, self.why) if self.variant else self.detect()
        return {"state": self.state, "installed": installed(), "variant": variant, "why": why,
                "phase": self.phase, "step": self.step, "line": self.line, "error": self.error, "songs_error": self.songs_error,
                "progress": round(progress, 3), "where": models_env_dir(), "python": model_worker.venv_py(),
                "seconds": round((self.finished or time.time()) - self.started, 1) if self.started else 0}

    def detect(self) -> tuple[str | None, str]:
        """detect_variant(), asked once (the registry or /sys, not on every status call)."""
        if self.detected is None:
            self.detected = detect_variant()
        return self.detected

    def start(self, variant: str | None = None) -> bool:
        """Start installing (False if an install is running already, or the variant is unknown)."""
        with self.lock:
            if self.state == "running":
                return False
            why = "chosen"
            if variant is None:
                variant, why = self.detect()
            if variant not in VARIANTS:
                self.state, self.error = "failed", why if variant is None else f"unknown variant {variant!r}"
                return False
            self._set_idle()
            self.state, self.variant, self.why, self.started = "running", variant, why, time.time()
            self.cancel_flag.clear()
            self.thread = threading.Thread(target=self._install, daemon=True, name="models-setup")
            self.thread.start()
            return True

    def cancel(self) -> None:
        self.cancel_flag.set()
        p = self.proc
        if p is not None and p.poll() is None:
            p.kill()

    def _check(self) -> None:
        if self.cancel_flag.is_set():
            raise Cancelled()

    def _install(self) -> None:
        root = models_env_dir()
        work = os.path.join(root, "python.new")          # moved to root/python once everything is in
        try:
            os.makedirs(root, exist_ok=True)
            shutil.rmtree(work, ignore_errors=True)
            py = self._python(root, work)
            self._packages(py)
            final = os.path.join(root, "python")
            if os.path.exists(final):                    # installed again: the host may be running from it
                model_worker.HOST.kill()
                shutil.rmtree(final)
            os.replace(work, final)
            model_worker.note(f"[models-setup] installed ({self.variant}) in {final}")
            self._songs(standalone_python(final))
            self.phase, self.step, self.fraction = "models", "Loading the models (their weights download now)", 0.0
            self.warm()
            self.fraction, self.state = 1.0, "done"
        except Cancelled:
            self.state, self.step = "cancelled", "Cancelled"
        except Exception as e:  # noqa: BLE001 - shown on the setup screen
            self.state, self.error = "failed", f"{type(e).__name__}: {e}"
            model_worker.note(f"[models-setup] failed in {self.phase or 'setup'}: {self.error}")
        finally:
            self.proc = None
            self.finished = time.time()
            if self.state != "done":
                shutil.rmtree(work, ignore_errors=True)

    def start_songs(self) -> bool:
        """Only the song learning (a computer that has the models already)."""
        with self.lock:
            if self.state == "running":
                return False
            self._set_idle()
            self.state, self.started = "running", time.time()
            self.cancel_flag.clear()
            self.thread = threading.Thread(target=self._install_songs, daemon=True, name="songs-setup")
            self.thread.start()
            return True

    def _install_songs(self) -> None:
        try:
            self._songs(model_worker.venv_py())
            self.fraction, self.state = 1.0, "done"
        except Cancelled:
            self.state, self.step = "cancelled", "Cancelled"
        finally:
            self.proc = None
            self.finished = time.time()

    def _songs(self, py: str) -> None:
        """What learning songs ahead needs: spotDL in an environment of its own, with Deno and ffmpeg. If this fails the
        models still work, and songs are learned as they play, so it only says so."""
        from . import fetch_ahead
        self.phase, self.step, self.fraction = "songs", "Setting up song learning", 0.0
        venv = os.path.dirname(os.path.dirname(fetch_ahead.SPOTDL))
        scripts = os.path.dirname(fetch_ahead.SPOTDL)
        python = model_worker.venv_python(venv)
        steps = [("the environment", [py, "-I", "-m", "venv", venv]),
                 ("spotDL", [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off", f"spotdl=={SPOTDL_VERSION}"])]
        home = fetch_ahead.SPOTDL_HOME
        exe = ".exe" if os.name == "nt" else ""
        # (each asks before replacing a copy it already has, and there is nobody to answer)
        if not os.path.isfile(os.path.join(home, "deno" + exe)):
            steps.append(("Deno", [fetch_ahead.SPOTDL, "--download-deno"]))
        if not fetch_ahead.ffmpeg_path():
            steps.append(("ffmpeg", [fetch_ahead.SPOTDL, "--download-ffmpeg"]))
        try:
            for i, (name, cmd) in enumerate(steps):
                self.fraction = i / len(steps)
                self._check()
                code = self.run(cmd, self._song_line)
                self._check()
                if code != 0:
                    raise RuntimeError(f"{name} would not install (exit {code}): {self.line}")
            if not fetch_ahead.available():
                raise RuntimeError(f"spotDL or ffmpeg is missing after installing (looked in {scripts} and {home})")
            model_worker.note("[models-setup] song learning installed")
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - said on the setup screen
            self.songs_error = f"{type(e).__name__}: {e}"
            model_worker.note(f"[models-setup] song learning: {self.songs_error}")
        self.fraction = 1.0

    def _song_line(self, line: str) -> None:
        self._check()
        if line:
            self.line = line[-300:]

    def _python(self, root: str, work: str) -> str:
        """Download, check and unpack the standalone Python into work; its interpreter."""
        self.phase, self.step, self.fraction = "python", f"Downloading Python {PY_VERSION}", 0.0
        triple = python_triple()
        if triple is None:
            raise RuntimeError(f"no Python build for {platform.system()} on {platform.machine()}")
        archive = os.path.join(root, f"python-{PY_VERSION}-{triple}.tar.gz")

        def progress(got, total):
            self.fraction = 0.9 * got / total if total else 0.0

        try:
            self.fetch(python_url(triple), archive, progress)
            digest = hashlib.sha256()
            with open(archive, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != PBS_SHA256[triple]:
                raise RuntimeError("the Python download is damaged (its checksum does not match)")
            self._check()
            self.step = f"Unpacking Python {PY_VERSION}"
            tmp = work + ".unpack"
            shutil.rmtree(tmp, ignore_errors=True)
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmp, filter="data")
            os.replace(os.path.join(tmp, "python"), work)     # (the archive holds one folder, python/)
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            try:
                os.remove(archive)
            except OSError:
                pass
        self.fraction = 1.0
        return standalone_python(work)

    def _packages(self, py: str) -> None:
        """pip-install the variant's requirements into the new Python."""
        self.phase, self.step, self.fraction = "packages", "Installing PyTorch and the models' packages", 0.0
        req = requirements_file(self.variant)
        if not os.path.exists(req):
            raise RuntimeError(f"{os.path.basename(req)} is missing")
        seen = [0]

        def on_line(line: str) -> None:
            self._check()
            if line:
                self.line = line[-300:]
            if line.startswith("Collecting "):
                seen[0] += 1
                self.fraction = min(0.95, seen[0] / EXPECTED_PACKAGES)
            elif line.startswith("Installing collected packages"):
                self.step, self.fraction = "Putting the packages in place", 0.97

        cmd = [py, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
               "--no-warn-script-location", "-r", req]
        code = self.run(cmd, on_line)
        self._check()
        if code != 0:
            raise RuntimeError(f"pip could not install the packages (exit {code}): {self.line}")
        # Installed is not the same as working: a missing system library only shows when PyTorch loads
        self.step = "Checking that PyTorch runs"
        code = self.run([py, "-I", "-c", "import torch; torch.zeros(1)"], on_line)
        self._check()
        if code != 0:
            raise RuntimeError(f"PyTorch installed but will not run on this computer: {self.line}")
        self.fraction = 1.0


INSTALLER = Installer()
SPOTDL_VERSION = "4.5.2"
MIN_FREE_GB = 15                         # PyTorch and the models' weights, with room to spare


def install_on_first_run(load_config, packaged: bool, live: bool, installer=None, free_gb=None) -> bool:
    """A downloaded app installs the models by itself, in the background, the first time it opens (and again next time if
    that didn't finish). Not from source, not in a test copy, not after Cancel, and not on a nearly full disk: then the
    button in Settings is still there. The app works meanwhile, with its own analysis."""
    installer = installer or INSTALLER
    if not packaged or not live or load_config().get("models_auto_install", True) is False:
        return False
    if installed():                       # (from a version before song learning came with the models)
        from . import fetch_ahead
        return False if fetch_ahead.available() else installer.start_songs()
    if installer.detect()[0] is None:
        return False
    free = shutil.disk_usage(model_worker.data_dir()).free / 1e9 if free_gb is None else free_gb
    if free < MIN_FREE_GB:
        model_worker.note(f"[models-setup] not installing by itself: {free:.0f} GB free, needs {MIN_FREE_GB}")
        return False
    return installer.start()


def status() -> dict:
    """GET /api/models/setup: where the install stands, the variant for this machine, and whether a model
    environment is there at all."""
    return INSTALLER.status()


def post(body: dict) -> tuple[dict, int]:
    """POST /api/models/setup: {"action": "install", "variant": optional} or {"action": "cancel"}.
    (the reply, its HTTP status)"""
    action = str((body or {}).get("action") or "install")
    if action == "cancel":
        INSTALLER.cancel()
        return INSTALLER.status(), 200
    if action != "install":
        return {"error": f"unknown action {action!r}"}, 400
    variant = (body or {}).get("variant") or None
    if not INSTALLER.start(variant):
        return {**INSTALLER.status(), "error": INSTALLER.error or "an install is running already"}, 409
    return INSTALLER.status(), 202
