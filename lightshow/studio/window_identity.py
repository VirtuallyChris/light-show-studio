"""Give the studio's window its own place on the taskbar.

The studio runs in a Chrome app window, and Windows files every window under its
program's app ID: pinning the studio pinned Chrome. Real apps set their own ID on
their window, together with how to start them again, their name and their icon.
This does the same for the studio's window, once Chrome has opened it:

  System.AppUserModel.ID                           LightShowStudio.Studio
  System.AppUserModel.RelaunchCommand              the launcher (Light Show Studio.vbs)
  System.AppUserModel.RelaunchDisplayNameResource  Light Show Studio
  System.AppUserModel.RelaunchIconResource         Light Show Studio.ico

Pinning it then pins a Light Show Studio button that starts the whole studio,
server and all, and its windows group under that button, apart from your Chrome.
Off Windows, or if anything refuses, nothing changes.
"""

from __future__ import annotations

import os
import sys
import time

APP_ID = "LightShowStudio.Studio"
NAME = "Light Show Studio"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FMTID = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
PID_ID, PID_RELAUNCH, PID_ICON, PID_NAME = 5, 2, 3, 4
VT_LPWSTR = 31


def relaunch_command() -> str:
    wscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    return f'"{wscript}" "{os.path.join(ROOT, NAME + ".vbs")}"'


def icon_resource() -> str:
    return os.path.join(ROOT, NAME + ".ico") + ",0"


def _ct():
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("b", ctypes.c_ubyte * 16)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                    ("p", ctypes.c_void_p), ("pad", ctypes.c_void_p)]
    return ctypes, wintypes, GUID, PROPERTYKEY, PROPVARIANT


def _guid(GUID, text):
    import ctypes
    import uuid
    g = GUID()
    ctypes.memmove(g.b, uuid.UUID(text).bytes_le, 16)
    return g


class _Store:
    """The window's IPropertyStore, called through its vtable."""

    def __init__(self, hwnd: int):
        ctypes, wintypes, GUID, PROPERTYKEY, PROPVARIANT = _ct()
        self.ct, self.PK, self.PV, self.GUID = ctypes, PROPERTYKEY, PROPVARIANT, GUID
        self.ole = ctypes.WinDLL("ole32")
        self.inited = (self.ole.CoInitializeEx(None, 2) & 0xFFFFFFFF) in (0, 1)   # apartment; undone in close()
        shell = ctypes.WinDLL("shell32")
        shell.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.ptr = ctypes.c_void_p()
        iid = _guid(GUID, "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
        hr = shell.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(self.ptr))
        if hr != 0 or not self.ptr.value:
            self.close()
            raise OSError(f"no property store for the window (0x{hr & 0xFFFFFFFF:08x})")

    def _fn(self, slot, *args):
        ct = self.ct
        vt = ct.cast(ct.cast(self.ptr, ct.POINTER(ct.c_void_p))[0], ct.POINTER(ct.c_void_p))
        return ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *args)(vt[slot])

    def _key(self, pid):
        return self.PK(_guid(self.GUID, _FMTID), pid)

    def set(self, pid: int, text: str) -> bool:
        ct = self.ct
        buf = ct.create_unicode_buffer(text)
        v = self.PV(vt=VT_LPWSTR, p=ct.cast(buf, ct.c_void_p).value)
        key = self._key(pid)
        return self._fn(6, ct.POINTER(self.PK), ct.POINTER(self.PV))(self.ptr, ct.byref(key), ct.byref(v)) == 0

    def get(self, pid: int) -> str | None:
        ct = self.ct
        v, key = self.PV(), self._key(pid)
        if self._fn(5, ct.POINTER(self.PK), ct.POINTER(self.PV))(self.ptr, ct.byref(key), ct.byref(v)) != 0:
            return None
        text = ct.wstring_at(v.p) if v.vt == VT_LPWSTR and v.p else None
        ct.WinDLL("ole32").PropVariantClear(ct.byref(v))
        return text

    def commit(self) -> bool:
        return self._fn(7)(self.ptr) == 0

    def close(self):
        if getattr(self, "ptr", None) is not None and self.ptr.value:
            self._fn(2)(self.ptr)                          # IUnknown::Release
            self.ptr = self.ct.c_void_p()
        if getattr(self, "inited", False):
            self.ole.CoUninitialize()
            self.inited = False


def apply(hwnd: int) -> bool:
    """Give one window the studio's taskbar identity. The relaunch details go first, the ID last."""
    if sys.platform != "win32":
        return False
    try:
        st = _Store(hwnd)
    except Exception:  # noqa: BLE001
        return False
    try:
        ok = (st.set(PID_RELAUNCH, relaunch_command()) and st.set(PID_NAME, NAME)
              and st.set(PID_ICON, icon_resource()) and st.set(PID_ID, APP_ID))
        return bool(ok and st.commit())
    finally:
        st.close()


def read(hwnd: int) -> dict:
    """The window's taskbar identity as Windows sees it (for checking)."""
    st = _Store(hwnd)
    try:
        return {"id": st.get(PID_ID), "relaunch": st.get(PID_RELAUNCH), "name": st.get(PID_NAME), "icon": st.get(PID_ICON)}
    finally:
        st.close()


def windows_titled(prefix: str, pids: set[int] | None = None) -> list[int]:
    """Visible top-level windows whose title starts with `prefix` (optionally of these processes)."""
    if sys.platform != "win32":
        return []
    ctypes, wintypes, *_ = _ct()
    user = ctypes.WinDLL("user32")
    found = []
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(h, _):
        if not user.IsWindowVisible(h):
            return True
        n = user.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user.GetWindowTextW(h, buf, n + 1)
        if buf.value.startswith(prefix):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pids is None or pid.value in pids:
                found.append(int(h))
        return True
    user.EnumWindows(PROC(each), 0)
    return found


def profile_pids(profile: str) -> set[int]:
    """Chrome processes running a given profile (the studio window's own)."""
    import subprocess
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe' OR Name='brave.exe'\" | "
          "Where-Object { $_.CommandLine -like '*" + profile.replace("'", "''") + "*' } | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return set()
    return {int(x) for x in out.split() if x.strip().isdigit()}


def adopt_studio_windows(profile: str, wait_s: float = 20.0) -> int:
    """Wait for the studio's window(s) to appear and give each the studio's identity. Returns how many."""
    done: set[int] = set()
    t_end = time.monotonic() + wait_s
    while time.monotonic() < t_end:
        pids = profile_pids(profile)
        for h in (windows_titled(NAME, pids) if pids else []):       # only the studio profile's windows, never your own Chrome
            if h not in done and apply(h):
                done.add(h)
        if done:
            return len(done)
        time.sleep(1.0)
    return len(done)
