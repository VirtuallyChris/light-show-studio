"""The downloaded app's updates: only ever a real step up, a download checked before it is trusted, and opening always
works, whatever goes wrong. A fake release and fake windows: nothing is installed and GitHub is not asked."""

import asyncio
import hashlib
import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from lightshow.studio import updates


class TestVersions(unittest.TestCase):
    def test_only_a_real_step_up_is_newer(self):
        self.assertTrue(updates.newer("0.1.1", "0.1.0"))
        self.assertTrue(updates.newer("v0.10.0", "0.9.9"))
        self.assertTrue(updates.newer("1.0", "0.9.9"))
        self.assertFalse(updates.newer("0.1.0", "0.1.0"))
        self.assertFalse(updates.newer("0.0.9", "0.1.0"))
        self.assertFalse(updates.newer("nightly", "0.1.0"))
        self.assertFalse(updates.newer("0.1.1", ""))

    def test_from_source_there_is_no_version(self):
        self.assertEqual(updates.version(), "")

    def test_each_system_gets_its_own_file(self):
        self.assertEqual(updates.asset_name("win32", "AMD64"), "LightShowStudio-Windows.exe")
        self.assertEqual(updates.asset_name("darwin", "arm64"), "LightShowStudio-Mac.dmg")
        self.assertEqual(updates.asset_name("darwin", "x86_64"), "LightShowStudio-Mac-Intel.dmg")
        self.assertEqual(updates.asset_name("linux", "x86_64"), "LightShowStudio-Linux.AppImage")


class TestInstallTarget(unittest.TestCase):
    def test_windows_only_when_installed(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "LightShowStudio.exe")
            self.assertEqual(updates.install_target("win32", exe), "")
            open(os.path.join(d, "unins000.exe"), "w").close()
            self.assertEqual(updates.install_target("win32", exe), exe)

    def test_mac_never_from_the_disk_image_or_a_translocated_copy(self):
        tail = "/Light Show Studio.app/Contents/MacOS/LightShowStudio"
        self.assertEqual(updates.install_target("darwin", "/Volumes/Light Show Studio" + tail), "")
        self.assertEqual(updates.install_target("darwin", "/private/var/folders/x/AppTranslocation/y/d" + tail), "")
        self.assertEqual(updates.install_target("darwin", "/some/LightShowStudio"), "")
        with tempfile.TemporaryDirectory() as d:
            app = d.replace("\\", "/") + "/Light Show Studio.app"
            self.assertEqual(updates.install_target("darwin", app + "/Contents/MacOS/LightShowStudio"), app)

    def test_linux_only_as_an_appimage(self):
        self.assertEqual(updates.install_target("linux", "/opt/x/LightShowStudio", {}), "")
        with tempfile.TemporaryDirectory() as d:
            image = os.path.join(d, "LightShowStudio-Linux.AppImage")
            self.assertEqual(updates.install_target("linux", "/tmp/x", {"APPIMAGE": image}), image)


class TestLatest(unittest.TestCase):
    def test_reads_githubs_answer(self):
        doc = {"tag_name": "v0.2.0", "html_url": "https://x/r", "assets": [
            {"name": "LightShowStudio-Mac.dmg", "browser_download_url": "https://x/a", "size": 9, "digest": "sha256:abc"},
            {"name": "other", "browser_download_url": "https://x/b", "size": 1}]}
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps(doc).encode()
        with mock.patch.object(updates, "_open", return_value=reply):
            rel = updates.latest()
        self.assertEqual(rel["version"], "0.2.0")
        self.assertEqual(rel["assets"]["LightShowStudio-Mac.dmg"], {"url": "https://x/a", "size": 9, "sha256": "abc"})
        self.assertEqual(rel["assets"]["other"]["sha256"], "")


class TestDownload(unittest.TestCase):
    BODY = b"the new version" * 1000

    def setUp(self):
        body = self.BODY

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f"
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.dest = os.path.join(self.dir.name, "sub", "f")
        p = mock.patch.object(updates, "_open", lambda url, timeout: __import__("urllib.request").request.urlopen(url, timeout=timeout))
        p.start()
        self.addCleanup(p.stop)

    def asset(self, **over):
        return {"url": self.url, "size": len(self.BODY), "sha256": hashlib.sha256(self.BODY).hexdigest(), **over}

    def test_a_good_download_arrives(self):
        seen = []
        self.assertEqual(updates.download(self.asset(), self.dest, seen.append), self.dest)
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), self.BODY)
        self.assertEqual(seen[-1], 1.0)

    def test_a_wrong_digest_leaves_nothing(self):
        with self.assertRaises(ValueError):
            updates.download(self.asset(sha256="0" * 64), self.dest)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])

    def test_a_short_download_leaves_nothing(self):
        with self.assertRaises(ValueError):
            updates.download(self.asset(size=len(self.BODY) + 1), self.dest)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])

    def test_skip_stops_it(self):
        with self.assertRaises(updates.Skipped):
            updates.download(self.asset(), self.dest, stop=lambda: True)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])


