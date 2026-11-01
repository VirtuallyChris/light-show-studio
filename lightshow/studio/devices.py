"""Which processor each model runs on: the graphics card where there is one PyTorch can use, else the CPU.

The order: CUDA (an NVIDIA card; a ROCm build of PyTorch on an AMD card under Linux reports as CUDA too),
then MPS (Apple Silicon), then DirectML (torch_directml: AMD and Intel cards on Windows, best effort),
then the CPU. A model that does not work on its card is pinned back to the CPU: the ones known not to
(KNOWN_BAD), and any whose load or warm-up fails there (models_host pins it and loads it again).

Used in the model environment by every model (never imports torch until asked), and in the studio's own
Python by the section labeler (xgboost_device: no torch there).

LSS_MODELS_DEVICE=cpu (or cuda, mps, directml) in the environment overrides the choice for every model.
"""

from __future__ import annotations

import ctypes
import os
import sys

ORDER = ("cuda", "mps", "directml", "cpu")

# What does not run on a card's PyTorch (a guess where marked untested: no such card was at hand).
# DirectML has no complex numbers, so no STFT on the card: Demucs (stems, and lyrics' own copy), Beat This
# and SongFormer make their spectrograms there. Untested on a real AMD card.
KNOWN_BAD = {
    "directml": {"songformer_infer", "stems_infer", "beats_infer", "lyrics.demucs"},
}

# Apple's PyTorch runs an operation it does not have on the CPU instead of failing (set before torch loads)
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

_PINNED: set[str] = set()          # models pinned to the CPU this run (they failed on their card)
_FOUND: list[str] = []             # the best device found, once (asking torch is slow the first time)


def _torch_kind(torch) -> str:
    """The best device this PyTorch can use, in ORDER."""
    try:
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    try:
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch_directml
        if torch_directml.device_count() > 0:
            return "directml"
    except Exception:  # noqa: BLE001 - not installed, or no card it can use
        pass
    return "cpu"


def found() -> str:
    """The best device on this machine for any model ("cuda", "mps", "directml" or "cpu")."""
    want = os.environ.get("LSS_MODELS_DEVICE", "").strip().lower()
    if want in ORDER:
        return want
    if not _FOUND:
        try:
            import torch
            _FOUND.append(_torch_kind(torch))
        except Exception:  # noqa: BLE001 - no PyTorch: the CPU, for what can run without it
            _FOUND.append("cpu")
    return _FOUND[0]


def pinned(model: str) -> bool:
    """True when the model (or the one it is part of: "lyrics.demucs" is part of "lyrics") runs on the CPU."""
    return model in _PINNED or model.split(".", 1)[0] in _PINNED


def pin_to_cpu(model: str) -> bool:
    """Run this model on the CPU from now on. True if that is a change (it was on a card)."""
    if best_device(model) == "cpu":
        return False
    _PINNED.add(model)
    return True


def may_fall_back() -> bool:
    """Whether a model that fails on the card should move to the CPU: on Apple, DirectML and AMD (a ROCm
    PyTorch, though it says "cuda"), not on NVIDIA, where every model is known to run."""
    kind = found()
    if kind == "cuda":
        torch = sys.modules.get("torch")
        return bool(getattr(getattr(torch, "version", None), "hip", None))
    return kind != "cpu"


def best_device(model: str | None = None) -> str:
    """Where this model runs: the machine's best device, unless the model does not work there."""
    kind = found()
    if model and (pinned(model) or model in KNOWN_BAD.get(kind, ()) or model.split(".", 1)[0] in KNOWN_BAD.get(kind, ())):
        return "cpu"
    return kind


def torch_device(kind: str):
    """What PyTorch takes for a device kind: "cuda:0", "mps", the DirectML device, or "cpu"."""
    if kind == "cuda":
        return "cuda:0"
    if kind == "directml":
        import torch_directml
        return torch_directml.device()
    return kind


def device_for(model: str):
    """The PyTorch device this model loads onto."""
    return torch_device(best_device(model))


def whisper_device() -> tuple[str, str]:
    """(device, compute type) for faster-whisper. CTranslate2 runs on an NVIDIA card or the CPU only (a ROCm
    PyTorch says "cuda", but CTranslate2's CUDA build cannot use an AMD card): float16 there, int8 on the CPU."""
    if best_device("lyrics") == "cuda":
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:  # noqa: BLE001
            pass
    return "cpu", "int8"


def nvidia_gpus() -> int:
    """How many NVIDIA cards the driver sees, asked of the driver itself (no torch, no nvidia-smi)."""
    names = {"win32": ["nvcuda.dll"], "darwin": []}.get(sys.platform, ["libcuda.so.1", "libcuda.so"])
    for name in names:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        try:
            n = ctypes.c_int(0)
            if lib.cuInit(0) == 0 and lib.cuDeviceGetCount(ctypes.byref(n)) == 0:
                return n.value
        except Exception:  # noqa: BLE001
            pass
        return 0
    return 0


_XGB: list[str] = []


def xgboost_device() -> str:
    """"cuda" when this XGBoost was built with CUDA and there is an NVIDIA card, else "cpu" (fast enough
    for the labeler's few thousand bars)."""
    if not _XGB:
        kind = "cpu"
        try:
            import xgboost
            if str(xgboost.build_info().get("USE_CUDA", "")).upper() in ("1", "ON", "TRUE") and nvidia_gpus() > 0:
                kind = "cuda"
        except Exception:  # noqa: BLE001
            pass
        _XGB.append(kind)
    return _XGB[0]
