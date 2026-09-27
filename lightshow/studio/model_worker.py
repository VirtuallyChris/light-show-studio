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

import numpy as np

from .priority import POPEN_BELOW_NORMAL

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The model environment lives on the local disk (PyTorch is thousands of files);
# a copy inside the project is honoured too.
_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "lanlightshow", "venv-songformer", "Scripts", "python.exe"),
    os.path.join(ROOT, ".venv-songformer", "Scripts", "python.exe"),
]
VENV_PY = next((c for c in _CANDIDATES if os.path.exists(c)), _CANDIDATES[0])


class ModelWorker:
    def __init__(self, module: str, answer_key: str, start_timeout: float = 900.0, request_timeout: float = 600.0):
        self.module, self.answer_key = module, answer_key
        self.start_timeout, self.request_timeout = start_timeout, request_timeout
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.failed = ""              # why the worker could not start (it is not retried until the studio restarts)
        self.last_error = ""          # why the last request failed

    @staticmethod
    def available() -> bool:
        return os.path.exists(VENV_PY)

    def _start(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        if self.failed:
            return False
        if not self.available():
            self.failed = "the model environment is not installed"
            return False
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen([VENV_PY, "-m", self.module], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=(subprocess.CREATE_NO_WINDOW | POPEN_BELOW_NORMAL) if sys.platform == "win32" else 0)
        hello = self._read(self.start_timeout)          # the first run downloads the model
        if not hello or not hello.get("ready"):
            self.failed = (hello or {}).get("error", "the model did not start")
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
                        print(f"[{self.module.rsplit('.', 1)[-1]}] {r['error']}", file=sys.stderr)
                        return None
                    self.last_error = ""
                    return r.get(self.answer_key)
                self.stop()
            self.last_error = "the model stopped answering"
            return None

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
