"""Bulb Studio from the studio (bulbstudio.py): its shows on the phone remote, switched on and off, and which speakers
they play on. A fake Bulb Studio only: nothing here starts it, casts or lights a bulb."""

import json
import os
import shutil
import tempfile
import unittest

from lightshow.studio.bulbstudio import BulbStudio

WITCH = {"id": "witch", "name": "Witch", "speaker": "Office Home 2", "bulb": {"name": "Bedroom Bulb Left", "via": "cloud"},
         "ready": True, "running": False}
BARE = {"id": "new-show", "name": "New show", "speaker": "", "bulb": None, "ready": False, "running": False}


class FakeApp:
    def __init__(self, open_=True):
        self.open, self.calls, self.shows = open_, [], {"witch": dict(WITCH), "new-show": dict(BARE)}

    def __call__(self, method, url, body=None, timeout=2.0):
        path = url.split("8770", 1)[1]
        self.calls.append((method, path))
        if not self.open:
            raise OSError("connection refused")
        if path == "/api/shows":
            return list(self.shows.values())
        if path == "/api/running":
            return {"shows": {i: {"error": ""} for i, s in self.shows.items() if s["running"]}}
        if path == "/api/settings":
            return {}
        if path == "/api/quit":
            self.open = False
            return {"ok": True}
        if path.startswith("/api/run/"):
            self.shows[path.rsplit("/", 1)[1]]["running"] = True
            return {"ok": True, "error": ""}
        if path.startswith("/api/stop/"):
            self.shows[path.rsplit("/", 1)[1]]["running"] = False
            return {"ok": True}
        raise AssertionError(path)


class TestBulbStudio(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for sid, s in (("witch", {"name": "Witch", "duration": 4.0, "speaker": "Office Home 2", "bulb": {"name": "Bedroom Bulb Left"}}),
                       ("new-show", {"name": "New show", "duration": 0.0, "speaker": "", "bulb": None})):
            os.makedirs(os.path.join(self.dir, sid))
            with open(os.path.join(self.dir, sid, "show.json"), "w", encoding="utf-8") as fh:
                json.dump(s, fh)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, app, launch=True):
        b = BulbStudio(shows_dir=self.dir, launch=launch, http=app, sleep=lambda s: None)
        b.started = 0

        def start():
            b.started += 1
            app.open = True
        b._start_app = start
        return b

    def test_only_the_shows_that_can_run_are_listed_with_their_speaker_and_bulb(self):
        d = self.make(FakeApp()).list()
        self.assertTrue(d["open"])
        self.assertEqual([(s["id"], s["speaker"], s["bulb"], s["on"]) for s in d["shows"]],
                         [("witch", "Office Home 2", "Bedroom Bulb Left", False)])

    def test_with_bulb_show_closed_its_shows_come_from_its_folder_all_off(self):
        d = self.make(FakeApp(open_=False)).list()
        self.assertFalse(d["open"])
        self.assertEqual([(s["id"], s["on"]) for s in d["shows"]], [("witch", False)])

    def test_switching_on_and_off(self):
        app = FakeApp()
        b = self.make(app)
        self.assertTrue(b.set("witch", True)["ok"])
        self.assertEqual(b.speakers_in_use(), {"Office Home 2": "Witch"})
        self.assertTrue(b.list()["shows"][0]["on"])
        self.assertTrue(b.set("witch", False)["ok"])
        self.assertEqual(b.speakers_in_use(), {})
        self.assertEqual(b.started, 0)

    def test_switched_on_while_closed_bulb_show_is_started_first(self):
        app = FakeApp(open_=False)
        b = self.make(app)
        self.assertEqual(b.set("witch", True), {"ok": True, "error": ""})
        self.assertEqual(b.started, 1)
        self.assertIn(("POST", "/api/run/witch"), app.calls)

    def test_its_window_from_the_studio_starts_it_when_closed(self):
        app = FakeApp(open_=False)
        b = self.make(app)
        self.assertEqual(b.open_window(), {"ok": True, "url": "http://127.0.0.1:8770/", "error": ""})
        self.assertEqual(b.started, 1)
        self.assertEqual(b.open_window()["ok"], True)
        self.assertEqual(b.started, 1)                                   # open already: not started again

    def test_a_test_copy_opens_no_window_it_would_have_to_start(self):
        b = self.make(FakeApp(open_=False), launch=False)
        self.assertFalse(b.open_window()["ok"])
        self.assertEqual(b.started, 0)

    def test_a_test_copy_never_starts_bulb_studio(self):
        b = self.make(FakeApp(open_=False), launch=False)
        self.assertFalse(b.set("witch", True)["ok"])
        self.assertEqual(b.started, 0)

    def test_never_on_the_party_s_speaker_and_never_a_show_not_set_up(self):
        app = FakeApp()
        b = self.make(app)
        r = b.set("witch", True, busy={"Office Home 2"})
        self.assertFalse(r["ok"])
        self.assertIn("playing the party", r["error"])
        self.assertFalse(b.set("new-show", True)["ok"])
        self.assertNotIn(("POST", "/api/run/witch"), app.calls)

    def test_closing_the_studio_closes_bulb_show_if_it_is_open(self):
        app = FakeApp()
        b = self.make(app)
        self.assertTrue(b.quit())
        self.assertIn(("POST", "/api/quit"), app.calls)
        self.assertFalse(b.quit())                                # (already closed: nothing to do, no error)
        self.assertEqual(b.started, 0)

    def test_switched_off_while_closed_is_nothing_to_do(self):
        app = FakeApp(open_=False)
        self.assertTrue(self.make(app).set("witch", False)["ok"])
        self.assertEqual(self.make(app).speakers_in_use(), {})


if __name__ == "__main__":
    unittest.main()
