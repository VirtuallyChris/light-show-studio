"""Where the app's code and its data live, run from source or as a packaged download.

From source both are the project folder, as they always were: config.json, shows/ and the logs sit next
to the code. A packaged app (PyInstaller) has its code in a read-only bundle, so its data goes to the
usual per-user folder of each system instead:

    Windows   %LOCALAPPDATA%\\LightShowStudio
    macOS     ~/Library/Application Support/Light Show Studio
    Linux     $XDG_DATA_HOME/light-show-studio  (~/.local/share/light-show-studio)

LSS_DATA_DIR overrides it (the release smoke test uses a scratch folder).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def code_dir() -> str:
    """The folder holding the `lightshow` and `bulbshow` packages (the bundle's own folder when packaged)."""
    return getattr(sys, "_MEIPASS", SOURCE_ROOT) if FROZEN else SOURCE_ROOT


def user_data_dir(plat: str = sys.platform, home: str | None = None, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if plat == "win32":
        from lightshow.studio.winpaths import local_appdata
        return os.path.join(local_appdata(), "LightShowStudio")
    if plat == "darwin":
        return os.path.join(home, "Library", "Application Support", "Light Show Studio")
    return os.path.join(env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "light-show-studio")


def data_dir() -> str:
    """Where config.json, shows/ and the logs go. Made on first use."""
    path = os.environ.get("LSS_DATA_DIR") or (user_data_dir() if FROZEN else SOURCE_ROOT)
    os.makedirs(path, exist_ok=True)
    return path


_DLL_LOCK = threading.Lock()


def popen_outside(cmd: list[str], **kwargs) -> subprocess.Popen:
    """subprocess.Popen for a program outside a packaged app (a browser, the AI models' Python, an installer). On Windows a
    packaged app searches its own folder for DLLs, and a program it starts inherits that: PyTorch then loaded the app's older
    C++ runtime and would not start. So the search goes back to normal for the moment the program starts."""
    if not (FROZEN and sys.platform == "win32"):
        return subprocess.Popen(cmd, **kwargs)
    import ctypes
    set_dir = ctypes.windll.kernel32.SetDllDirectoryW
    set_dir.argtypes = [ctypes.c_wchar_p]
    with _DLL_LOCK:
        set_dir(None)
        try:
            return subprocess.Popen(cmd, **kwargs)
        finally:
            set_dir(sys._MEIPASS)


def run_outside(cmd: list[str], timeout: float | None = None, check: bool = False, capture_output: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for such a program, without a packaged app's library paths either."""
    if not FROZEN:
        return subprocess.run(cmd, timeout=timeout, check=check, capture_output=capture_output, **kwargs)
    if "env" not in kwargs:
        from .studio.updates import clean_env
        kwargs["env"] = clean_env()
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    proc = popen_outside(cmd, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def self_command(module: str, *args: str, frozen: bool = FROZEN, exe: str | None = None) -> list[str]:
    """How to start one of the app's own modules in a new process: `python -m module` from source; the
    packaged app's own executable with --run-module, which lightshow/__main__.py hands to that module."""
    exe = exe or sys.executable
    if frozen:
        return [exe, "--run-module", module, *args]
    return [exe, "-m", module, *args]