class FakeSplash:
    made: list = []

    def __init__(self, new_version):
        self.state, self.skip, self.closed = {"phase": "download", "progress": 0.0}, threading.Event(), False
        FakeSplash.made.append(self)

    def close(self):
        self.closed = True


class TestAtLaunch(unittest.TestCase):
    NAME = updates.asset_name()
    REL = {"version": "0.2.0", "page": "p", "assets": {NAME: {"url": "u", "size": 1, "sha256": ""}}}

    def setUp(self):
        FakeSplash.made = []
        self.left, self.handed = [], []
        for name, value in (("auto_update_on", lambda: True), ("port_busy", lambda port: False), ("clean", lambda: None), ("note", lambda line: None)):
            p = mock.patch.object(updates, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_it(self, fetch=lambda t: TestAtLaunch.REL, current="0.1.0", target="/app", get=lambda a, d, p, s: "/dl/new"):
        updates.at_launch(8765, current=current, target=target, fetch=fetch, splash=FakeSplash, get=get,
                          hand_over=lambda path, tgt: self.handed.append((path, tgt)), leave=self.left.append)

    def test_a_new_release_installs_and_this_copy_ends(self):
        self.run_it()
        self.assertEqual(self.handed, [("/dl/new", "/app")])
        self.assertEqual(self.left, [0])
        self.assertTrue(FakeSplash.made[0].closed)

    def test_up_to_date_opens_with_no_window(self):
        self.run_it(current="0.2.0")
        self.assertEqual((self.handed, self.left, FakeSplash.made), ([], [], []))

    def test_offline_opens_with_no_window(self):
        def offline(t):
            raise OSError("no network")
        self.run_it(fetch=offline)
        self.assertEqual((self.handed, self.left, FakeSplash.made), ([], [], []))

    def test_a_bad_answer_opens_with_no_window(self):
        def bad(t):
            raise ValueError("not json")
        self.run_it(fetch=bad)
        self.assertEqual((self.handed, self.left), ([], []))

    def test_a_failed_download_opens_this_version(self):
        def fails(a, d, p, s):
            raise OSError("stalled")
        self.run_it(get=fails)
        self.assertEqual((self.handed, self.left), ([], []))
        self.assertTrue(FakeSplash.made[0].closed)

    def test_skip_opens_this_version(self):
        def skipped(a, d, p, s):
            raise updates.Skipped()
        self.run_it(get=skipped)
        self.assertEqual((self.handed, self.left), ([], []))
        self.assertTrue(FakeSplash.made[0].closed)

    def test_an_install_that_will_not_start_opens_this_version(self):
        def fails(path, tgt):
            raise OSError("no")
        updates.at_launch(8765, current="0.1.0", target="/app", fetch=lambda t: self.REL, splash=FakeSplash, get=lambda a, d, p, s: "/x",
                          hand_over=fails, leave=self.left.append)
        self.assertEqual(self.left, [])

    def test_never_asks_in_these_cases(self):
        def boom(t):
            self.fail("should not have looked")
        self.run_it(fetch=boom, current="")               # from source
        self.run_it(fetch=boom, target="")                # cannot replace itself
        with mock.patch.object(updates, "auto_update_on", lambda: False):
            self.run_it(fetch=boom)                       # switched off
        with mock.patch.object(updates, "port_busy", lambda port: True):
            self.run_it(fetch=boom)                       # already open: this only opens a window on it

    def test_no_file_for_this_system_opens_this_version(self):
        self.run_it(fetch=lambda t: {"version": "0.2.0", "page": "p", "assets": {}})
        self.assertEqual((self.handed, self.left, FakeSplash.made), ([], [], []))


class TestScripts(unittest.TestCase):
    def test_windows_runs_without_questions_and_never_loops(self):
        s = updates.windows_script(r"C:\d\up.exe", r"C:\Program 100%\L.exe", r"C:\d\log")
        self.assertIn('"C:\\d\\up.exe" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS', s)
        self.assertIn('start "" "C:\\Program 100%%\\L.exe" studio --no-update', s)

    def test_mac_puts_the_old_app_back_and_never_loops(self):
        s = updates.mac_script("/d/up.dmg", "/Applications/Light Show Studio.app")
        self.assertIn('mv "$APP.old" "$APP"', s)
        self.assertIn('ditto "$MNT/Light Show Studio.app" "$APP.new"', s)
        self.assertIn('open -n "$APP" --args studio --no-update', s)

    @unittest.skipIf(os.name == "nt", "needs a Unix shell")
    def test_mac_script_is_valid_shell(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(updates.mac_script("/d/it's.dmg", "/Applications/Light Show Studio.app"))
        self.addCleanup(os.remove, fh.name)
        self.assertEqual(subprocess.run(["sh", "-n", fh.name]).returncode, 0)

    @unittest.skipIf(os.name == "nt", "AppImages are Linux")
    def test_linux_replaces_the_appimage_and_opens_it_without_updating_again(self):
        with tempfile.TemporaryDirectory() as d:
            new, target = os.path.join(d, "new"), os.path.join(d, "App.AppImage")
            for path, text in ((new, "new"), (target, "old")):
                with open(path, "w") as fh:
                    fh.write(text)
            with mock.patch.object(subprocess, "Popen") as popen:
                updates.install(new, target, "linux")
            with open(target) as fh:
                self.assertEqual(fh.read(), "new")
            self.assertEqual(popen.call_args[0][0], [target, "studio", "--no-update"])

    def test_a_new_copy_does_not_inherit_this_ones_start(self):
        with mock.patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "x", "_MEIPASS2": "y", "KEEP": "z"}):
            env = updates.clean_env()
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
        self.assertNotIn("_MEIPASS2", env)
        self.assertEqual(env["KEEP"], "z")
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")


