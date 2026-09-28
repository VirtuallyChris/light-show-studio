"""Where Windows keeps per-user local data, asked of Windows itself.

The LOCALAPPDATA environment variable is usually set, but a process started with a
trimmed environment (by a tool, a service, a scheduler) may not have it, and then the
model environment and the hidden player's profile under it seem to be missing. The
shell's known-folder lookup does not depend on the environment.
"""

from __future__ import annotations

import os
import sys


def local_appdata() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_LocalAppData {F1B32785-6FBA-4FCF-9D55-7B8E7F157091}
            fid = GUID(0xF1B32785, 0x6FBA, 0x4FCF, (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91))
            out = ctypes.c_wchar_p()
            shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
            if shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                try:
                    if out.value:
                        return out.value
                finally:
                    ole32.CoTaskMemFree(out)
        except Exception:  # noqa: BLE001 - fall back to the environment
            pass
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
