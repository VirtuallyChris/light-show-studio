"""Who goes first when the PC is busy.

Sound and lights have to be on time; analysis only has to get done. Windows
shares the CPU by priority, so the audio processes ask for real-time audio
scheduling (the "Pro Audio" class that recording software uses), and the models
and analysis step back. Then however hard the analysis works, a sound card that
wants its next 10 ms, or a light frame that is due, is served first.

Everything here does nothing on other systems or if Windows refuses.
"""

from __future__ import annotations

import sys

HIGH = 0x00000080                    # process priority classes
ABOVE_NORMAL = 0x00008000
BELOW_NORMAL = 0x00004000
IDLE = 0x00000040

_WIN = sys.platform == "win32"


def _kernel32():
    """kernel32 with handle types declared: the "current process/thread" handles are -1 and -2,
    which ctypes would otherwise pass as 32-bit numbers and Windows would reject."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetCurrentThread.restype = wintypes.HANDLE
    k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    return k


def process(level: int) -> bool:
    """Set this whole process's priority class."""
    if not _WIN:
        return False
    try:
        k = _kernel32()
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), level))
    except Exception:  # noqa: BLE001
        return False


def audio_thread() -> bool:
    """Put the calling thread in Windows' real-time audio scheduling (MMCSS, "Pro Audio")."""
    if not _WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        avrt = ctypes.WinDLL("avrt")
        avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        idx = wintypes.DWORD(0)
        h = avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(idx))
        if not h:
            return False
        avrt.AvSetMmThreadPriority(h, 1)            # AVRT_PRIORITY_HIGH
        return True
    except Exception:  # noqa: BLE001
        return False


def low_thread() -> bool:
    """Lower the calling thread, for work that only has to get done eventually."""
    if not _WIN:
        return False
    try:
        k = _kernel32()
        return bool(k.SetThreadPriority(k.GetCurrentThread(), -2))   # THREAD_PRIORITY_LOWEST
    except Exception:  # noqa: BLE001
        return False


def high_thread() -> bool:
    """Raise the calling thread, for work that has to be on time."""
    if not _WIN:
        return False
    try:
        k = _kernel32()
        return bool(k.SetThreadPriority(k.GetCurrentThread(), 2))    # THREAD_PRIORITY_HIGHEST
    except Exception:  # noqa: BLE001
        return False


# for subprocess.Popen(creationflags=...): start a model worker already stepped back
POPEN_BELOW_NORMAL = BELOW_NORMAL if _WIN else 0