class FakeRequest:
    def __init__(self, body, remote="127.0.0.1"):
        self.body, self.remote = body, remote

    async def json(self):
        return self.body


class TestNotice(unittest.TestCase):
    def make(self, **kw):
        self.cfg = {}
        self.spawned, self.quits = [], []

        async def quit_app(why):
            self.quits.append(why)
        args = dict(load_config=lambda: self.cfg, save_config=lambda c: self.cfg.update(c), live=True, port=8765, quit_app=quit_app,
                    fetch=lambda: {"version": "0.2.0", "page": "p", "assets": {}}, packaged=True, target="/app",
                    spawn=lambda *a, **k: self.spawned.append(a[0]))
        n = updates.UpdateNotice(**{**args, **kw})
        n.current = "0.1.0"
        return n

    def post(self, n, body, remote="127.0.0.1"):
        async def go():
            r = await n.h_set(FakeRequest(body, remote))
            await asyncio.sleep(0.3)
            return r
        return asyncio.run(go())

    def test_says_when_there_is_a_new_version(self):
        n = self.make()
        self.assertFalse(n.status()["available"])
        n.check()
        self.assertTrue(n.status()["available"])
        self.assertEqual(n.status()["latest"], "0.2.0")

    def test_a_failed_check_is_said_and_forgets_nothing(self):
        n = self.make()
        n.check()

        def offline():
            raise OSError("no network")
        n.fetch = offline
        n.check()
        self.assertTrue(n.status()["available"])
        self.assertIn("no network", n.status()["error"])

    def test_restart_starts_a_new_copy_and_closes_this_one(self):
        n = self.make()
        n.check()
        self.post(n, {"restart": True})
        self.assertEqual(self.spawned[0][1:], ["studio", "--restarted", "--port", "8765"])
        self.assertEqual(len(self.quits), 1)

    def test_restart_only_when_there_is_something_to_install(self):
        n = self.make()
        self.post(n, {"restart": True})                   # nothing new
        self.assertEqual((self.spawned, self.quits), ([], []))
        n = self.make(target="")
        n.check()
        self.post(n, {"restart": True})                   # cannot replace itself
        self.assertEqual((self.spawned, self.quits), ([], []))

    def test_a_test_copy_never_restarts_or_looks(self):
        n = self.make(live=False)
        n.release = {"version": "0.2.0"}
        self.post(n, {"restart": True})
        self.assertEqual((self.spawned, self.quits), ([], []))
        asked = []
        n.fetch = lambda: asked.append(1) or {}
        n.start()
        self.post(n, {"check": True})
        self.assertEqual(asked, [])

    def test_from_another_computer_refused(self):
        n = self.make()
        n.check()
        r = self.post(n, {"restart": True, "auto_update": False}, remote="192.168.1.50")
        self.assertEqual(r.status, 403)
        self.assertEqual((self.spawned, self.cfg), ([], {}))

    def test_the_setting_is_kept(self):
        n = self.make()
        self.assertTrue(n.status()["auto_update"])
        self.post(n, {"auto_update": False})
        self.assertFalse(n.status()["auto_update"])


class TestSplashWindow(unittest.TestCase):
    def test_serves_the_logo_its_page_shows(self):
        with mock.patch.object(updates, "_browser", return_value=None):
            splash = updates.Splash("9.9.9")
        self.addCleanup(splash.close)
        base = f"http://127.0.0.1:{splash.server.server_address[1]}"
        with urllib.request.urlopen(base + "/") as page:
            self.assertIn(b'src="/logo.png"', page.read())
        with urllib.request.urlopen(base + "/logo.png") as logo:
            self.assertEqual(logo.headers["Content-Type"], "image/png")
            self.assertTrue(logo.read().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
