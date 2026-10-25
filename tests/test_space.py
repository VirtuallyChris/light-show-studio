"""The house map (space.py): where each segment of a mapped light is, and the test patterns played across it."""

import unittest

from lightshow.studio.space import blink_frame, clean, pattern_level, positions


class PositionsTest(unittest.TestCase):
    def test_a_strip_runs_between_its_pins_and_turns_its_corners(self):
        m = {"kind": "strip", "anchors": [{"seg": 0, "x": 0, "y": 0}, {"seg": 10, "x": 10, "y": 0}, {"seg": 20, "x": 10, "y": 10}]}
        pos = positions(m, 21)
        self.assertEqual(pos[0], [0, 0])
        self.assertEqual(pos[5], [5, 0])                            # along the first wall
        self.assertEqual(pos[10], [10, 0])                          # the corner
        self.assertEqual(pos[15], [10, 5])                          # along the second
        self.assertIsNone(positions({"kind": "strip", "anchors": m["anchors"][:1]}, 21))   # one pin: nowhere yet

    def test_a_pole_is_one_spot(self):
        self.assertEqual(positions({"kind": "pole", "x": 1, "y": 2}, 7), [[1, 2]] * 7)
        self.assertIsNone(positions({"kind": "pole", "x": None, "y": None}, 7))

    def test_each_tube_is_where_it_was_placed(self):
        tubes = [{"x": 5, "y": 5}] + [None] * 19
        pos = positions({"kind": "tubes", "tubes": tubes}, 240)
        self.assertEqual(pos[0], [5, 5])
        self.assertEqual(pos[11], [5, 5])                           # all its LEDs
        self.assertIsNone(pos[12])                                  # one placed: the others nowhere yet

    def test_the_tubes_string_like_a_strip_between_the_placed_ones(self):
        """The user placed tubes 1, 5, 6, 11 and 20 (the last off down the hall, past the plan's top wall)."""
        from lightshow.studio.space import fill_tubes
        placed = {1: (15.38, 0.63), 5: (16.41, 8.0), 6: (17.73, 9.08), 11: (25.78, 10.15), 20: (26.75, -9.39)}
        tubes = [{"x": placed[i][0], "y": placed[i][1]} if i in placed else None for i in range(1, 21)]
        t = fill_tubes(tubes)
        self.assertEqual((t[0]["x"], t[0]["y"]), (15.38, 0.63))     # the placed ones stay put
        self.assertNotIn("filled", t[4])
        self.assertAlmostEqual(t[2]["y"], 0.63 + (8.0 - 0.63) * 2 / 4, places=2)   # tube 3: halfway from 1 to 5
        self.assertAlmostEqual(t[15]["y"], 10.15 + (-9.39 - 10.15) * 5 / 9, places=2)   # tube 16: on to the hall
        self.assertTrue(t[15]["filled"])
        self.assertTrue(all(t))
        pos = positions({"kind": "tubes", "tubes": tubes}, 240)
        self.assertTrue(all(pos))                                   # every LED somewhere now

    def test_tubes_past_the_last_placed_carry_on_in_its_line(self):
        from lightshow.studio.space import fill_tubes
        t = fill_tubes([{"x": 0, "y": 0}, {"x": 1, "y": 0}] + [None] * 18)
        self.assertAlmostEqual(t[19]["x"], 19.0)


class CleanTest(unittest.TestCase):
    def test_a_map_keeps_its_plan_shapes_and_refuses_nonsense(self):
        doc = clean({"plan": {"width_ft": 41, "shapes": {"rooms": [{"name": "Kitchen", "poly": [[0, 0], [1, 0], [1, 1]], "label": [0.2, 0.2]}],
                                                        "walls": [[[0, 0], [1, 0]]], "fixtures": [{"name": "island", "rect": [0, 0, 1, 2]}]}},
                     "lights": {"AA": {"kind": "strip", "anchors": [{"seg": 5, "x": 1, "y": 1}, {"seg": 0, "x": 0, "y": 0}]}}})
        self.assertEqual(doc["plan"]["shapes"]["rooms"][0]["label"], [0.2, 0.2])
        self.assertEqual([a["seg"] for a in doc["lights"]["AA"]["anchors"]], [0, 5])   # pins in segment order
        with self.assertRaises(ValueError):
            clean({"lights": {"AA": {"kind": "laser"}}})


class PatternsTest(unittest.TestCase):
    DOC = {"plan": {"width_ft": 40, "depth_ft": 10}, "center": [20, 5]}

    def test_the_point_check_lights_only_what_is_near_the_spot(self):
        self.assertEqual(pattern_level("near", [10, 5], 0, self.DOC, [11, 5]), 1.0)
        self.assertEqual(pattern_level("near", [20, 5], 0, self.DOC, [11, 5]), 0.0)

    def test_a_sweep_crosses_the_house_left_to_right(self):
        early = pattern_level("sweep_x", [2, 5], 0.4, self.DOC, None)
        late = pattern_level("sweep_x", [2, 5], 3.0, self.DOC, None)
        self.assertGreater(early, 0.5)
        self.assertLess(late, 0.01)

    def test_the_locator_blinks_one_segment_and_keeps_the_rest_dim(self):
        on = blink_frame(0.1, 2, 1, 4)
        self.assertEqual(on[6:9], [255, 255, 255])
        self.assertEqual(on[0:3], [12, 12, 12])
        self.assertEqual(blink_frame(0.6, 2, 1, 4)[6:9], [12, 12, 12])   # and off again


if __name__ == "__main__":
    unittest.main()
