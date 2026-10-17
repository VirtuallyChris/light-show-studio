"""The models, in the separate model environment (CUDA PyTorch), shared by everything.

The four models (sections, stems, genre, beats) run in ONE host process (models_host.py): the studio
starts it and owns it, and the learn-ahead process talks to the same one, so each model loads once
for the whole app. A ModelWorker is one model's handle on it: the same calls as when each model was a
process of its own (ask_file, ask_audio, warm), one request at a time per model. Audio from memory
goes over a local connection, never to disk. The learn-ahead analysis (fetch_ahead.AnalysisWorker)
is still a process of its own (the studio's Python, below normal priority): hosted = False.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
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


WORKERS: list = []                     # every model worker made (see kill_all)


def kill_all() -> None:
    """Stop every model worker and all it started, the model host included, at once (the studio is
    closing)."""
    for w in list(WORKERS):
        try:
            w.kill()
        except Exception:  # noqa: BLE001
            pass
    HOST.kill()


def _kill_tree(p) -> None:
    """A process and everything it started (a venv's python.exe is a launcher for the real one)."""
    if not p or p.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            p.kill()
    except Exception:  # noqa: BLE001
        p.kill()


def _base_python() -> tuple[str, str] | None:
    """The model environment's real interpreter and its site-packages, to start the host without the
    venv's launcher (one process, not two). None if it cannot be told."""
    venv = os.path.dirname(os.path.dirname(venv_py()))
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8") as fh:
            home = next(line.split("=", 1)[1].strip() for line in fh if line.split("=", 1)[0].strip() == "home")
    except (OSError, StopIteration):
        return None
    exe, site_dir = os.path.join(home, "python.exe"), os.path.join(venv, "Lib", "site-packages")
    return (exe, site_dir) if os.path.exists(exe) and os.path.isdir(site_dir) else None


class ModelHost:
    """This process's connection to the model host (models_host.py), shared by its model workers.

    The first process to need a model starts the host and owns it (the studio; the host exits when
    the owner does) and puts its address and access key in the environment, so the processes it
    starts (the learn-ahead analysis) use the same host instead of starting their own."""

    START_TIMEOUT = 90.0

    def __init__(self):
        self.lock = threading.Lock()
        self.wlock = threading.Lock()
        self.proc: subprocess.Popen | None = None          # the host, when this process owns it
        self.sock: socket.socket | None = None
        self.pending: dict[int, list] = {}
        self.next_id = 0
        self.gen = 0                                       # a new connection (a host restarted) is a new generation
        self.error = ""

    def _connect_to(self, addr: str, token: str) -> bool:
        host, port = addr.rsplit(":", 1)
        try:
            sock = socket.create_connection((host, int(port)), timeout=5)
            sock.settimeout(None)
            sock.sendall((token + "\n").encode())
        except OSError:
            return False
        self.sock = sock
        self.gen += 1
        threading.Thread(target=self._reader, args=(sock,), daemon=True, name="models-reader").start()
        return True

    def _start_host(self) -> bool:
        token = secrets.token_hex(16)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LSS_MODELS_TOKEN": token, "LSS_MODELS_OWNER": str(os.getpid())}
        env.setdefault("LOCALAPPDATA", local_appdata())    # PyTorch and the model cache look for it
        try:
            err = open(MODELS_LOG, "a", encoding="utf-8")
            err.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} lightshow.studio.models_host starting\n")
            err.flush()
        except OSError:
            err = subprocess.DEVNULL
        flags = (DETACHED_PROCESS | POPEN_BELOW_NORMAL) if sys.platform == "win32" else 0
        cmds = []
        base = _base_python()
        if base:                                           # the real interpreter itself: no launcher process
            boot = ("import sys, site, runpy; sys.path.insert(0, %r); site.addsitedir(%r); "
                    "runpy.run_module('lightshow.studio.models_host', run_name='__main__', alter_sys=True)") % (ROOT, base[1])
            cmds.append([base[0], "-I", "-S", "-c", boot])
        cmds.append([venv_py(), "-m", "lightshow.studio.models_host"])
        for cmd in cmds:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, creationflags=flags)
            box = {}
            t = threading.Thread(target=lambda: box.setdefault("line", proc.stdout.readline()), daemon=True)
            t.start()
            t.join(self.START_TIMEOUT)
            try:
                hello = json.loads(box.get("line") or b"null")
            except ValueError:
                hello = None
            if hello and hello.get("ready"):
                self.proc = proc
                addr = f"127.0.0.1:{hello['port']}"
                os.environ["LSS_MODELS_ADDR"], os.environ["LSS_MODELS_TOKEN"] = addr, token   # for what we start
                return self._connect_to(addr, token)
            self.error = (hello or {}).get("error", "the model host did not start")
            _kill_tree(proc)
        return False

    def connect(self) -> bool:
        with self.lock:
            if self.sock is not None:
                return True
            if not os.path.exists(venv_py()):
                self.error = f"the model environment is not installed (looked for {' and '.join(_CANDIDATES)})"
                return False
            addr, token = os.environ.get("LSS_MODELS_ADDR"), os.environ.get("LSS_MODELS_TOKEN")
            if addr and token and (self.proc is None or self.proc.poll() is None) and self._connect_to(addr, token):
                return True
            _kill_tree(self.proc)                          # (ours, and gone quiet: started again)
            self.proc = None
            return self._start_host()

    def _reader(self, sock) -> None:
        f = sock.makefile("rb")
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                r = json.loads(line)
                n = int(r.pop("payload", 0) or 0)
                payload = _read_exactly(f, n) if n else b""
                slot = self.pending.pop(r.pop("id", None), None)
                if slot:
                    slot[1], slot[2] = r, payload or b""
                    slot[0].set()
        except (OSError, ValueError):
            pass
        with self.lock:
            if self.sock is sock:
                self.sock = None
        for slot in list(self.pending.values()):           # nothing more will come on this connection
            slot[0].set()
        self.pending.clear()

    def ask(self, model: str, header: dict, payload: bytes = b"", timeout: float = 600.0):
        """(reply, payload bytes); reply None when the host could not be reached or did not answer in time."""
        if not self.connect():
            return None, b""
        with self.lock:
            self.next_id += 1
            rid, sock = self.next_id, self.sock
        slot = [threading.Event(), None, b""]
        self.pending[rid] = slot
        try:
            with self.wlock:
                sock.sendall((json.dumps({"id": rid, "model": model, **header, "payload": len(payload)}) + "\n").encode() + payload)
        except (OSError, AttributeError):
            self.pending.pop(rid, None)
            return None, b""
        if not slot[0].wait(timeout):
            self.pending.pop(rid, None)
            return None, b""
        return slot[1], slot[2]

    def kill(self) -> None:
        with self.lock:
            sock, self.sock = self.sock, None
            proc, self.proc = self.proc, None
        try:
            if sock is not None:
                sock.close()
        except OSError:
            pass
        _kill_tree(proc)


