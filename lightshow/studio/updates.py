"""Keeping a downloaded app up to date, the way Discord does: on opening, a new release is downloaded and installed
in a small window, and then the new version opens. A copy run from source never updates itself (git pull does that).

Opening must always work. Offline, a slow answer, a failed download or an app that cannot replace itself (running
from the disk image, say) all mean the version already here opens, and Skip in the window does the same. A relaunch
after installing passes --no-update, so a failed install opens the old version instead of trying again in a loop.

While the app is open it looks every few hours, and if there is a new release Settings > Updates says so, with a
button to restart into it. Nothing restarts by itself: that could be in the middle of a party.

    GET  /api/update     {packaged, version, latest, available, can_install, page, auto_update, error}
    POST /api/update     {auto_update?, check?, restart?}: from this computer only
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from ..appdirs import FROZEN, code_dir, data_dir

LATEST_URL = "https://api.github.com/repos/VirtuallyChris/light-show-studio/releases/latest"
CHECK_AT_LAUNCH_S = 4.0                  # the longest opening waits to hear whether there is a new version
CHECK_EVERY_S = 6 * 3600
STALL_S = 30.0                           # a download that sends nothing for this long has failed
HERE = os.path.dirname(os.path.abspath(__file__))


class Skipped(Exception):
    """Skip was pressed in the updating window."""


def version() -> str:
    """This app's version, written into it when it was built. "" from source, and for a build not made from a tag."""
    if not FROZEN:
        return ""
    try:
        with open(os.path.join(code_dir(), "lightshow", "VERSION"), encoding="utf-8") as fh:
            v = fh.read().strip()
    except OSError:
        return ""
    return "" if v in ("", "0.0.0") else v


def parse(v: str) -> tuple[int, ...]:
    """"v1.2.3" as (1, 2, 3). () for anything else, which is never newer."""
    try:
        return tuple(int(p) for p in v.strip().lstrip("v").split("."))
    except ValueError:
        return ()


def newer(candidate: str, current: str) -> bool:
    a, b = parse(candidate), parse(current)
    return bool(a) and bool(b) and a > b


def asset_name(plat: str = sys.platform, machine: str | None = None) -> str:
    """The release file for this system: the names the release workflow and the README's buttons use."""
    machine = machine or platform.machine()
    if plat == "win32":
        return "LightShowStudio-Windows.exe"
    if plat == "darwin":
        return "LightShowStudio-Mac-Intel.dmg" if machine == "x86_64" else "LightShowStudio-Mac.dmg"
    return "LightShowStudio-Linux.AppImage"


