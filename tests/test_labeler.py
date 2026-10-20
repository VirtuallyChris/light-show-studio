"""The learned section labeler: its bar labels smoothed into sections, and where its sections play."""

import unittest
from unittest import mock

import numpy as np

from lightshow.studio import labeler
from lightshow.studio.analysis import settle_sections


def probs(labels, sure=0.9):
    P = np.full((len(labels), 6), (1 - sure) / 5)
    for i, c in enumerate(labels):
        P[i, c] = sure
    return P


class SmoothTest(unittest.TestCase):
    def test_a_one_bar_flicker_is_folded_into_its_neighbors(self):
        V, D = 1, 3
        lab = labeler.smooth(probs([V] * 6 + [D] + [V] * 5 + [D] * 8))
        self.assertEqual(list(lab), [V] * 12 + [D] * 8)

    def test_runs_become_sections_from_the_start_to_the_end(self):
        spans = np.array([[1.0 + 2 * i, 3.0 + 2 * i] for i in range(6)])
        secs = labeler.to_sections(np.array([0, 0, 1, 1, 3, 3]), spans, 14.0)
        self.assertEqual([(s["type"], s["start"], s["end"]) for s in secs],
                         [("intro", 0.0, 5.0), ("verse", 5.0, 9.0), ("drop", 9.0, 14.0)])


class WherePlaysTest(unittest.TestCase):
    def song(self, **kw):
        return {"sections": [{"type": "verse", "start": 0.0, "end": 10.0}], "drops": [], "beats": [], "duration": 10.0, **kw}

    def test_the_labelers_sections_replace_the_rules(self):
        with mock.patch.object(labeler, "apply", return_value=[{"type": "drop", "start": 0.0, "end": 10.0}]):
            a = self.song(track_id="x")
            settle_sections(a)
        self.assertEqual([s["type"] for s in a["sections"]], ["drop"])
        self.assertEqual([s["type"] for s in a["sections_rules"]], ["verse"])
        self.assertEqual(a["drops"], [0.0])

    def test_the_users_own_sections_win(self):
        with mock.patch.object(labeler, "apply", return_value=[{"type": "drop", "start": 0.0, "end": 10.0}]) as ap:
            a = self.song(track_id="x", sections_truth=[{"type": "breakdown", "start": 0.0, "end": 10.0}])
            settle_sections(a)
        self.assertEqual([s["type"] for s in a["sections"]], ["breakdown"])
        ap.assert_not_called()

    def test_without_an_answer_the_rules_stand(self):
        with mock.patch.object(labeler, "apply", return_value=None):
            a = self.song(track_id="x")
            settle_sections(a)
        self.assertEqual([s["type"] for s in a["sections"]], ["verse"])
        self.assertNotIn("sections_rules", a)

    def test_training_reads_the_rules_alone(self):
        with mock.patch.object(labeler, "apply") as ap:
            settle_sections(self.song(track_id="x", _no_labeler=True))
        ap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
