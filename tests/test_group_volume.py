"""A speaker group's volume (cast.py): moving it never unmutes a speaker that was muted before, by hand or by its
room (the user). Fakes only: nothing here touches a speaker."""

import unittest
from types import SimpleNamespace

from lightshow.studio.cast import CastManager


def speaker(name, level, muted=False, kind="audio", calls=None):
    st = SimpleNamespace(volume_level=level, volume_muted=muted)

    def set_volume(v):
        calls.append(("set", name, v))
        st.volume_level, st.volume_muted = v, False                 # a Google speaker: a level unmutes it

    def set_muted(m):
        calls.append(("mute", name, m))
        st.volume_muted = m
    return SimpleNamespace(cast_info=SimpleNamespace(friendly_name=name, cast_type=kind), status=st,
                           socket_client=SimpleNamespace(is_connected=True), set_volume=set_volume, set_volume_muted=set_muted)


class TestGroupVolume(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.cm = CastManager(None)
        self.a = speaker("Kitchen Home", 0.5, calls=self.calls)
        self.b = speaker("Bedroom Home", 0.3, muted=True, calls=self.calls)   # muted by hand
        self.g = speaker("Everywhere", 0.4, kind="group", calls=self.calls)
        self.cm.casts = [self.g, self.a, self.b]

    def test_which_are_groups_and_which_are_muted(self):
        self.assertTrue(self.cm.is_group("Everywhere"))
        self.assertFalse(self.cm.is_group("Kitchen Home"))
        self.assertEqual(self.cm.muted_speakers(), {"Bedroom Home"})
        self.assertTrue(self.cm.is_muted("Bedroom Home"))

    def test_a_speaker_the_group_unmuted_is_muted_again_the_rest_left_alone(self):
        before = self.cm.muted_speakers()
        self.cm.set_device_volume("Everywhere", 0.6)
        self.b.status.volume_muted = False                           # the group's change unmuted it
        self.a.status.volume_level = 0.7                             # (and moved the other)
        self.assertEqual(self.cm.mute_again(before, sleep=lambda s: None), ["Bedroom Home"])
        self.assertTrue(self.b.status.volume_muted)
        self.assertFalse(self.a.status.volume_muted)
        self.assertEqual(self.calls[-1], ("mute", "Bedroom Home", True))

    def test_still_muted_after_the_group_nothing_is_sent(self):
        before = self.cm.muted_speakers()
        self.calls.clear()
        self.assertEqual(self.cm.mute_again(before, sleep=lambda s: None), [])
        self.assertEqual(self.calls, [])                             # no change: no chime



class TestStaleStatus(unittest.TestCase):
    """A late word about the party's previous stream must not put its position over the new one's (a doorbell looked
    long over at once and its lights were skipped)."""

    def test_a_status_from_the_stream_before_is_ignored(self):
        cm = CastManager(None)
        cm.media_url, cm.last_time, cm.state = "http://pc/party/stream?sid=8", 0.0, "BUFFERING"
        old = SimpleNamespace(player_state="PLAYING", current_time=312.4, content_id="http://pc/party/stream?sid=7", duration=None)
        cm.new_media_status(old)
        self.assertEqual(cm.last_time, 0.0)
        new = SimpleNamespace(player_state="PLAYING", current_time=1.2, content_id="http://pc/party/stream?sid=8", duration=None)
        cm.new_media_status(new)
        self.assertEqual((cm.last_time, cm.state), (1.2, "PLAYING"))

    def test_the_same_stream_written_another_way_is_taken_after_a_while(self):
        cm = CastManager(None)
        cm.media_url = "http://pc/party/stream?sid=8"
        odd = SimpleNamespace(player_state="PLAYING", current_time=3.0, content_id="http://192.168.1.5/party/stream?sid=8", duration=None)
        cm.new_media_status(odd)
        self.assertEqual(cm.last_time, 0.0)
        cm._other_since -= 6.0
        cm.new_media_status(odd)
        self.assertEqual(cm.last_time, 3.0)


if __name__ == "__main__":
    unittest.main()
