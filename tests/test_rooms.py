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
        self.status = mock.Mock(volume_level=level, volume_muted=False, app_id=None)
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
        self.cast = object()                             # the studio is casting

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

    def test_switching_rooms_touches_no_speaker_unless_the_studio_casts(self):
        self.r.cast.cast = None                                        # the jam plays on this PC
        self.r.set_on(self.rid, False)
        self.assertEqual(self.r.sync_speakers(wait=True), [])
        self.assertFalse(self.home.muted)

    def test_a_room_switched_off_leaves_a_speaker_someone_else_casts_to(self):
        self.home.status.app_id = "233637DE"                          # YouTube, from a phone
        self.r.set_on(self.rid, False)
        self.r.sync_speakers(wait=True)
        self.assertFalse(self.home.muted)
        self.assertEqual(self.r.muted(), {})                           # not muted, so not unmuted later either

    def test_letting_go_of_the_speakers_unmutes_the_ones_the_studio_muted(self):
        from lightshow.studio.cast import CastManager
        self.r.set_on(self.rid, False)
        self.r.sync_speakers(wait=True)
        cm = CastManager(None)
        released = []
        cm.on_release = lambda: released.append(True)
        cm.cast = mock.Mock()
        with mock.patch("lightshow.studio.cast.time.sleep", lambda s: None):
            cm.disconnect()
        for _ in range(50):
            if released:
                break
            __import__("time").sleep(0.01)
        self.assertEqual(released, [True])

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
    """Every speaker's level is kept: set on the Party Hub, or changed in person, and the person in the room wins
    (the user: someone turning a speaker down must not have it turned back up)."""

    def setUp(self):
        from types import SimpleNamespace
        from lightshow.studio.rooms import Rooms
        self.cfg = {}
        self.calls = []
        self.spk = {}

        def device(name):
            return self.spk.get(name)
        self.cast = SimpleNamespace(_device=device, cast=object())     # the studio is casting
        self.rooms = Rooms(lambda: dict(self.cfg), lambda c: self.cfg.update(c), cast=self.cast)
        self.t = 0.0
        self.rooms.clock = lambda: self.t

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

    def test_a_level_changed_on_the_speaker_is_kept_not_turned_back(self):
        st = self.speaker("Living Room", 0.6)
        self.look("Living Room")                                     # first seen: 0.6 is kept
        self.assertEqual(self.rooms.levels(), {"Living Room": 0.6})
        st.volume_level = 0.2                                        # someone turned it down in the room
        self.assertEqual(self.look("Living Room"), [])              # once: could be in passing
        self.assertEqual(self.look("Living Room"), ["Living Room: 0.60 -> 0.20 on the speaker, kept"])
        self.assertEqual(self.calls, [])                             # never turned back
        self.assertEqual(self.rooms.levels(), {"Living Room": 0.2})  # saved: the Party Hub shows it from now on
        self.look("Living Room"); self.look("Living Room")
        self.assertEqual(self.calls, [])

    def test_a_change_the_studio_sent_that_the_speaker_missed_is_sent_again_once(self):
        st = self.speaker("Game Room", 0.3)
        self.look("Game Room")
        self.rooms.remember_level("Game Room", 0.7, sent=True)        # set on the Party Hub; the speaker missed it
        self.look("Game Room")
        self.assertEqual(self.look("Game Room"), ["Game Room: missed 0.70, sent again"])
        self.assertEqual(self.calls, [("set", "Game Room", 0.7)])
        self.assertEqual(st.volume_level, 0.7)

    def test_a_change_long_after_the_party_hub_set_it_is_a_persons(self):
        st = self.speaker("Game Room", 0.7)
        self.rooms.remember_level("Game Room", 0.7, sent=True)
        self.look("Game Room")
        self.t += 30.0                                               # well after the Party Hub's change
        st.volume_level = 0.4                                        # turned down in person
        self.look("Game Room"); self.look("Game Room")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rooms.levels()["Game Room"], 0.4)

    def test_a_level_set_on_the_party_hub_is_the_one_kept(self):
        st = self.speaker("Game Room", 0.5)
        self.rooms.remember_level("Game Room", 0.8)
        st.volume_level = 0.8
        self.look("Game Room"); self.look("Game Room")
        self.assertEqual(self.calls, [])

    def test_muted_by_hand_stays_muted(self):
        st = self.speaker("Office", 0.4)
        self.look("Office")
        st.volume_muted = True                                       # someone muted it in the room
        self.look("Office"); self.look("Office"); self.look("Office")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rooms.levels(), {"Office": 0.4})       # its level is still its level

    def test_a_group_moved_on_purpose_keeps_its_speakers_where_they_went(self):
        a = self.speaker("Kitchen Home", 0.58)
        b = self.speaker("Office speaker", 0.39)
        self.look("Kitchen Home", "Office speaker")
        a.volume_level, b.volume_level = 0.45, 0.30          # the Everywhere group turned down on the Party Hub
        self.rooms.keep_as_they_are(sleep=lambda s: None)
        self.look("Kitchen Home", "Office speaker"); self.look("Kitchen Home", "Office speaker")
        self.assertEqual(self.calls, [])                     # nothing set back
        self.assertEqual(self.rooms.levels(), {"Kitchen Home": 0.45, "Office speaker": 0.3})

    def test_not_casting_no_speaker_is_touched(self):
        """The jam played on this PC while the user's wife cast to the office speaker: hands off."""
        st = self.speaker("Office", 0.4)
        self.look("Office")
        self.cast.cast = None
        st.volume_level = 0.1
        self.look("Office"); self.look("Office")
        self.assertEqual(self.calls, [])

    def test_a_speaker_someone_else_casts_to_is_left_alone(self):
        st = self.speaker("Office", 0.4)
        self.look("Office")
        st.app_id, st.volume_level = "CC32E753", 0.1                # Spotify casting to it from a phone
        self.look("Office"); self.look("Office")
        self.assertEqual(self.calls, [])
        st.app_id = "CC1AD845"                                       # the studio's own stream again
        self.look("Office"); self.look("Office")
        self.assertEqual(self.calls, [])                             # the level they left it at is kept
        self.assertEqual(self.rooms.levels(), {"Office": 0.1})

    def test_a_missed_change_is_sent_again_at_most_once_in_ten_seconds_and_never_a_group(self):
        st = self.speaker("Living Room", 0.1)
        self.speaker("Party", 0.3, kind="group")
        self.look("Living Room", "Party")
        self.rooms.remember_level("Living Room", 0.6, sent=True)
        self.look("Living Room", "Party"); self.look("Living Room", "Party")
        self.assertEqual(self.calls, [("set", "Living Room", 0.6)])
        st.volume_level = 0.1                                        # missed again at once
        self.rooms.remember_level("Living Room", 0.6, sent=True)
        self.assertEqual(self.rooms.keep_levels(["Living Room"], clock=lambda: self.t + 1), [])
        self.assertEqual(self.rooms.keep_levels(["Living Room"], clock=lambda: self.t + 2), [])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("Party", self.rooms.levels())


class TestFullBrightness(unittest.TestCase):
    """Between songs every light the show drives goes to 100%, without asking (a home automation may have
    dimmed it); a light fading back out of the show is left alone, and nothing happens with no show."""

    def test_every_driven_light_to_full_between_songs(self):
        from types import SimpleNamespace
        from unittest import mock
        from lightshow.studio import server
        sent = []
        dev = lambda ip: SimpleNamespace(ip=ip, brightness=lambda v, ip=ip: sent.append((ip, v)))
        out = server.LightOutput(enabled=True)
        out.devices, out.active = [dev("strip"), dev("pole"), dev("leaving")], True
        out.leaving = {"leaving": {}}
        with mock.patch.object(server.time, "sleep", lambda s: None):
            self.assertEqual(out.full_brightness(), 2)
        self.assertEqual(sent, [("strip", 100), ("pole", 100)] * 2)            # said twice (UDP)
        out.active = False
        self.assertEqual(out.full_brightness(), 0)                              # no show: the lights are left alone
