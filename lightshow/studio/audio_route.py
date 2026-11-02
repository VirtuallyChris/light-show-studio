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
SET_SLOT = 25                                          # after IInspectable's 6 and 19 unpublished methods
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
        self.ct = ctypes
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


def device_mute(name_part: str, on: bool | None = None) -> bool | None:
    """Mute or unmute a whole output (on=None only reads it), as its volume control in Windows does. Its state after, or None
    if Windows refused. Muting an output silences it but not a recording of it: the sound is recorded before the mute."""
    if not _WIN:
        return None
    try:
        import ctypes
        dev = device_id(name_part)
        if not dev:
            return None
        ole = ctypes.OleDLL("ole32")
        started = False
        try:
            ole.CoInitializeEx(None, 0)                   # (apartment-threaded)
            started = True
        except OSError:
            pass                                          # this thread is already set up, some other way

        def method(ptr, slot, *argtypes):
            vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
            return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
        held = []
        try:
            enumerator, device, volume = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            clsid, iid = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E"), _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
            ole.CoCreateInstance(ctypes.byref(clsid), None, 23, ctypes.byref(iid), ctypes.byref(enumerator))
            held.append(enumerator)
            if method(enumerator, 5, ctypes.c_wchar_p, ctypes.c_void_p)(enumerator, dev, ctypes.byref(device)) != 0:   # GetDevice
                return None
            held.append(device)
            iid = _guid("5CDF2C82-841E-4546-9722-0CF74078229A")                                                       # IAudioEndpointVolume
            if method(device, 3, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)(device, ctypes.byref(iid), 23, None, ctypes.byref(volume)) != 0:
                return None
            held.append(volume)
            if on is not None and method(volume, 14, ctypes.c_int, ctypes.c_void_p)(volume, int(on), None) != 0:       # SetMute
                return None
            state = ctypes.c_int()
            if method(volume, 15, ctypes.c_void_p)(volume, ctypes.byref(state)) != 0:                                  # GetMute
                return None
            return bool(state.value)
        finally:
            for ptr in reversed(held):
                method(ptr, 2)(ptr)                       # IUnknown::Release
            if started:
                ole.CoUninitialize()
    except Exception:  # noqa: BLE001
        return None


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
