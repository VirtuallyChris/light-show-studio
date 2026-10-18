"""Rooms: lights and speakers by room, and a room switched off or on (lights fade, speakers mute).

Everything here is fake: no light and no speaker is ever touched."""

import time
import unittest
from unittest import mock

from lightshow.studio import rooms as rooms_mod
from lightshow.studio import server
from lightshow.studio.rooms import Rooms, clean


class Store:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def load(self):
        import copy
        return copy.deepcopy(self.cfg)

    def save(self, cfg):
        import copy
        self.cfg = copy.deepcopy(cfg)


class FakeSpeaker:
    """Like a Google speaker: every change chimes, and setting a level unmutes it."""

    def __init__(self, level):
        self.status = mock.Mock(volume_level=level, volume_muted=False)
        self.levels, self.chimes = [], 0

    @property
    def muted(self):
        return self.status.volume_muted

    def set_volume(self, v):
        self.status.volume_level, self.status.volume_muted = v, False
        self.levels.append(round(v, 3))
        self.chimes += 1

    def set_volume_muted(self, m):
        self.status.volume_muted = m
        self.chimes += 1


class FakeCast:
    def __init__(self, speakers):
        self.speakers = speakers

    def _device(self, name):
        return self.speakers.get(name)


class TestRoomList(unittest.TestCase):
    def test_a_light_or_speaker_stands_in_one_room(self):
        out = clean([{"name": "Living room", "lights": ["A", "B"], "speakers": ["Living Home"]},
                     {"name": "Game room", "lights": ["B", "C"], "speakers": ["Living Home", "Game Home"], "on": False},
                     {"name": ""}])
        self.assertEqual(out[0]["lights"], ["A", "B"])
        self.assertEqual(out[1]["lights"], ["C"])
        self.assertEqual(out[1]["speakers"], ["Game Home"])
        self.assertFalse(out[1]["on"])
        self.assertEqual(out[2]["name"], "Room 3")
        self.assertEqual(len({r["id"] for r in out}), 3)

    def test_switching_a_room_off_lists_its_lights(self):
        st = Store()
        r = Rooms(st.load, st.save, touch=False)
        rid = r.replace([{"name": "Living room", "lights": ["A"]}, {"name": "Game room", "lights": ["B"]}])[0]["id"]
        self.assertEqual(r.off_light_keys(), set())
        r.set_on(rid, False)
        self.assertEqual(r.off_light_keys(), {"A"})


class TestSpeakers(unittest.TestCase):
    def setUp(self):
        self.st = Store()
        self.home = FakeSpeaker(0.4)
        self.r = Rooms(self.st.load, self.st.save, FakeCast({"Living Home": self.home}))
        self.rid = self.r.replace([{"name": "Living room", "speakers": ["Living Home"]}])[0]["id"]

    def test_off_mutes_at_once_and_keeps_its_level(self):
        self.r.set_on(self.rid, False)
        self.r.sync_speakers(wait=True)
        self.assertTrue(self.home.muted)
        self.assertEqual(self.home.chimes, 1)                          # one change, not a fade of beeps
        self.assertEqual(self.home.status.volume_level, 0.4)          # its level untouched
        self.assertEqual(self.r.muted(), {"Living Home": 0.4})        # and kept for the page and for later

    def test_on_again_unmutes_at_its_level(self):
        self.r.set_on(self.rid, False)
        self.r.sync_speakers(wait=True)
        self.r.set_on(self.rid, True)
        self.r.sync_speakers(wait=True)
        self.assertFalse(self.home.muted)
        self.assertEqual(self.home.levels, [])                         # never a volume step
        self.assertEqual(self.home.chimes, 2)
        self.assertEqual(self.home.status.volume_level, 0.4)
        self.assertEqual(self.r.muted(), {})

    def test_a_volume_set_while_muted_is_kept_for_later(self):
        self.r.set_on(self.rid, False)
        self.r.sync_speakers(wait=True)
        self.assertTrue(self.r.set_saved_volume("Living Home", 0.6))
        self.assertTrue(self.home.muted)                               # still muted: nothing was sent to it
        self.r.set_on(self.rid, True)
        self.r.sync_speakers(wait=True)
        self.assertEqual(self.home.status.volume_level, 0.6)
        self.assertFalse(self.home.muted)
        self.assertEqual(self.home.chimes, 2)                          # muted, then the new level (which unmutes)

    def test_quitting_unmutes(self):
        self.r.set_on(self.rid, False)
        self.r.sync_speakers(wait=True)
        self.r.unmute_all()
        self.assertFalse(self.home.muted)
        self.assertEqual(self.home.status.volume_level, 0.4)
        self.assertEqual(self.r.muted(), {})

    def test_a_test_copy_never_touches_a_speaker(self):
        r = Rooms(self.st.load, self.st.save, FakeCast({"Living Home": self.home}), touch=False)
        r.set_on(self.rid, False)
        self.assertEqual(r.sync_speakers(wait=True), [])
        self.assertFalse(self.home.muted)
        self.assertEqual(self.home.levels, [])