def _context() -> ssl.SSLContext:
    """A packaged app can't rely on the system's certificates (Python on a Mac has none of its own): certifi's."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": "LightShowStudio", "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout, context=_context())


def latest(timeout: float = 10.0, url: str = LATEST_URL) -> dict:
    """The newest release: {version, page, assets: {name: {url, size, sha256}}}."""
    with _open(url, timeout) as r:
        doc = json.loads(r.read().decode("utf-8"))
    assets = {}
    for a in doc.get("assets") or []:
        digest = str(a.get("digest") or "")
        assets[a.get("name")] = {"url": a.get("browser_download_url"), "size": int(a.get("size") or 0),
                                 "sha256": digest[7:] if digest.startswith("sha256:") else ""}
    return {"version": str(doc.get("tag_name") or "").lstrip("v"), "page": doc.get("html_url") or "", "assets": assets}


def install_target(plat: str = sys.platform, exe: str | None = None, env: dict | None = None) -> str:
    """What an update would replace, or "" if this copy cannot replace itself (then it only says there is a new version).

    Windows: an installed app (a bare folder has no uninstaller). Mac: the .app, unless it runs from the disk image or
    from the read-only place macOS moves an app that was never dragged out of Downloads. Linux: the AppImage."""
    exe = exe or sys.executable
    env = os.environ if env is None else env
    if plat == "win32":
        return exe if os.path.exists(os.path.join(os.path.dirname(exe), "unins000.exe")) else ""
    if plat == "darwin":
        i = exe.find(".app/")
        app = exe[:i + 4] if i > 0 else ""
        if not app or app.startswith("/Volumes/") or "/AppTranslocation/" in app or not os.access(os.path.dirname(app), os.W_OK):
            return ""
        return app
    image = env.get("APPIMAGE") or ""
    return image if image and os.access(os.path.dirname(image), os.W_OK) else ""


def download(asset: dict, dest: str, progress=lambda frac: None, stop=lambda: False) -> str:
    """The release file, in `dest`, checked against the size and digest GitHub gives. Raises if anything is off."""
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    total, got, h = asset.get("size") or 0, 0, hashlib.sha256()
    try:
        with _open(asset["url"], STALL_S) as r, open(tmp, "wb") as out:
            while True:
                if stop():
                    raise Skipped()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    progress(min(got / total, 1.0))
        if total and got != total:
            raise ValueError(f"got {got} bytes of {total}")
        if asset.get("sha256") and h.hexdigest() != asset["sha256"]:
            raise ValueError("the download does not match its digest")
        os.replace(tmp, dest)
        return dest
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def clean_env() -> dict:
    """The environment for a new top-level copy of the app: without what this packaged process's own start set up."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and not k.startswith("_MEI")}
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    elif FROZEN:
        env.pop("LD_LIBRARY_PATH", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def windows_script(installer: str, exe: str, log: str) -> str:
    """Waits a moment for this app to end, runs the installer (its own small progress window, no questions, closing
    whatever still holds the app's files, such as Bulb Show), and opens the app whether or not that worked."""
    q = lambda p: '"' + p.replace("%", "%%") + '"'  # noqa: E731
    return "\r\n".join(["@echo off", "chcp 65001 >nul", "ping -n 3 127.0.0.1 >nul",
                        f"{q(installer)} /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS /LOG={q(log)}",
                        f'start "" {q(exe)} studio --no-update', ""])


def mac_script(dmg: str, app: str) -> str:
    """Swaps in the .app from the disk image, putting the old one back if that fails, and opens the app either way."""
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"  # noqa: E731
    name = os.path.basename(app)
    return "\n".join(["#!/bin/sh", f"APP={q(app)}", f"DMG={q(dmg)}", 'MNT="$(mktemp -d)"', "sleep 2",
                      'if hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG"; then',
                      f'  if ditto "$MNT/{name}" "$APP.new"; then',
                      '    rm -rf "$APP.old"',
                      '    if mv "$APP" "$APP.old" && mv "$APP.new" "$APP"; then rm -rf "$APP.old"; else mv "$APP.old" "$APP"; fi',
                      "  fi",
                      '  hdiutil detach "$MNT" -quiet',
                      "fi",
                      'rm -rf "$APP.new"',
                      'open -n "$APP" --args studio --no-update', ""])


def install(path: str, target: str, plat: str = sys.platform) -> None:
    """Hands over to the new version. The caller ends this process straight after."""
    folder = os.path.dirname(path)
    if plat == "win32":
        script = os.path.join(folder, "install.cmd")
        with open(script, "w", encoding="utf-8", newline="") as fh:
            fh.write(windows_script(path, target, os.path.join(folder, "install.log")))
        subprocess.Popen(["cmd", "/c", script], env=clean_env(), cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    elif plat == "darwin":
        script = os.path.join(folder, "install.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(mac_script(path, target))
        subprocess.Popen(["/bin/sh", script], env=clean_env(), cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        os.chmod(path, 0o755)                     # (an AppImage can be replaced while it runs)
        os.replace(path, target)
        subprocess.Popen([target, "studio", "--no-update"], env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def updates_dir() -> str:
    return os.path.join(data_dir(), "updates")


def clean() -> None:
    """The installer of the update just installed, and the window's browser profile."""
    shutil.rmtree(updates_dir(), ignore_errors=True)


def note(line: str) -> None:
    """A line in the data folder's update.log: this happens before the studio's own log exists."""
    try:
        with open(os.path.join(data_dir(), "update.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def auto_update_on() -> bool:
    try:
        with open(os.path.join(data_dir(), "config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("auto_update", True) is not False
    except (OSError, ValueError):
        return True


def _browser(plat: str = sys.platform) -> str | None:
    """A Chromium-family browser for the window. (server.find_chromium needs the whole studio imported.)"""
    if plat == "win32":
        bases = [os.environ.get(e) for e in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        paths = [os.path.join(b, *p) for b in bases if b for p in (("Google", "Chrome", "Application", "chrome.exe"), ("Microsoft", "Edge", "Application", "msedge.exe"))]
    elif plat == "darwin":
        paths = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        paths = [p for p in (shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")) if p]
    return next((p for p in paths if os.path.exists(p)), None)


class Splash:
    """The small "Updating" window: its page (static/updating.html) from a little server of its own, as the studio
    itself has not started yet."""

    def __init__(self, new_version: str):
        self.state = {"version": new_version, "phase": "download", "progress": 0.0}
        self.skip = threading.Event()
        self.proc = None
        splash = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, body: bytes, kind: str):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/state":
                    self._send(json.dumps(splash.state).encode(), "application/json")
                else:
                    with open(os.path.join(HERE, "static", "updating.html"), "rb") as fh:
                        self._send(fh.read(), "text/html; charset=utf-8")

            def do_POST(self):
                splash.skip.set()
                self._send(b"{}", "application/json")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True, name="update-window").start()
        url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        exe = _browser()
        if exe:
            try:
                self.proc = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={os.path.join(updates_dir(), 'window')}", "--no-first-run",
                                              "--no-default-browser-check", "--window-size=440,270"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        webbrowser.open(url)

    def close(self) -> None:
        self.state["phase"] = "done"
        time.sleep(0.6)                           # (a page in a browser tab, which can't be closed from here, hears it)
        if self.proc is not None:
            self.proc.terminate()
        self.server.shutdown()


def port_busy(port: int, timeout: float = 0.5) -> bool:
    """Is a studio already answering on this port? (Then this opening only opens another window on it.)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - anything at all means no
        return False


def at_launch(port: int, current: str | None = None, target: str | None = None, fetch=latest, splash=Splash, get=download,
              hand_over=install, leave=os._exit) -> None:
    """Before the studio opens: a new release is installed and the new version opened, and this process ends.
    Returns, for the studio to open as usual, in every other case."""
    current = version() if current is None else current
    target = install_target() if target is None else target
    if not current or not target or not auto_update_on() or port_busy(port):
        return
    try:
        rel = fetch(CHECK_AT_LAUNCH_S)
    except Exception as e:  # noqa: BLE001 - offline, GitHub away, a bad answer: open the app
        note(f"could not check for an update: {type(e).__name__}: {e}")
        return
    asset = rel["assets"].get(asset_name())
    if not newer(rel["version"], current) or not asset:
        return
    window = None
    try:
        clean()
        window = splash(rel["version"])
        path = get(asset, os.path.join(updates_dir(), asset_name()), lambda f: window.state.update(progress=f), window.skip.is_set)
        window.state["phase"] = "install"
        note(f"installing {rel['version']} over {current}")
        hand_over(path, target)
    except Skipped:
        note(f"{rel['version']} skipped")
        window.close()
        return
    except Exception as e:  # noqa: BLE001 - the version here opens instead
        note(f"the update to {rel['version']} failed: {type(e).__name__}: {e}")
        if window is not None:
            window.close()
        return
    window.close()
    leave(0)


class UpdateNotice:
    """While the app is open: says when there is a new release, and restarts into it when asked."""

    def __init__(self, load_config, save_config, live: bool, port: int, quit_app=None, fetch=latest, packaged: bool | None = None,
                 target: str | None = None, spawn=subprocess.Popen):
        """`live`: a real app (a test copy never looks). `quit_app`: an async function that closes the app down."""
        self.load_config, self.save_config, self.live, self.port, self.quit_app = load_config, save_config, live, port, quit_app
        self.fetch, self.spawn = fetch, spawn
        self.current = version()
        self.packaged = bool(self.current) if packaged is None else packaged
        self.target = install_target() if target is None else target
        self.release: dict = {}
        self.error = ""
        self.stop_event = threading.Event()

    def check(self) -> None:
        try:
            self.release, self.error = self.fetch(), ""
        except Exception as e:  # noqa: BLE001 - said in Settings, tried again later
            self.error = f"{type(e).__name__}: {e}"

    def start(self) -> None:
        if not (self.packaged and self.live):
            return

        def run():
            while True:
                self.check()
                if self.stop_event.wait(CHECK_EVERY_S):
                    return
        threading.Thread(target=run, daemon=True, name="update-check").start()

    def status(self) -> dict:
        latest_version = self.release.get("version") or ""
        return {"packaged": self.packaged, "version": self.current, "latest": latest_version,
                "available": newer(latest_version, self.current), "can_install": bool(self.target),
                "page": self.release.get("page") or "", "auto_update": self.load_config().get("auto_update", True) is not False,
                "error": self.error}

    def routes(self) -> list:
        from aiohttp import web
        return [web.get("/api/update", self.h_status), web.post("/api/update", self.h_set)]

    async def h_status(self, request):
        from aiohttp import web
        return web.json_response(self.status())

    async def h_set(self, request):
        import asyncio

        from aiohttp import web
        if request.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"error": "from this computer only"}, status=403)
        body = await request.json()
        if "auto_update" in body:
            cfg = self.load_config()
            cfg["auto_update"] = bool(body["auto_update"])
            self.save_config(cfg)
        if body.get("check") and self.packaged and self.live:
            await asyncio.get_running_loop().run_in_executor(None, self.check)
        st = self.status()
        if body.get("restart") and st["available"] and st["can_install"] and self.live and self.quit_app:
            # the new copy waits for this port to come free, then updates itself as it opens
            self.spawn([sys.executable, "studio", "--restarted", "--port", str(self.port)], env=clean_env(), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **({"start_new_session": True} if os.name != "nt" else {}))
            asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(self.quit_app("Restarting to update.")))
        return web.json_response(st)
