"""Small failures that must not spread: a test copy and your settings, a song that will not load, a reply cut off,
sections past the end of a song. Nothing here touches a light, a speaker or your own data."""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import warnings
from types import SimpleNamespace
from unittest import mock

from lightshow.studio import analysis, calibrate, model_worker, server
from lightshow.studio.party import PartyLibrary
from lightshow.studio.party_service import PartyService
from lightshow.studio.show_moments import Rehearsal, ShowMomentRunner


class TestATestCopyAndYourSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"jam_opener": "Song"}, fh)
        with open(self.path, "rb") as fh:
            self.before = fh.read()
        for p in (mock.patch.object(server, "CONFIG_PATH", self.path), mock.patch.object(server, "_CONFIG_SCRATCH", None)):
            p.start()
            self.addCleanup(p.stop)

    def test_changes_stay_in_memory_and_the_file_is_never_written(self):
        server.keep_config_in_memory()
        cfg = server.load_config()
        self.assertEqual(cfg["party_opener"], "Song")                    # your settings, under today's names
        cfg["party_energy"] = 50
        server.save_config(cfg)
        self.assertEqual(server.load_config()["party_energy"], 50)       # the copy sees its change
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), self.before)

    def test_the_real_app_still_saves(self):
        cfg = server.load_config()
        cfg["party_energy"] = 50
        server.save_config(cfg)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["party_energy"], 50)


class TestAMomentWhoseSongWillNotLoad(unittest.TestCase):
    def test_it_is_played_the_ordinary_way_and_never_sticks_on_starting(self):
        class Broken(Rehearsal):
            def stream_show_moment(self, name):
                raise OSError("no such file")
        cfg = {}
        r = ShowMomentRunner(lambda: dict(cfg), cfg.update, lambda name: 0.1)
        r.replace([{"id": "door", "name": "Doorbell", "show": "Ding"}])
        h = Broken(lambda name: 0.1)
        with mock.patch("lightshow.studio.show_moments.MUSIC_BACK_S", 0.05):
            r.run("door", h)
            r.thread.join(5)
        self.assertFalse(r.thread.is_alive())
        self.assertEqual(r.status()["phase"], "done")
        self.assertIn(("show", "Ding"), h.calls)
        self.assertIn("resume", h.calls)


class TestAFreshStreamIsLeftToSettle(unittest.TestCase):
    def engine(self):
        relay = SimpleNamespace(open_stream=lambda *a, **k: "stream", oldest_time=0.0, capture_time=10.0)
        return SimpleNamespace(lock=threading.Lock(), gate=SimpleNamespace(paused=False), show_moment_hold=False, relay=relay,
                               room_capture_time=lambda: 5.0, cfg=SimpleNamespace(target_delay=5.0), clock=lambda: 123.0, _streamed_at=-1e9)

    def test_a_cast_restart_counts_as_a_fresh_stream(self):
        out = mock.Mock()
        e = self.engine()
        e.output = out
        PartyService._restart_cast(SimpleNamespace(engine=e), out)
        out.start_stream.assert_called_once()
        self.assertEqual(e._streamed_at, 123.0)


class TestSavesAreWhole(unittest.TestCase):
    def test_a_pinned_song_is_saved_by_a_swap_never_written_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            lib = PartyLibrary(d)
            with mock.patch("lightshow.studio.party.os.replace", wraps=os.replace) as swap:
                lib.link("abc", "My show")
            swap.assert_called_once()
            self.assertEqual(PartyLibrary(d).links(), {"abc": "My show"})


class TestAReplyCutOff(unittest.TestCase):
    def test_it_fails_rather_than_succeeding_with_nothing(self):
        host = model_worker.ModelHost()
        ours, theirs = socket.socketpair()
        slot = [threading.Event(), None, b""]
        host.pending[1] = slot
        reader = threading.Thread(target=host._reader, args=(ours,), daemon=True)
        reader.start()
        theirs.sendall(json.dumps({"id": 1, "ok": True, "payload": 100}).encode() + b"\n" + b"only this")
        theirs.close()
        reader.join(5)
        ours.close()
        self.assertTrue(slot[0].is_set())
        self.assertIsNone(slot[1])                                       # as if the host had not answered
        self.assertEqual(slot[2], b"")