def _read_exactly(f, n: int) -> bytes | None:
    chunks, got = [], 0
    while got < n:
        b = f.read(n - got)
        if not b:
            return None
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


HOST = ModelHost()
DETACHED_PROCESS = 0x00000008                              # no console (and no console host process) at all


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
        self.loaded_gen = 0           # the host connection this model was loaded on (hosted models)
        WORKERS.append(self)          # so the studio can stop them all when it closes (kill_all)

    hosted = True                                 # one of the model host's models (False: a process of its own)

    def python(self) -> str:
        """The Python the worker runs in: the model environment's (a subclass may run elsewhere)."""
        return venv_py()

    def available(self) -> bool:
        return os.path.exists(self.python() if not self.hosted else venv_py())

    env_extra: dict = {}                          # more environment for the worker process

    def _start(self) -> bool:
        if self.hosted:
            return self._start_hosted()
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

    def _start_hosted(self) -> bool:
        """The host reached and this model loaded on it (the first time, it may download the model)."""
        if self.loaded_gen and self.loaded_gen == HOST.gen and HOST.sock is not None:
            return True
        if self.failed:
            if time.monotonic() - self.failed_at < RETRY_S:
                return False
            note(f"[{self.name}] trying the model again (it would not start: {self.failed})")
            self.failed = ""
        r, _ = HOST.ask(self.name, {"op": "load"}, timeout=self.start_timeout)
        if not r or not r.get("ready"):
            self.failed = (r or {}).get("error") or HOST.error or "the model did not start"
            self.failed_at = time.monotonic()
            note(f"[{self.name}] the model would not start: {self.failed} (see models.log); trying again in {RETRY_S:.0f} s")
            return False
        self.loaded_gen = HOST.gen
        return True

    def ask_hosted(self, header: dict, payload: bytes = b""):
        """(reply, payload bytes) from the model host, the model loaded first; (None, b"") if it cannot."""
        with self.lock:
            for _ in (1, 2):                             # a host that went away is started again, once
                if not self._start_hosted():
                    self.last_error = self.failed
                    return None, b""
                r, out = HOST.ask(self.name, header, payload, timeout=self.request_timeout)
                if r is not None:
                    if "error" in r:
                        self.last_error = r["error"]
                        note(f"[{self.name}] {r['error']}")
                        return None, b""
                    self.last_error = ""
                    return r, out
                self.loaded_gen = 0
            self.last_error = "the model stopped answering"
            note(f"[{self.name}] the model stopped answering (see models.log)")
            return None, b""

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
        if self.hosted:
            r, _ = self.ask_hosted(header, payload)
            return r.get(self.answer_key) if r else None
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

    def warm(self) -> bool:
        """Start the worker (and load its model) now, ahead of the first request."""
        with self.lock:
            return self._start()

    def ask_file_and_cache(self, path: str, cache_dir: str):
        return self._ask({"path": os.path.abspath(path), "cache": os.path.abspath(cache_dir)})

    def ask_file(self, path: str):
        return self._ask({"path": os.path.abspath(path)})       # always a full path: the worker changes directory

    def ask_audio(self, y: np.ndarray, sr: int):
        y = np.ascontiguousarray(y, dtype=np.float32)
        return self._ask({"samples": int(len(y)), "sr": int(sr)}, y.tobytes())

    def kill(self) -> None:
        """Stop at once, with everything it started: the studio is closing. (A venv's python.exe is a
        launcher that starts the real interpreter: the whole tree goes, or the real one runs on until
        its current song is done, minutes later.) A hosted model goes with the host (kill_all)."""
        p, self.proc = self.proc, None
        _kill_tree(p)

    def stop(self) -> None:
        if self.hosted:
            self.loaded_gen = 0                      # (the host serves the others; this model reloads if needed)
            return
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                p.kill()
