"""What Mac and Linux print differently from Windows: reading each form, on any system."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from lightshow import govee_lan
from lightshow.studio import link_health, linux_audio, mac_capture, player_browser


class TestArp(unittest.TestCase):
    def test_windows(self):
        text = "  192.168.1.178       d0-c9-07-aa-01-02     dynamic\n"
        self.assertEqual(govee_lan.parse_arp(text), {"192.168.1.178": "D0:C9:07:AA:01:02"})

    def test_a_mac_drops_leading_zeros(self):
        text = "? (192.168.1.178) at d0:c9:7:aa:1:2 on en0 ifscope [ethernet]\n? (192.168.1.1) at (incomplete) on en0\n"
        self.assertEqual(govee_lan.parse_arp(text), {"192.168.1.178": "D0:C9:07:AA:01:02"})

    def test_linux_arp_and_ip_neigh(self):
        self.assertEqual(govee_lan.parse_arp("? (192.168.1.178) at d0:c9:07:aa:01:02 [ether] on wlan0\n"), {"192.168.1.178": "D0:C9:07:AA:01:02"})
        self.assertEqual(govee_lan.parse_arp("192.168.1.178 dev wlan0 lladdr d0:c9:07:aa:01:02 REACHABLE\n192.168.1.9 dev wlan0 FAILED\n"),
                         {"192.168.1.178": "D0:C9:07:AA:01:02"})

    def test_macs_stay_as_they_were_on_windows(self):
        self.assertEqual(govee_lan.normalize_mac("d0-c9-07-aa-01-02"), "D0:C9:07:AA:01:02")


class TestPing(unittest.TestCase):
    def test_the_timeout_is_a_second_on_each_system(self):
        self.assertEqual(link_health.ping_args("1.2.3.4", "darwin"), ["ping", "-c", "1", "-W", "1000", "1.2.3.4"])   # milliseconds
        self.assertEqual(link_health.ping_args("1.2.3.4", "linux"), ["ping", "-c", "1", "-W", "1", "1.2.3.4"])       # seconds

    def test_reads_the_round_trip_it_reports(self):
        self.assertEqual(link_health.ping_ms("64 bytes from 1.2.3.4: icmp_seq=0 ttl=255 time=12.345 ms\n"), 12.345)
        self.assertEqual(link_health.ping_ms("64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=3 ms\n"), 3.0)
        self.assertIsNone(link_health.ping_ms("Request timeout for icmp_seq 0\n"))


class TestLinuxOutput(unittest.TestCase):
    def fake_pactl(self, sinks="0\talsa_output.pci\tmodule\ts16le\tRUNNING", default="alsa_output.pci"):
        self.calls = []

        def run(cmd, **kw):
            self.calls.append(cmd[1:])
            out = {"list": sinks, "get-default-sink": default, "load-module": "42"}.get(cmd[1], "")
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")
        return run

    def setUp(self):
        linux_audio._module.clear()

    def test_makes_the_output_once_and_leaves_the_default_alone(self):
        with mock.patch.object(linux_audio, "available", lambda: True), mock.patch.object(subprocess, "run", self.fake_pactl()):
            self.assertEqual(linux_audio.ensure_sink(), linux_audio.DESCRIPTION)
        load = next(c for c in self.calls if c[0] == "load-module")
        self.assertIn(f"sink_name={linux_audio.SINK}", load)
        self.assertNotIn("set-default-sink", [c[0] for c in self.calls])

    def test_already_there_makes_no_second_one(self):
        sinks = f"0\talsa\tm\ts16le\tIDLE\n7\t{linux_audio.SINK}\tm\ts16le\tIDLE"
        with mock.patch.object(linux_audio, "available", lambda: True), mock.patch.object(subprocess, "run", self.fake_pactl(sinks)):
            self.assertEqual(linux_audio.ensure_sink(), linux_audio.DESCRIPTION)
        self.assertNotIn("load-module", [c[0] for c in self.calls])

    def test_puts_the_default_back_if_it_moved(self):
        answers = iter(["old-default", "lightshow_hidden"])

        def run(cmd, **kw):
            out = {"list": "", "load-module": "42"}.get(cmd[1]) if cmd[1] != "get-default-sink" else next(answers)
            self_calls.append(cmd[1:])
            return subprocess.CompletedProcess(cmd, 0, stdout=(out or "") + "\n", stderr="")
        self_calls = []
        with mock.patch.object(linux_audio, "available", lambda: True), mock.patch.object(subprocess, "run", run):
            linux_audio.ensure_sink()
        self.assertIn(["set-default-sink", "old-default"], self_calls)

    def test_removes_only_what_it_made(self):
        with mock.patch.object(subprocess, "run", self.fake_pactl()):
            linux_audio.remove_sink()
            self.assertEqual(self.calls, [])
            linux_audio._module[:] = ["42"]
            linux_audio.remove_sink()
        self.assertEqual(self.calls, [["unload-module", "42"]])

    def test_no_pulseaudio_says_so_rather_than_failing(self):
        with mock.patch.object(linux_audio, "available", lambda: False):
            self.assertIsNone(linux_audio.ensure_sink())


class TestMacCapture(unittest.TestCase):
    def test_needs_14_2(self):
        self.assertTrue(mac_capture.version_ok("14.2"))
        self.assertTrue(mac_capture.version_ok("15.7.9"))
        self.assertTrue(mac_capture.version_ok("26.0"))
        self.assertFalse(mac_capture.version_ok("14.1.2"))
        self.assertFalse(mac_capture.version_ok("12.7"))
        self.assertFalse(mac_capture.version_ok(""))

    def test_frames_from_the_helper(self):
        raw = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32).tobytes()
        self.assertEqual(mac_capture.to_frames(raw, 2, 2).tolist()[0], [np.float32(0.1), np.float32(0.2)])
        self.assertEqual(mac_capture.to_frames(raw, 2, 1).shape, (2, 1))
        self.assertEqual(mac_capture.to_frames(np.array([0.5, 0.6], dtype=np.float32).tobytes(), 1, 2).shape, (2, 2))

    def resampled(self, rate_in, rate_out, block):
        tone = np.sin(2 * np.pi * 440 * np.arange(rate_in * 2) / rate_in).astype(np.float32)[:, None]
        r = mac_capture.Resampler(rate_in, rate_out)
        return np.concatenate([r.feed(tone[i:i + block]) for i in range(0, len(tone), block)])

    def test_resampling_keeps_pitch_length_and_has_no_seams(self):
        for block in (480, 512, 1024, 7):
            out = self.resampled(48000, 44100, block)
            self.assertAlmostEqual(len(out), 88200, delta=3, msg=f"block {block}")
            self.assertAlmostEqual(np.abs(np.fft.rfft(out[:88200, 0])).argmax() / 2, 440, delta=1)
            step = 2 * np.sin(np.pi * 440 / 44100) * 1.05      # the largest a 440 Hz tone moves between frames
            self.assertLess(np.abs(np.diff(out[:, 0])).max(), step, f"a seam at block {block}")

    def test_the_same_rate_passes_through(self):
        x = np.ones((10, 2), dtype=np.float32)
        self.assertIs(mac_capture.Resampler(44100, 44100).feed(x), x)

    @unittest.skipIf(os.name == "nt", "select on a pipe needs Unix")
    def test_reads_a_helper_to_its_end(self):
        script = ("import sys,struct;o=sys.stdout.buffer;o.write(b'{\"rate\": 44100, \"channels\": 2}\\n' + struct.pack('<4f',0.5,0.5,0.25,0.25));"
                  "o.flush();o.write(struct.pack('<2f',-0.5,-0.5)[:6]);o.flush();o.write(struct.pack('<2f',-0.5,-0.5)[6:]);o.flush()")
        got = list(mac_capture.read_tap([sys.executable, "-c", script], 44100, 2, lambda: False))
        frames = np.concatenate(got)
        self.assertEqual(frames.shape, (3, 2))                 # (the first frames came with the first line; a split frame is joined)
        self.assertEqual(frames[0, 0], int(0.5 * 32767))
        self.assertEqual(frames[2, 1], int(-0.5 * 32767))

    def test_follows_the_players_audio_process(self):
        runs = []

        def fake_read(cmd, sr, ch, stop, restart):
            runs.append(cmd)
            yield np.zeros((1, 2), dtype=np.int16)
        calls = iter([[], [101], [202]])
        with mock.patch.object(mac_capture, "read_tap", fake_read), mock.patch.object(mac_capture.time, "sleep"):
            out = []
            gen = mac_capture.frames(44100, 2, lambda: len(out) >= 2, pids=lambda: next(calls), helper="tap")
            for block in gen:
                out.append(block)
        self.assertEqual(runs, [["tap", "101"], ["tap", "202"]])


class TestPlayerBrowser(unittest.TestCase):
    def test_uses_chrome_on_a_mac_and_linux(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.assertEqual(player_browser.player_browser_exe("darwin", exists=lambda p: p == chrome), chrome)
        self.assertIsNone(player_browser.player_browser_exe("darwin", exists=lambda p: False))
        self.assertEqual(player_browser.player_browser_exe("linux", which=lambda n: "/usr/bin/" + n if n == "google-chrome" else None), "/usr/bin/google-chrome")

    def test_never_linux_chromium_which_cannot_play_spotify(self):
        self.assertIsNone(player_browser.player_browser_exe("linux", which=lambda n: "/usr/bin/" + n if "chromium" in n else None))

    def test_says_it_is_an_ordinary_browser_on_each_system(self):
        self.assertIn("Macintosh", player_browser.user_agent("x", "darwin", "156.0.1.2"))
        self.assertIn("X11; Linux", player_browser.user_agent("x", "linux", "156.0.1.2"))
        self.assertIn("Edg/156.0.1.2", player_browser.user_agent("x", "win32", "156.0.1.2"))
        for plat in ("darwin", "linux", "win32"):
            self.assertNotIn("Headless", player_browser.user_agent("x", plat, "156.0.1.2"))

    def test_finds_the_audio_process_in_a_listing(self):
        prof = "/home/u/.cache/player"
        listing = "\n".join([f"101\t/opt/chrome --type=utility --utility-sub-type=audio.mojom.AudioService --user-data-dir={prof}",
                             f"102\t/opt/chrome --type=renderer --user-data-dir={prof}",
                             "103\t/opt/chrome --type=utility --utility-sub-type=audio.mojom.AudioService --user-data-dir=/home/u/mine"])
        self.assertEqual(player_browser.parse_audio_pids(listing, prof), [101])


class TestPickingTheHiddenOutput(unittest.TestCase):
    def test_never_the_default_or_the_rooms_output(self):
        outs = ["Speakers", "LG Monitor (NVIDIA)", "DELL Monitor (NVIDIA)"]
        self.assertEqual(player_browser.pick_hidden_output(outs, "Speakers", "LG Monitor"), ("DELL Monitor (NVIDIA)", True))

    def test_one_output_has_nothing_to_pick(self):
        self.assertEqual(player_browser.pick_hidden_output(["Speakers"], "Speakers"), ("", False))
        self.assertEqual(player_browser.pick_hidden_output([], ""), ("", False))

    def test_vb_cable_first_and_it_needs_no_muting(self):
        outs = ["Speakers", "Digital Audio (S/PDIF)", "CABLE Input (VB-Audio Virtual Cable)"]
        self.assertEqual(player_browser.pick_hidden_output(outs, "Speakers"), ("CABLE Input (VB-Audio Virtual Cable)", False))

    def test_the_least_likely_to_be_in_use_next(self):
        outs = ["Speakers", "Headset (Bluetooth)", "LG Monitor", "Digital Audio (S/PDIF)"]
        self.assertEqual(player_browser.pick_hidden_output(outs, "Speakers"), ("Digital Audio (S/PDIF)", True))
        self.assertEqual(player_browser.pick_hidden_output(["Speakers", "Headset (Bluetooth)", "LG Monitor"], "Speakers"), ("LG Monitor", True))
        self.assertEqual(player_browser.pick_hidden_output(["Speakers", "Headset (Bluetooth)"], "Speakers"), ("Headset (Bluetooth)", True))


class TestMutingThePickedOutput(unittest.TestCase):
    """Windows only in real life; the calls to Windows are fakes here, so this runs anywhere and mutes nothing."""

    def setUp(self):
        self.state = {"Digital Audio (S/PDIF)": False}
        self.calls = []

        def device_mute(name, on=None):
            if on is not None:
                self.calls.append((name, on))
                self.state[name] = on
            return self.state.get(name)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for target, name, value in ((player_browser.audio_route, "device_mute", device_mute),
                                    (player_browser, "MUTED_NOTE", os.path.join(self.dir.name, "muted.txt")),
                                    (player_browser, "HOME", self.dir.name),
                                    (player_browser.sys, "platform", "win32")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def player(self, output="auto"):
        return player_browser.HiddenPlayer("http://x/", output)

    def test_mutes_for_as_long_as_it_runs_and_puts_it_back(self):
        p = self.player()
        self.assertTrue(p._mute_output("Digital Audio (S/PDIF)"))
        self.assertTrue(self.state["Digital Audio (S/PDIF)"])
        self.assertTrue(os.path.exists(player_browser.MUTED_NOTE))
        p.stop()
        self.assertFalse(self.state["Digital Audio (S/PDIF)"])
        self.assertFalse(os.path.exists(player_browser.MUTED_NOTE))

    def test_an_output_muted_already_stays_muted_afterwards(self):
        self.state["Digital Audio (S/PDIF)"] = True
        p = self.player()
        self.assertTrue(p._mute_output("Digital Audio (S/PDIF)"))
        p.stop()
        self.assertTrue(self.state["Digital Audio (S/PDIF)"])
        self.assertEqual(self.calls, [])

    def test_vb_cable_is_never_muted(self):
        p = self.player()
        self.assertTrue(p._mute_output("CABLE Input (VB-Audio Virtual Cable)"))
        self.assertEqual(self.calls, [])

    def test_a_refusal_is_said_not_ignored(self):
        p = self.player()
        self.assertFalse(p._mute_output("an output Windows refuses"))

    def test_after_a_crash_the_next_run_puts_it_back(self):
        with open(player_browser.MUTED_NOTE, "w", encoding="utf-8") as fh:
            fh.write("Digital Audio (S/PDIF)")
        self.state["Digital Audio (S/PDIF)"] = True
        player_browser.HiddenPlayer.undo_leftover_mute()
        self.assertFalse(self.state["Digital Audio (S/PDIF)"])
        self.assertFalse(os.path.exists(player_browser.MUTED_NOTE))

    def test_an_output_chosen_in_settings_is_used_as_it_is(self):
        p = self.player("S/PDIF")
        self.assertFalse(p.automatic)
        self.assertEqual(p.output_name(), "S/PDIF")

    def test_says_what_to_do_with_a_single_output(self):
        self.assertIn("VB-Cable", self.player().no_output_note())
        self.assertIn("S/PDIF", self.player("S/PDIF").no_output_note())


if __name__ == "__main__":
    unittest.main()
