"""Send one program's sound to a chosen output: Windows' per-app output setting, from code.

This is the "App volume and device preferences" setting in Windows' sound settings
(and the Volume mixer), which lets one program play on a different output from
everything else. Windows has no published way for a program to set it, so this
uses the interface Windows' own settings page uses, as the EarTrumpet audio tool
does. Windows remembers the choice for the program's file (its full path), and
applies it to every process started from that file.

The studio uses it on its private copy of Chrome's program file (see
player_browser.py), so only the hidden Spotify player is routed, never the Chrome
you browse with.

Everything here returns False, and changes nothing, off Windows or if Windows
refuses; the caller then says so rather than guessing.
"""

from __future__ import annotations

import sys

_WIN = sys.platform == "win32"

CLASS = "Windows.Media.Internal.AudioPolicyConfig"
IIDS = ("ab3d4648-e242-459f-b02f-541c70306324",      # Windows 10 21H2 and later, Windows 11
        "2a59116d-6c4f-45e0-a74f-707e3fef9258")      # earlier Windows 10
SET_SLOT, GET_SLOT = 25, 26                            # after IInspectable's 6 and 19 unpublished methods
RENDER, CONSOLE, MULTIMEDIA = 0, 0, 1
PREFIX, SUFFIX = "\\\\?\\SWD#MMDEVAPI#", "#{e6327cad-dcec-4949-ae8a-991e976a79d2}"


def _guid(text: str):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("b", ctypes.c_ubyte * 16)]
    g = GUID()
    ctypes.memmove(g.b, uuid.UUID(text).bytes_le, 16)
    return g


class _Factory:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.combase = ctypes.WinDLL("combase")
        # Set this thread up for Windows Runtime only if nothing has yet, and undo it after (close):
        # left in place, it stops the sound library from listing devices on this thread later.
        self.inited = (self.combase.RoInitialize(1) & 0xFFFFFFFF) in (0, 1)
        self.ptr = ctypes.c_void_p()
        cls = self._hstring(CLASS)
        try:
            for iid in IIDS:
                g = _guid(iid)
                if self.combase.RoGetActivationFactory(cls, ctypes.byref(g), ctypes.byref(self.ptr)) == 0 and self.ptr.value:
                    return
            self.close()
            raise OSError("Windows would not hand out its audio policy interface")
        finally:
            self.combase.WindowsDeleteString(cls)

    def close(self) -> None:
        if self.ptr.value:
            self._method(2)(self.ptr)                     # IUnknown::Release
            self.ptr = self.ct.c_void_p()
        if self.inited:
            self.combase.RoUninitialize()
            self.inited = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _hstring(self, text: str):
        h = self.ct.c_void_p()
        if text:
            self.combase.WindowsCreateString(self.ct.c_wchar_p(text), len(text), self.ct.byref(h))
        return h

    def _method(self, slot: int, *argtypes):
        ct = self.ct
        vtable = ct.cast(ct.cast(self.ptr, ct.POINTER(ct.c_void_p))[0], ct.POINTER(ct.c_void_p))
        return ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *argtypes)(vtable[slot])

    def set(self, pid: int, device_id: str | None) -> bool:
        ct = self.ct
        fn = self._method(SET_SLOT, ct.c_uint, ct.c_int, ct.c_int, ct.c_void_p)
        h = self._hstring(PREFIX + device_id + SUFFIX if device_id else "")
        try:
            return all(fn(self.ptr, pid, RENDER, role, h) == 0 for role in (CONSOLE, MULTIMEDIA))
        finally:
            if h.value:
                self.combase.WindowsDeleteString(h)

    def get(self, pid: int) -> str:
        ct = self.ct
        fn = self._method(GET_SLOT, ct.c_uint, ct.c_int, ct.c_int, ct.POINTER(ct.c_void_p))
        out = ct.c_void_p()
        if fn(self.ptr, pid, RENDER, MULTIMEDIA, ct.byref(out)) != 0 or not out.value:
            return ""
        n = ct.c_uint()
        self.combase.WindowsGetStringRawBuffer.restype = ct.c_wchar_p
        text = self.combase.WindowsGetStringRawBuffer(out, ct.byref(n)) or ""
        self.combase.WindowsDeleteString(out)
        return text


def device_id(name_part: str) -> str | None:
    """The Windows id of the output whose name contains `name_part` (e.g. "S/PDIF")."""
    try:
        import soundcard as sc
        dev = next((s for s in sc.all_speakers() if name_part.lower() in s.name.lower()), None)
        return dev.id if dev else None
    except Exception:  # noqa: BLE001
        return None


def route(pid: int, output_name_part: str | None) -> bool:
    """Send the program that process `pid` runs from to an output (None: back to the default)."""
    if not _WIN:
        return False
    try:
        dev = device_id(output_name_part) if output_name_part else None
        if output_name_part and not dev:
            return False
        with _Factory() as f:
            return f.set(pid, dev)
    except Exception:  # noqa: BLE001
        return False


def routed_to(pid: int) -> str:
    """The output id the program of process `pid` is set to, or "" for the default."""
    if not _WIN:
        return ""
    try:
        with _Factory() as f:
            got = f.get(pid)
    except Exception:  # noqa: BLE001
        return ""
    return got[len(PREFIX):-len(SUFFIX)] if got.startswith(PREFIX) and got.endswith(SUFFIX) else got
