"""The model host's handling of the graphics card's memory, with stand-in models (no GPU, no torch)."""
import threading
import time
import unittest
from unittest import mock

from lightshow.studio import models_host as mh


class OutOfMemoryError(RuntimeError):
    pass


class TestCard(unittest.TestCase):
    def test_alone_waits_for_the_others_and_holds_new_ones_back(self):
        card, order = mh.Card(), []
        started = threading.Event()

        def other():
            with card.shared():
                started.set()
                time.sleep(0.2)
                order.append("other done")

        threading.Thread(target=other).start()
        started.wait()
        with card.alone():
            order.append("alone")
            late = threading.Thread(target=lambda: (card.shared().__enter__(), order.append("late")))
            late.start()
            time.sleep(0.1)
            order.append("alone done")
        late.join(1)
        self.assertEqual(order, ["other done", "alone", "alone done", "late"])

    def fake_torch(self, held_gb, free_gb, total_gb=16.0):
        gb = 2**30
        cuda = mock.Mock(mem_get_info=lambda: (free_gb * gb, total_gb * gb), memory_reserved=lambda: held_gb * gb)
        return mock.Mock(cuda=cuda)

    def test_the_share_leaves_room_for_what_pytorch_does_not_hold(self):
        card, limits = mh.Card(), []
        card.cap, card.limit = 13.0 / 16, limits.append
        # the night's case: PyTorch holds 9 GB, Whisper, the runtime and a second host the rest: 1 GB free
        with mock.patch.object(mh, "_cuda", return_value=self.fake_torch(held_gb=9.0, free_gb=1.0)):
            card.fit()
        self.assertAlmostEqual(limits[-1] * 16, 8.5)                # under what it holds: the next big one runs alone
        with mock.patch.object(mh, "_cuda", return_value=self.fake_torch(held_gb=2.0, free_gb=13.0)):
            card.fit()
        self.assertAlmostEqual(limits[-1], 13.0 / 16)               # room to spare: the cap, never above it

    def test_each_request_fits_the_share_first(self):
        card, fits = mh.Card(), []
        card.fit = lambda: fits.append(1)
        with card.shared():
            pass
        with mock.patch.object(mh, "_free"), card.alone():
            pass
        self.assertEqual(len(fits), 2)

    def test_out_of_memory_is_recognised(self):
        self.assertTrue(mh._out_of_memory(OutOfMemoryError("CUDA out of memory. Tried to allocate 2 GiB")))
        self.assertTrue(mh._out_of_memory(RuntimeError("CUDA error: out of memory")))
        self.assertFalse(mh._out_of_memory(ValueError("not enough audio")))


class TestModelAnswer(unittest.TestCase):
    def _model(self, handler):
        m = mh.Model.__new__(mh.Model)                    # no thread, no loading: just the answering
        m.name, m.state = "stand_in", None
        return m, mock.patch.dict(mh.HANDLERS, {"stand_in": handler})

    def test_out_of_memory_beside_the_others_runs_again_alone(self):
        calls, card = [], mh.Card()

        def handler(state, req, payload):
            calls.append(card.solo)
            if not card.solo:
                raise OutOfMemoryError("CUDA out of memory")
            return {"ok": True}, b""

        m, patch = self._model(handler)
        with patch, mock.patch.object(mh, "CARD", card), mock.patch.object(mh, "_log"):
            self.assertEqual(m._answer({}, b""), ({"ok": True}, b""))
        self.assertEqual(calls, [False, True])

    def test_too_big_even_alone_goes_past_the_cap_once_then_back_under(self):
        card, limits = mh.Card(), []
        card.cap = 0.8
        card.limit = limits.append

        def handler(state, req, payload):
            if not limits or limits[-1] != 1.0:
                raise OutOfMemoryError("CUDA out of memory")
            return {"ok": True}, b""

        m, patch = self._model(handler)
        with patch, mock.patch.object(mh, "CARD", card), mock.patch.object(mh, "_log"):
            self.assertEqual(m._answer({}, b""), ({"ok": True}, b""))
        self.assertEqual(limits, [1.0, 0.8])

    def test_other_errors_are_not_retried(self):
        calls = []

        def handler(state, req, payload):
            calls.append(1)
            raise ValueError("not enough audio")

        m, patch = self._model(handler)
        with patch, mock.patch.object(mh, "CARD", mh.Card()):
            with self.assertRaises(ValueError):
                m._answer({}, b"")
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
