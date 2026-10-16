"""A long-lived model process in the separate model environment, shared by everything.

The genre model and SongFormer both run in their own Python environment (CUDA
PyTorch), not in the studio's. Starting one takes seconds, so each runs as a
worker that loads its model once and then answers requests over stdin/stdout:
one JSON line in (a file path, or a sample count followed by raw float32
audio), one JSON line out. Audio from memory is piped, never written to disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

from .priority import POPEN_BELOW_NORMAL
from .winpaths import local_appdata

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The model environment lives in the project folder (.venv-songformer), the folder the studio runs from,
# and nowhere else. It used to be under AppData\Local, but a folder made there by a program Windows runs
# as a packaged app (the Claude desktop app's terminal) is quietly redirected into that app's own storage:
# that program and everything it starts see it, the studio started from its shortcut does not, and the
# models were missing from the studio for days without a word.
_CANDIDATES = [
    os.path.join(ROOT, ".venv-songformer", "Scripts", "python.exe"),
]


def venv_py() -> str:
    """The model environment's Python, looked for each time (it may be installed while the studio runs)."""
    return next((c for c in _CANDIDATES if os.path.exists(c)), _CANDIDATES[0])


VENV_PY = venv_py()
MODELS_LOG = os.path.join(ROOT, "models.log")   # what the model processes print (PyTorch's warnings, a crash)
RETRY_S = 120.0                                  # a model that would not start is tried again after this


def note(msg: str) -> None:
    """A line for models.log (and the studio's own log): a studio started without a console prints nowhere."""
    print(msg)
    try:
        with open(MODELS_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


class ModelWorker:
    def __init__(self, module: str, answer_key: str, start_timeout: float = 900.0, request_timeout: float = 600.0):
        self.module, self.answer_key = module, answer_key
        self.start_timeout, self.request_timeout = start_timeout, request_timeout
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.failed = ""              # why the worker could not start (tried again after RETRY_S)
        self.failed_at = 0.0
        self.name = module.rsplit(".", 1)[-1]
        self.last_error = ""          # why the last request failed

    def python(self) -> str:
        """The Python the worker runs in: the model environment's (a subclass may run elsewhere)."""
        return venv_py()

    def available(self) -> bool:
        return os.path.exists(self.python())

    env_extra: dict = {}                          # more environment for the worker process

    def _start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        if self.failed:
            if time.monotonic() - self.failed_at < RETRY_S:
                return False
            note(f"[{self.name}] trying the model again (it would not start: {self.failed})")
            self.failed = ""
        if not self.available():
            self.failed = f"the model environment is not installed (looked for {' and '.join(_CANDIDATES)})"
            self.failed_at = time.monotonic()
            note(f"[{self.name}] {self.failed}")
            return False
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", **self.env_extra}
        env.setdefault("LOCALAPPDATA", local_appdata())    # PyTorch and the model cache look for it
        try:
            err = open(MODELS_LOG, "a", encoding="utf-8")
            err.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {self.module} starting\n")
            err.flush()
        except OSError:
            err = subprocess.DEVNULL
        self.proc = subprocess.Popen([self.python(), "-m", self.module], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                     creationflags=(subprocess.CREATE_NO_WINDOW | POPEN_BELOW_NORMAL) if sys.platform == "win32" else 0)
        hello = self._read(self.start_timeout)          # the first run downloads the model
        if not hello or not hello.get("ready"):
            self.failed = (hello or {}).get("error", "the model did not start")
            self.failed_at = time.monotonic()
            note(f"[{self.name}] the model would not start: {self.failed} (see models.log); trying again in {RETRY_S:.0f} s")
            self.stop()
            return False
        return True

    def _read(self, timeout: float) -> dict | None:
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("line", self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        try:
            return json.loads(box["line"]) if box.get("line") else None
        except ValueError:
            return None

    def _ask(self, header: dict, payload: bytes = b""):
        with self.lock:
            for _ in (1, 2):                             # a worker that died is restarted once
                if not self._start():
                    self.last_error = self.failed
                    return None
                try:
                    self.proc.stdin.write((json.dumps(header) + "\n").encode() + payload)
                    self.proc.stdin.flush()
                    r = self._read(self.request_timeout)
                except OSError:
                    r = None
                if r is not None:
                    if "error" in r:
                        self.last_error = r["error"]
                        note(f"[{self.name}] {r['error']}")
                        return None
                    self.last_error = ""
                    return r.get(self.answer_key)
                self.stop()
            self.last_error = "the model stopped answering"
            note(f"[{self.name}] the model stopped answering (see models.log)")
            return None

    def ask_file_and_cache(self, path: str, cache_dir: str):
        return self._ask({"path": os.path.abspath(path), "cache": os.path.abspath(cache_dir)})

    def ask_file(self, path: str):
        return self._ask({"path": os.path.abspath(path)})       # always a full path: the worker changes directory

    def ask_audio(self, y: np.ndarray, sr: int):
        y = np.ascontiguousarray(y, dtype=np.float32)
        return self._ask({"samples": int(len(y)), "sr": int(sr)}, y.tobytes())

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                p.kill()
