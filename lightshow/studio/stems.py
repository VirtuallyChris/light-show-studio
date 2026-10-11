"""The song split into drums and bass, by the Demucs worker in the model environment.

`separate_file` and `separate_audio` return {"drums": array, "bass": array, "other": array, "sr": 22050,
"levels": {"vocals": [...], "other": [...]}, "fps": 30}, or None when the model environment is
missing or the separation failed (the analysis then works from the full mix, as before).
Nothing is written to disk: the stems come back over the worker's pipe.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

from .model_worker import ModelWorker


class StemsWorker(ModelWorker):
    def _ask_stems(self, header: dict, payload: bytes = b""):
        with self.lock:
            for _ in (1, 2):                             # a worker that died is restarted once
                if not self._start():
                    self.last_error = self.failed
                    return None
                try:
                    self.proc.stdin.write((json.dumps(header) + "\n").encode() + payload)
                    self.proc.stdin.flush()
                    r = self._read(self.request_timeout)
                    if r is not None and "stems" in r:
                        n, order = int(r["stems"]["n"]), r["stems"].get("order", ["drums", "bass"])
                        raw = _read_exactly(self.proc.stdout, n * 4 * len(order))
                        if raw is None:
                            r = None
                        else:
                            arr = np.frombuffer(raw, dtype=np.float32)
                            self.last_error = ""
                            return {**{k: arr[i * n:(i + 1) * n].copy() for i, k in enumerate(order)}, "sr": int(r["stems"]["sr"]),
                                    "levels": r.get("levels", {}), "fps": r.get("fps", 30), "seconds": r.get("seconds")}
                except OSError:
                    r = None
                if r is not None and "error" in r:
                    self.last_error = r["error"]
                    print(f"[stems] {r['error']}", file=sys.stderr)
                    return None
                self.stop()
            self.last_error = "the separation stopped answering"
            return None


def _read_exactly(stream, n: int):
    chunks, got = [], 0
    while got < n:
        b = stream.read(n - got)
        if not b:
            return None
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


WORKER = StemsWorker("lightshow.studio.stems_infer", "stems")


def available() -> bool:
    return WORKER.available() and not WORKER.failed


def separate_file(path: str):
    if not WORKER.available():
        return None
    return WORKER._ask_stems({"path": os.path.abspath(path)})


def separate_audio(y, sr: int):
    """y: mono (n,) or (channels, n) float32."""
    if not WORKER.available():
        return None
    y = np.asarray(y, dtype=np.float32)
    ch = 1 if y.ndim == 1 else y.shape[0]
    inter = y if ch == 1 else np.ascontiguousarray(y.T)
    return WORKER._ask_stems({"samples": int(inter.size // ch), "sr": int(sr), "channels": ch}, inter.tobytes())