class FakeLight:
    __hash__ = None                                    # like GoveeDevice (a dataclass): cannot be a dictionary key

    def __init__(self, ip, mac=""):
        self.ip, self.mac, self.device_id, self.sku = ip, mac, "", "H6076"
        self.calls, self.frames = [], []

    def razer(self, on):
        self.calls.append(("razer", on))

    def turn(self, on):
        self.calls.append(("turn", on))

    def brightness(self, b):
        self.calls.append(("brightness", b))

    def color(self, r, g, b):
        self.calls.append(("color", (r, g, b)))

    def white(self, k):
        self.calls.append(("white", k))

    def razer_leds(self, cols, gradient=False):
        self.frames.append(cols)


class TestLightsLeaveAndArrive(unittest.TestCase):
    def setUp(self):
        self.st = Store({"light_states": {"AA": {"onOff": 1, "brightness": 25, "color": {"r": 255, "g": 0, "b": 0}}}})
        self.patches = [mock.patch.object(server, "load_config", self.st.load), mock.patch.object(server, "save_config", self.st.save),
                        mock.patch.object(server.govee_lan, "statuses", lambda devs, timeout=1.0: {}),
                        mock.patch.object(server.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()
        self.a, self.b = FakeLight("10.0.0.1", "AA"), FakeLight("10.0.0.2", "BB")
        self.out = server.LightOutput(enabled=True)
        self.out.select([self.a.ip, self.b.ip], [self.a, self.b])
        self.out.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_frames_go_to_the_light_they_were_drawn_for(self):
        self.out.frame([[0, 0, 9], [9, 0, 0]], [self.b.ip, self.a.ip])
        self.assertEqual(self.a.frames[-1], [(9, 0, 0)])
        self.assertEqual(self.b.frames[-1], [(0, 0, 9)])

    def test_an_unticked_light_goes_back_even_if_it_never_answered(self):
        self.out.saved.pop(self.b.ip, None)                            # its state could not be read
        dropped, _ = self.out.select([self.a.ip], [self.a, self.b])
        self.b.calls.clear()
        self.out.reselect(dropped, [])
        self.assertIn(("razer", False), self.b.calls)

    def test_a_light_that_never_answered_gets_its_state_from_last_time(self):
        self.assertEqual(self.out.saved.get(self.a.ip, {}).get("brightness"), 25)

    def test_leaving_fades_to_how_it_was_then_is_handed_back(self):
        dropped, _ = self.out.select([self.b.ip], [self.a, self.b])
        self.out.leave(dropped, 1.0)
        show = [[0, 255, 0], [0, 0, 255]]
        self.out.frame(show, [self.a.ip, self.b.ip])
        self.assertEqual(self.a.frames[-1], [(0, 255, 0)])            # still the show at the start
        self.out.leaving[self.a.ip]["t0"] -= 1.0                       # the fade has run
        self.out.last_sent = 0
        self.out.frame(show, [self.a.ip, self.b.ip])
        self.assertEqual(self.a.frames[-1], [(64, 0, 0)])             # red at 25 %: how it was
        self.a.calls.clear()
        self.out.finish_leaving()
        self.assertIn(("razer", False), self.a.calls)
        self.assertIn(("brightness", 25), self.a.calls)
        self.assertNotIn(self.a.ip, self.out.leaving)

    def test_arriving_fades_from_how_it_was_into_the_show(self):
        dropped, _ = self.out.select([self.b.ip], [self.a, self.b])
        self.out.leave(dropped, 1.0)
        self.out.leaving[self.a.ip]["t0"] -= 1.0
        _, added = self.out.select([self.a.ip, self.b.ip], [self.a, self.b])
        self.out.arrive(added, 1.0)
        show = [[0, 255, 0], [0, 0, 255]]
        self.out.last_sent = 0
        self.out.frame(show, [self.a.ip, self.b.ip])
        self.assertEqual(self.a.frames[-1], [(64, 0, 0)])             # picks up from where the fade out ended
        self.out.arriving[self.a.ip]["t0"] -= 1.0
        self.out.last_sent = 0
        self.out.frame(show, [self.a.ip, self.b.ip])
        self.assertEqual(self.a.frames[-1], [(0, 255, 0)])


if __name__ == "__main__":
    unittest.main()


class TestKeptLevels(unittest.TestCase):
    """The levels set on the Party Hub are kept and the party's speakers held to them."""

    def setUp(self):
        from types import SimpleNamespace
        from lightshow.studio.rooms import Rooms
        self.cfg = {}
        self.calls = []
        self.spk = {}

        def device(name):
            return self.spk.get(name)
        self.cast = SimpleNamespace(_device=device)
        self.rooms = Rooms(lambda: dict(self.cfg), lambda c: self.cfg.update(c), cast=self.cast)
        self.t = 0.0

    def speaker(self, name, level, muted=False, kind="audio"):
        from types import SimpleNamespace
        st = SimpleNamespace(volume_level=level, volume_muted=muted)
        self.spk[name] = SimpleNamespace(status=st, cast_info=SimpleNamespace(cast_type=kind),
                                         set_volume=lambda v, st=st: (self.calls.append(("set", name, v)), setattr(st, "volume_level", v), setattr(st, "volume_muted", False)),
                                         set_volume_muted=lambda m, st=st: (self.calls.append(("mute", name, m)), setattr(st, "volume_muted", m)))
        return st

    def look(self, *names):
        self.t += 5.0
        return self.rooms.keep_levels(list(names), clock=lambda: self.t)

    def test_a_level_turned_down_on_the_speaker_is_set_back(self):
        st = self.speaker("Living Room", 0.6)
        self.look("Living Room")                                     # first seen: 0.6 is kept
        self.assertEqual(self.rooms.levels(), {"Living Room": 0.6})
        st.volume_level = 0.2                                        # someone turned it down
        self.assertEqual(self.look("Living Room"), [])              # once: could be in passing
        self.assertEqual(self.look("Living Room"), ["Living Room: 0.20 -> 0.60"])
        self.assertEqual(self.calls, [("set", "Living Room", 0.6)])   # one change: one chime

    def test_a_level_set_on_the_party_hub_is_the_one_kept(self):
        st = self.speaker("Game Room", 0.5)
        self.rooms.remember_level("Game Room", 0.8)
        st.volume_level = 0.8
        self.look("Game Room"); self.look("Game Room")
        self.assertEqual(self.calls, [])

    def test_muted_on_the_speaker_is_unmuted_but_not_a_room_switched_off(self):
        st = self.speaker("Office", 0.4)
        self.look("Office")
        st.volume_muted = True
        self.look("Office"); self.look("Office")
        self.assertEqual(self.calls, [("mute", "Office", False)])
        self.cfg["room_muted"] = {"Office": 0.4}                     # its room is off: left muted
        st.volume_muted = True
        self.look("Office"); self.look("Office")
        self.assertEqual(len(self.calls), 1)

    def test_never_more_than_once_in_ten_seconds_and_never_a_group(self):
        st = self.speaker("Living Room", 0.6)
        self.speaker("Party", 0.3, kind="group")
        self.look("Living Room", "Party")
        st.volume_level = 0.1
        self.look("Living Room", "Party"); self.look("Living Room", "Party")
        st.volume_level = 0.1                                        # turned down again at once
        self.t += 0.0
        self.assertEqual(self.rooms.keep_levels(["Living Room"], clock=lambda: self.t + 1), [])
        self.assertNotIn("Party", self.rooms.levels())