class TestSectionsPastTheEnd(unittest.TestCase):
    """A saved analysis whose sections run past its curves (a song cut short, an edited one) still loads."""

    def song(self):
        n = 100                                                          # 100 frames of curve, far shorter than the sections
        return {"bpm": 120.0, "feature_fps": 10.0, "energy": [0.5] * n,
                "curves": {k: [0.5] * n for k in analysis._REPEAT_KEYS},
                "sections": [{"type": "verse", "start": 0.0, "end": 8.0}, {"type": "buildup", "start": 40.0, "end": 48.0},
                             {"type": "drop", "start": 48.0, "end": 64.0}, {"type": "verse", "start": 64.0, "end": 80.0},
                             {"type": "drop", "start": 80.0, "end": 96.0}]}

    def test_flat_buildups_does_not_raise(self):
        self.assertIsInstance(analysis.flat_buildups(self.song()), list)

    def test_repeated_drops_stays_quiet(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertIsInstance(analysis.repeated_drops(self.song()), list)


class TestCalibrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_it(self, *argv, told=False):
        results = [{"offset_ms": 120, "spread_ms": 5, "n": 6}]
        self.cast = mock.Mock()
        with mock.patch.dict(os.environ, {"LSS_DATA_DIR": self.tmp.name}), \
                mock.patch("lightshow.studio.cast.CastManager", self.cast), \
                mock.patch("lightshow.govee_lan.local_ip", return_value="127.0.0.1"), \
                mock.patch.object(calibrate, "write_calibration_file"), \
                mock.patch.object(calibrate, "measure", side_effect=results), \
                mock.patch.object(calibrate, "_tell_studio", return_value=told) as self.tell, \
                mock.patch.object(calibrate.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return calibrate.main(["Everywhere", "--runs", "1", *argv])

    def test_a_running_studio_saves_it_itself(self):
        self.assertEqual(self.run_it(told=True), 0)
        self.tell.assert_called_once_with(8765, "Everywhere", 120)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "config.json")))

    def test_with_no_studio_it_saves_the_file_itself(self):
        self.assertEqual(self.run_it(told=False), 0)
        with open(os.path.join(self.tmp.name, "config.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cast_offsets"], {"Everywhere": 120})

    def test_a_microphone_that_is_not_there_stops_before_any_speaker(self):
        sd = SimpleNamespace(query_devices=lambda: [{"name": "USB Mic", "max_input_channels": 1}])
        with mock.patch.dict(sys.modules, {"sounddevice": sd}), self.assertRaises(SystemExit):
            self.run_it("--mic", "no such mic")
        self.cast.assert_not_called()


class TestLearningAhead(unittest.TestCase):
    def test_the_disk_is_read_before_the_lock(self):
        """Whether a song is learned means opening its file. The poll thread and the audio wait on that lock."""
        from lightshow.studio.fetch_ahead import FetchAhead

        class Library:
            dir = tempfile.gettempdir()

            def has_learned(lib, track_id):
                self.assertFalse(fetch.lock._is_owned(), "read the disk while holding the lock")
                return track_id == "known"

            def summary(lib, track_id):
                return {}

            def match_project(lib, track):
                return None
        fetch = FetchAhead(Library(), keep=lambda: False)
        fetch.is_available = lambda: True
        fetch._ensure_threads = lambda: None
        fetch.wake = mock.Mock()
        tracks = [SimpleNamespace(id="known", label="k", duration=180.0), SimpleNamespace(id="new", label="n", duration=180.0)]
        with mock.patch.object(threading.Thread, "start"):
            fetch.submit(tracks)
        self.assertEqual([t.id for t in fetch.waiting], ["new"])


class TestTheOffsetRoute(unittest.IsolatedAsyncioTestCase):
    async def test_only_this_computer_may_set_it(self):
        saved = {}

        def request(remote):
            async def body():
                return {"device": "Everywhere", "offset_ms": 300}
            return SimpleNamespace(remote=remote, json=body)
        with mock.patch.object(server, "load_config", dict), mock.patch.object(server, "save_config", saved.update):
            await server.Studio.cast_offset_set(None, request("127.0.0.1"))
            self.assertEqual(saved, {"cast_offsets": {"Everywhere": 300}, "cast_offset_ms": 300})
            saved.clear()
            with self.assertRaises(server.web.HTTPForbidden):
                await server.Studio.cast_offset_set(None, request("192.168.1.50"))
            self.assertEqual(saved, {})


class TestTheProjectList(unittest.TestCase):
    def test_the_apps_own_folders_are_not_songs(self):
        import asyncio
        with tempfile.TemporaryDirectory() as d:
            for name in ("_calibration", "A song"):
                os.makedirs(os.path.join(d, name))
                with open(os.path.join(d, name, "song.wav"), "wb") as fh:
                    fh.write(b"x")
            with mock.patch.object(server, "SHOWS_DIR", d):
                r = asyncio.run(server.Studio.list_projects(None, None))
        self.assertEqual([p["name"] for p in json.loads(r.text)], ["A song"])


if __name__ == "__main__":
    unittest.main()
