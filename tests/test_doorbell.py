"""The Reolink doorbell ringing a cue. A fake doorbell only: nothing on the network is touched."""

import asyncio
import unittest

from lightshow.studio import doorbell as db
from lightshow.studio.doorbell import Doorbell


class Store:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def load(self):
        import copy
        return copy.deepcopy(self.cfg)

    def save(self, cfg):
        import copy
        self.cfg = copy.deepcopy(cfg)


class FakeBaichuan:
    def __init__(self):
        self.callbacks, self.subscribed = {}, False

    def register_callback(self, cid, cb, cmd_id=None, channel=None):
        self.callbacks[cid] = cb

    async def subscribe_events(self):
        self.subscribed = True

    async def check_subscribe_events(self):
        pass

    async def unsubscribe_events(self):
        self.subscribed = False


class FakeHost:
    """A doorbell: channel 0, a visitor button the test presses and lets go of."""

    def __init__(self, host, user, password):
        self.args, self.pressed = (host, user, password), False
        self.baichuan, self.logged_out = FakeBaichuan(), False
        self.channels = [0]

    async def get_host_data(self):
        pass

    def is_doorbell(self, ch):
        return True

    def camera_name(self, ch):
        return "Front door"

    def visitor_detected(self, ch):
        return self.pressed

    async def logout(self):
        self.logged_out = True

    def press(self, down=True):
        self.pressed = down
        for cb in self.baichuan.callbacks.values():
            cb()


class TestDoorbell(unittest.TestCase):
    def setUp(self):
        self.st = Store({"doorbell": {"host": "192.168.1.50", "username": "admin", "password": "secret", "cue": "doorbell", "on": True}})
        self.rung, self.t = [], [1000.0]
        self.hosts = []

        def make(h, u, p):
            self.hosts.append(FakeHost(h, u, p))
            return self.hosts[-1]
        self.bell = Doorbell(self.st.load, self.st.save, self.rung.append, make_host=make, clock=lambda: self.t[0])

    def run_briefly(self, fn):
        async def go():
            self.bell.start()
            await asyncio.sleep(0.05)                    # it connects and subscribes
            fn()
            await self.bell.close()
        asyncio.run(go())

    def test_a_press_rings_the_cue_once(self):
        def presses():
            h = self.hosts[0]
            self.assertTrue(self.bell.connected)
            self.assertEqual(h.args, ("192.168.1.50", "admin", "secret"))
            self.assertTrue(h.baichuan.subscribed)
            h.press(True)
            h.press(True)                                # another event while still held: the same press
            h.press(False)
            self.t[0] += 3
            h.press(True)                                # pressed again 3 s later: the same visitor
            h.press(False)
            self.t[0] += 30
            h.press(True)                                # half a minute later: a new ring
        self.run_briefly(presses)
        self.assertEqual(self.rung, ["doorbell", "doorbell"])
        self.assertTrue(self.hosts[0].logged_out)

    def test_a_cue_that_cannot_run_is_noted_not_raised(self):
        def busy(cid):
            raise RuntimeError("Shot o'clock is still running")
        self.bell.on_ring = busy
        self.run_briefly(lambda: self.hosts[0].press(True))
        self.assertIn("still running", self.bell.note)

    def test_switched_off_or_not_set_up_it_never_connects(self):
        self.bell.save({"on": False})
        self.run_briefly(lambda: None)
        self.assertEqual(self.hosts, [])

    def test_the_password_never_reaches_a_page_and_an_empty_one_keeps_the_saved(self):
        pub = self.bell.save({"host": "192.168.1.51", "password": ""})
        self.assertNotIn("password", pub)
        self.assertTrue(pub["has_password"])
        self.assertEqual(self.st.cfg["doorbell"]["password"], "secret")
        self.assertNotIn("secret", str(self.bell.status()))

    def test_it_tries_again_after_a_failure(self):
        calls = []

        def broken(h, u, p):
            calls.append(1)
            raise ConnectionError("no route to host")
        self.bell.make_host = broken
        old = db.RETRY_S
        db.RETRY_S = 0.01
        try:
            self.run_briefly(lambda: None)
        finally:
            db.RETRY_S = old
        self.assertGreater(len(calls), 1)
        self.assertIn("no route", self.bell.error)


if __name__ == "__main__":
    unittest.main()
