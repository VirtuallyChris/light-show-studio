"""The house map (space.py): where each segment of a mapped light is, and the test patterns played across it."""

import unittest

from lightshow.studio.space import blink_frame, clean, pattern_level, positions


class PositionsTest(unittest.TestCase):
    def test_a_strip_runs_between_its_pins_and_turns_its_corners(self):
        m = {"kind": "strip", "anchors": [{"seg": 0, "x": 0, "y": 0, "z": 8}, {"seg": 10, "x": 10, "y": 0, "z": 8},
                                          {"seg": 20, "x": 10, "y": 10, "z": 8}]}
        pos = positions(m, 21)
        self.assertEqual(pos[0], [0, 0, 8])
        self.assertEqual(pos[5], [5, 0, 8])                         # along the first wall
        self.assertEqual(pos[10], [10, 0, 8])                       # the corner
        self.assertEqual(pos[15], [10, 5, 8])                       # along the second
        self.assertIsNone(positions({"kind": "strip", "anchors": m["anchors"][:1]}, 21))   # one pin: nowhere yet

    def test_a_pole_stands_up_from_its_spot(self):
        pos = positions({"kind": "pole", "x": 1, "y": 2, "z0": 0, "z1": 7, "top_first": False}, 7)
        self.assertEqual([p[:2] for p in pos], [[1, 2]] * 7)
        self.assertLess(pos[0][2], pos[6][2])                       # segment 1 at the bottom
        top = positions({"kind": "pole", "x": 1, "y": 2, "z0": 0, "z1": 7, "top_first": True}, 7)
        self.assertGreater(top[0][2], top[6][2])

    def test_each_tube_hangs_down_from_where_it_was_placed(self):
        tubes = [{"x": 5, "y": 5, "z": 8, "len": 2}] + [None] * 19
        pos = positions({"kind": "tubes", "tubes": tubes}, 240)
        self.assertEqual(pos[0][:2], [5, 5])
        self.assertGreater(pos[0][2], pos[11][2])                   # its top LED first
        self.assertIsNone(pos[12])                                  # the second tube: not placed


class CleanTest(unittest.TestCase):
    def test_a_map_keeps_its_plan_shapes_and_refuses_nonsense(self):
        doc = clean({"plan": {"width_ft": 41, "shapes": {"rooms": [{"name": "Kitchen", "poly": [[0, 0], [1, 0], [1, 1]], "label": [0.2, 0.2]}],
                                                        "walls": [[[0, 0], [1, 0]]], "fixtures": [{"name": "island", "rect": [0, 0, 1, 2]}]}},
                     "lights": {"AA": {"kind": "strip", "anchors": [{"seg": 5, "x": 1, "y": 1, "z": 8}, {"seg": 0, "x": 0, "y": 0, "z": 8}]}}})
        self.assertEqual(doc["plan"]["shapes"]["rooms"][0]["label"], [0.2, 0.2])
        self.assertEqual([a["seg"] for a in doc["lights"]["AA"]["anchors"]], [0, 5])   # pins in segment order
        with self.assertRaises(ValueError):
            clean({"lights": {"AA": {"kind": "laser"}}})


class PatternsTest(unittest.TestCase):
    DOC = {"plan": {"width_ft": 40, "depth_ft": 10}, "center": [20, 5]}

    def test_the_point_check_lights_only_what_is_near_the_spot(self):
        self.assertEqual(pattern_level("near", [10, 5, 8], 0, self.DOC, [11, 5]), 1.0)
        self.assertEqual(pattern_level("near", [20, 5, 8], 0, self.DOC, [11, 5]), 0.0)

    def test_a_sweep_crosses_the_house_left_to_right(self):
        early = pattern_level("sweep_x", [2, 5, 8], 0.4, self.DOC, None)
        late = pattern_level("sweep_x", [2, 5, 8], 3.0, self.DOC, None)
        self.assertGreater(early, 0.5)
        self.assertLess(late, 0.01)

    def test_the_locator_blinks_one_segment_and_keeps_the_rest_dim(self):
        on = blink_frame(0.1, 2, 1, 4)
        self.assertEqual(on[6:9], [255, 255, 255])
        self.assertEqual(on[0:3], [12, 12, 12])
        self.assertEqual(blink_frame(0.6, 2, 1, 4)[6:9], [12, 12, 12])   # and off again


if __name__ == "__main__":
    unittest.main()
