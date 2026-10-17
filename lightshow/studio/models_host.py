"""All the studio's models in one process: SongFormer (sections), Demucs (stems), the genre model,
Beat This (beats) and ADTOF (drum hits), in the model environment (CUDA PyTorch).

Each model used to be a process of its own, and the learn-ahead process started its own set again:
with the venv's launcher and a console host for every one, Task Manager showed a couple of dozen
Python entries and ~15 GB of memory, each process carrying its own PyTorch and CUDA runtime. Now one
host process serves everyone. It is started by the studio (its owner: when the owner's pipe closes,
the host exits at once) and listens on a local port that the learn-ahead process is told about, so
the models load once for the whole app.

Requests come over a TCP connection on 127.0.0.1, after a first line with the access key the owner
was given (a random token, never written anywhere):

    request  one JSON line {"id": 7, "model": "stems", ..., "payload": nbytes} then nbytes of audio,
             the request fields as each model's own worker took them ({"path": ...} or {"samples":
             N, "sr": ..., "channels": ...}); or {"id": 7, "model": "beats", "op": "load"}
    reply    one JSON line {"id": 7, ..., "payload": nbytes} then nbytes (the stems' audio), the
             reply fields as each worker gave them ({"segments": ...}, {"labels": ...}, {"beats":
             ...}, {"stems": ...}, {"hits": ...}), or {"id": 7, "error": "...", "load_failed": true/false}

Each model answers one request at a time on a thread of its own, so different models run side by
side, as the analysis asks them. A model is loaded when first asked (or when warmed with "load").
The first line the host prints to its owner is {"ready": true, "port": p}; it exits when the owner
process (LSS_MODELS_OWNER) does.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback


def _log(msg: str) -> None:
    print(f"[models] {msg}", file=sys.stderr, flush=True)


class Model:
    """One model: loaded once, on its own thread, a request at a time."""

    def __init__(self, name: str):
        self.name, self.q = name, queue.Queue()
        self.state, self.load_error = None, ""
        self.phase = "loading"                                # loading -> warming -> ready (or failed)
        threading.Thread(target=self._run, daemon=True, name=f"model-{name}").start()

    def _load(self):
        if self.state is not None:
            return True
        t0 = time.time()
        try:
            with LOAD_LOCK:                              # one load at a time: two threads importing the same
                self.state = LOADERS[self.name]()        # library at once (transformers) break each other
                CARD.set_cap()
            _log(f"{self.name} loaded in {time.time() - t0:.1f} s")
            self.load_error = ""
            self.phase = "warming"
            self._warm_up()
            self.phase = "ready"
            return True
        except Exception as e:  # noqa: BLE001
            self.load_error = f"{type(e).__name__}: {e}"
            self.phase = "failed"
            _log(f"{self.name} would not load: {self.load_error}\n{traceback.format_exc()}")
            return False

    def _warm_up(self):
        """One practice run on a few seconds of quiet noise, right after loading: a GPU model's first run or
        two pay the card's setting up (kernels chosen, memory laid out), 8-12 s for SongFormer where a warm
        run takes 2.5, and that used to land on the first songs learned after the studio opened."""
        import numpy as np
        sr, secs = WARM_AUDIO.get(self.name, (22050, 10))
        y = (0.01 * np.random.default_rng(0).standard_normal(int(sr * secs))).astype(np.float32)
        t0 = time.time()
        try:
            HANDLERS[self.name](self.state, {"samples": len(y), "sr": sr}, y.tobytes())
            _log(f"{self.name} warmed up in {time.time() - t0:.1f} s")
        except Exception as e:  # noqa: BLE001 - a warm-up that fails costs nothing
            _log(f"{self.name} warm-up skipped: {type(e).__name__}: {e}")

    def _run(self):
        while True:
            req, payload, answer, queued = self.q.get()
            waited = time.time() - queued
            if not self._load():
                answer({"error": self.load_error, "load_failed": True})
                continue
            if req.get("op") == "load":
                answer({"ready": True})
                continue
            t0 = time.time()
            try:
                obj, out = self._answer(req, payload)
                took = time.time() - t0
                if took > 5 or waited > 2:                   # (a slow song learned: which model, and why)
                    _log(f"{time.strftime('%H:%M:%S')} {self.name}: waited {waited:.1f} s in its queue, ran {took:.1f} s{_card_use()}")
                answer({**obj, "seconds": round(took, 2)}, out)
            except Exception as e:  # noqa: BLE001 - one bad request must not end the model
                answer({"error": f"{type(e).__name__}: {e}"})
            finally:
                _free()

    def _answer(self, req, payload):
        """The model's answer. On the card beside the other models; one that runs out of the card's memory
        there (a long song while another model is busy with it too) waits for them to finish and runs
        alone; one that still cannot fit is let past the cap that once, slow, rather than not answer."""
        try:
            with CARD.shared():
                return HANDLERS[self.name](self.state, req, payload)
        except Exception as e:  # noqa: BLE001
            if not _out_of_memory(e):
                raise
        _log(f"{time.strftime('%H:%M:%S')} {self.name}: out of the card's memory beside the other models: again, alone")
        with CARD.alone():
            _free()
            try:
                return HANDLERS[self.name](self.state, req, payload)
            except Exception as e:  # noqa: BLE001
                if not _out_of_memory(e) or CARD.cap is None:
                    raise
            _log(f"{self.name}: too big for the card even alone: past the cap this once (slow)")
            _free()
            CARD.limit(1.0)
            try:
                return HANDLERS[self.name](self.state, req, payload)
            finally:
                _free()
                CARD.limit(CARD.cap)


# -- the graphics card ------------------------------------------------------------------------------------

class Card:
    """The graphics card the models share, and its memory.

    On Windows a CUDA program that asks for more memory than the card has is not refused: the driver
    quietly lends it the PC's own memory, over the bus, and everything it runs slows to a crawl. Five
    models, two of them busy with a long song at once, plus the memory PyTorch keeps cached between
    songs, reached 14 GB on a 16 GB card and 8 GB more borrowed: the sections and stems of a song took
    30 s instead of 6, and the next song sat "analyzing" for minutes. So the models are kept under the
    card's size, less room for the desktop, the browser and the room view (a model that would go past
    that is told it is out of memory, and runs again alone: see Model._answer)."""

    HEADROOM = 3.0 * 2**30                               # bytes left for everything else on the card

    def __init__(self):
        self.cond = threading.Condition()
        self.running = 0                                 # requests on the card now
        self.solo = False                                # one is waiting for the card to itself
        self.cap: float | None = None                    # the share of the card the models keep under

    def set_cap(self) -> None:
        torch = _cuda()
        if torch is None or self.cap is not None:
            return
        total = torch.cuda.get_device_properties(0).total_memory
        self.cap = max(0.5, (total - self.HEADROOM) / total)
        self.limit(self.cap)
        _log(f"the card has {total / 2**30:.1f} GB: the models keep under {self.cap * total / 2**30:.1f} GB")

    def limit(self, share: float) -> None:
        torch = _cuda()
        if torch is not None:
            torch.cuda.set_per_process_memory_fraction(share)

    @contextlib.contextmanager
    def shared(self):
        with self.cond:
            self.cond.wait_for(lambda: not self.solo)
            self.running += 1
        try:
            yield
        finally:
            with self.cond:
                self.running -= 1
                self.cond.notify_all()

    @contextlib.contextmanager
    def alone(self):
        with self.cond:
            self.cond.wait_for(lambda: not self.solo)
            self.solo = True                             # nothing new starts meanwhile
            self.cond.wait_for(lambda: self.running == 0)
        try:
            yield
        finally:
            with self.cond:
                self.solo = False
                self.cond.notify_all()


CARD = Card()


def _cuda():
    """torch, once a model has imported it and when there is a card; else None (never imports it)."""
    torch = sys.modules.get("torch")
    try:
        return torch if torch is not None and torch.cuda.is_available() else None
    except Exception:  # noqa: BLE001
        return None


def _out_of_memory(e: BaseException) -> bool:
    return type(e).__name__ == "OutOfMemoryError" or "out of memory" in str(e).lower()


def _free() -> None:
    """Hand back what PyTorch keeps cached from the last request: a long song's buffers, kept for the
    next song, piled up song after song until the card was full."""
    torch = _cuda()
    if torch is not None:
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def _card_use() -> str:
    torch = _cuda()
    if torch is None:
        return ""
    try:
        return f" (card: {torch.cuda.memory_reserved() / 2**30:.1f} GB held)"
    except Exception:  # noqa: BLE001
        return ""


# -- the models: how each loads, and how it answers (as its own worker module did) -----------------------

def _audio(req, payload, sr_out, mono=True):
    import librosa
    import numpy as np
    if "samples" in req:
        ch = int(req.get("channels", 1))
        y = np.frombuffer(payload, dtype=np.float32)
        y = y.reshape(-1, ch).T if ch > 1 else y
        sr = int(req.get("sr", sr_out or 22050))
        if mono and y.ndim > 1:
            y = y.mean(axis=0)
        if sr_out and sr != sr_out:
            y = librosa.resample(np.ascontiguousarray(y, dtype=np.float32), orig_sr=sr, target_sr=sr_out)
            sr = sr_out
        return np.asarray(y, dtype=np.float32), sr
    path = req["path"]
    if not os.path.isabs(path):
        raise ValueError(f"a full path is needed, got {path!r}")
    y, sr = librosa.load(path, sr=sr_out, mono=mono)
    return np.asarray(y, dtype=np.float32), sr


def _load_sections():
    from . import songformer_infer
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    model, _ = songformer_infer.load_model()
    return model


def _sections(model, req, payload):
    from . import songformer_infer
    y, _ = _audio(req, payload, songformer_infer.SR)
    if len(y) < songformer_infer.SR * 8:
        raise ValueError("not enough audio to find sections in")
    return {"segments": songformer_infer.segments(model, y)}, b""


def _load_stems():
    from . import stems_infer
    return stems_infer.load_model()


def _stems(state, req, payload):
    import numpy as np
    from . import stems_infer
    model, device = state
    y, sr = _audio(req, payload, None, mono=False)
    stems, levels = stems_infer.separate(model, device, np.asarray(y, dtype=np.float32), sr)
    d, b, o = stems["drums"], stems["bass"], stems["other"]
    return ({"stems": {"n": len(d), "sr": stems_infer.OUT_SR, "order": ["drums", "bass", "other"]},
             "levels": {k: levels[k] for k in ("vocals", "other")}, "fps": stems_infer.FPS},
            d.tobytes() + b.tobytes() + o.tobytes())


def _load_genre():
    from . import genre_infer
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    return genre_infer.load_model()                      # MAEST, or AST where it cannot load


def _genre(state, req, payload):
    from . import genre_infer
    y, _ = _audio(req, payload, genre_infer.SR)
    return genre_infer.classify(state, y), b""


def _load_beats():
    from . import beats_infer
    model, _ = beats_infer.load_model()
    return model


def _beats(model, req, payload):
    from . import beats_infer
    y, _ = _audio(req, payload, beats_infer.SR)
    if len(y) < beats_infer.SR * 5:
        raise ValueError("not enough audio to find beats in")
    return {"beats": beats_infer.find(model, y)}, b""


def _load_drums():
    from . import drums_infer
    return drums_infer.load_model()


def _drums(state, req, payload):
    from . import drums_infer
    y, _ = _audio(req, payload, drums_infer.SR)
    return {"hits": drums_infer.find(state, y)}, b""


LOADERS = {"songformer_infer": _load_sections, "stems_infer": _load_stems, "genre_infer": _load_genre, "beats_infer": _load_beats,
           "drums_infer": _load_drums}
HANDLERS = {"songformer_infer": _sections, "stems_infer": _stems, "genre_infer": _genre, "beats_infer": _beats,
            "drums_infer": _drums}
LOAD_LOCK = threading.Lock()
WARM_AUDIO = {"songformer_infer": (24000, 60), "stems_infer": (22050, 30), "genre_infer": (16000, 6),   # (rate, seconds)
              "beats_infer": (22050, 8), "drums_infer": (44100, 8)}
MODELS: dict[str, Model] = {}
MODELS_LOCK = threading.Lock()


def model(name: str) -> Model:
    with MODELS_LOCK:
        if name not in MODELS:
            MODELS[name] = Model(name)
        return MODELS[name]


# -- the connections --------------------------------------------------------------------------------------

def _read_exactly(f, n: int) -> bytes | None:
    chunks, got = [], 0
    while got < n:
        b = f.read(n - got)
        if not b:
            return None
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def _serve(conn: socket.socket, token: str) -> None:
    f = conn.makefile("rb")
    wlock = threading.Lock()
    try:
        if f.readline().decode().strip() != token:
            return
        while True:
            line = f.readline()
            if not line:
                return
            req = json.loads(line)
            payload = _read_exactly(f, int(req.pop("payload", 0) or 0)) or b""
            rid, name = req.pop("id"), req.pop("model")

            def answer(obj, out: bytes = b"", rid=rid):
                head = (json.dumps({"id": rid, **obj, "payload": len(out)}) + "\n").encode()
                try:
                    with wlock:
                        conn.sendall(head + out)
                except OSError:
                    pass                                 # the asker went away

            if name == "host" and req.get("op") == "status":    # where each model stands (the studio's loading screen)
                with MODELS_LOCK:
                    have = dict(MODELS)
                answer({"models": {n: (have[n].phase if n in have else "waiting") for n in LOADERS}})
                continue
            if name not in LOADERS:
                answer({"error": f"no model {name!r}"})
                continue
            model(name).q.put((req, payload, answer, time.time()))
    except (OSError, ValueError):
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr                              # libraries print; only the ready line goes to the owner
    token = os.environ.get("LSS_MODELS_TOKEN", "")
    if not token:
        real_stdout.write(json.dumps({"ready": False, "error": "no access key given"}) + "\n")
        real_stdout.flush()
        return 1
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    # When the owner (the studio) exits, so does the host: a thread waits on the owner's process. (Not on
    # a pipe from it: on Windows a thread blocked reading a pipe makes every other thread's look at that
    # pipe wait too, and loading PyTorch looks at stdin: the first model never loaded.)
    owner = int(os.environ.get("LSS_MODELS_OWNER", "0") or 0)

    def owner_gone():
        if sys.platform == "win32" and owner:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x00100000, False, owner)          # SYNCHRONIZE
            if h:
                k.WaitForSingleObject(h, 0xFFFFFFFF)
                os._exit(0)
        while True:                                              # elsewhere: the parent changing means it left
            time.sleep(2)
            if owner and os.getppid() != owner:
                os._exit(0)
    threading.Thread(target=owner_gone, daemon=True).start()

    real_stdout.write(json.dumps({"ready": True, "port": port, "pid": os.getpid()}) + "\n")
    real_stdout.flush()
    # Nothing reads the owner's pipe after this line: anything a library writes to the process's own stdout
    # from native code (not through Python) would fill it and freeze that thread, silently, mid-request.
    # From here on the process's stdout is its log file.
    try:
        os.dup2(sys.stderr.fileno(), 1)
    except OSError:
        pass
    _log(f"serving on 127.0.0.1:{port}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve, args=(conn, token), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
