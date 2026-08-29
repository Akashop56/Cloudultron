"""Tests for the two-hash state model, the diff, and the anti-loop detector.

These are the tests that matter most in the project: everything else is
plumbing, and if the change classification is wrong the loop either never stops
or stops constantly.
"""

from __future__ import annotations

import unittest

from tests.support import (
    SAMPLE_DUMP,
    SAMPLE_DUMP_MOVED,
    SAMPLE_DUMP_RETEXTED,
    SAMPLE_DUMP_WITH_BANNER,
)

from cloudultron.testing.fake import Element, FakeScreen
from cloudultron.ui.hashing import (
    LoopDetector,
    compare,
    content_hash,
    element_keys,
    structure_hash,
)
from cloudultron.ui.parser import parse_hierarchy


def _screen(raw: str):
    return parse_hierarchy(raw)[0]


def named_screen(name: str) -> str:
    """A dump whose *geometry* is unique per letter.

    Necessary for detector tests: a screen that differs only in its label has an
    identical structure hash, which would make "two alternating screens" look
    like stagnation and quietly test the wrong code path.
    """
    count = max(1, ord(name) - 96)
    elements = [
        Element(
            cls="android.widget.Button",
            resource_id=f"com.foo:id/b{i}",
            text=f"{name}{i}",
            bounds=(10, 10 + 50 * i, 190, 50 + 50 * i),
            clickable=True,
        )
        for i in range(count)
    ]
    return FakeScreen(name=name, width=200, height=600, elements=elements).to_dump()


class HashSeparationTests(unittest.TestCase):
    def test_text_change_is_invisible_to_structure_hash(self):
        before, after = _screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_RETEXTED)
        self.assertEqual(structure_hash(before), structure_hash(after))
        self.assertNotEqual(content_hash(before), content_hash(after))

    def test_geometry_change_is_visible_to_both(self):
        before, after = _screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_MOVED)
        self.assertNotEqual(structure_hash(before), structure_hash(after))
        self.assertNotEqual(content_hash(before), content_hash(after))

    def test_identical_dump_yields_identical_hashes(self):
        a, b = _screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP)
        self.assertEqual(structure_hash(a), structure_hash(b))
        self.assertEqual(content_hash(a), content_hash(b))

    def test_hashes_are_short_hex(self):
        digest = structure_hash(_screen(SAMPLE_DUMP))
        self.assertEqual(len(digest), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest))

    def test_element_keys_prefer_resource_id_over_position(self):
        before, moved = _screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_MOVED)
        self.assertIn("id:com.foo:id/submit", element_keys(before))
        # Same identity even though its bounds changed: this is what makes a
        # "which element is new" diff survive a layout shift.
        self.assertIn("id:com.foo:id/submit", element_keys(moved))


class DiffLevelTests(unittest.TestCase):
    def test_none_when_identical(self):
        diff = compare(_screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP))
        self.assertEqual(diff.level, "none")
        self.assertFalse(diff.changed)
        self.assertTrue(diff.is_static)
        self.assertEqual(diff.similarity, 1.0)

    def test_volatile_for_text_only_change(self):
        diff = compare(_screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_RETEXTED))
        self.assertEqual(diff.level, "volatile")
        # Content moved, but not enough to justify re-deciding a tap target.
        self.assertFalse(diff.changed)

    def test_structural_for_geometry_change(self):
        diff = compare(_screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_MOVED))
        self.assertEqual(diff.level, "structural")
        self.assertTrue(diff.changed)
        self.assertEqual(diff.added, ())
        self.assertEqual(diff.removed, ())

    def test_inserted_node_is_structural(self):
        # A banner being added puts a new box in the wireframe, so it is a
        # structural change even though no existing element moved.
        diff = compare(_screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_WITH_BANNER))
        self.assertEqual(diff.level, "structural")
        self.assertTrue(diff.changed)
        self.assertIn("id:com.foo:id/banner", diff.added)
        self.assertEqual(diff.removed, ())
        self.assertEqual(diff.node_delta, 1)

    def test_content_level_for_identity_change_in_place(self):
        # No resource id, so identity is label-derived: renaming changes the
        # element set without changing any box on screen.
        def dump(label: str) -> str:
            return FakeScreen(
                name="s",
                width=200,
                height=200,
                elements=[
                    Element(
                        cls="android.widget.Button",
                        text=label,
                        bounds=(10, 10, 190, 60),
                        clickable=True,
                    )
                ],
            ).to_dump()

        diff = compare(_screen(dump("Go")), _screen(dump("Stop")))
        self.assertEqual(diff.level, "content")
        self.assertEqual(diff.added, ("label:Button:Stop",))
        self.assertEqual(diff.removed, ("label:Button:Go",))
        self.assertFalse(diff.structure_changed)

    def test_initial_state_reports_a_change(self):
        diff = compare(None, _screen(SAMPLE_DUMP))
        self.assertEqual(diff.level, "initial")
        self.assertTrue(diff.changed)

    def test_summary_is_readable(self):
        summary = compare(_screen(SAMPLE_DUMP), _screen(SAMPLE_DUMP_WITH_BANNER)).summary()
        self.assertIn("+1", summary)
        self.assertIn("sim=", summary)

    def test_toggle_of_a_switch_is_volatile_not_structural(self):
        # A checkbox toggling is the canonical case for "content moved, layout
        # did not": the loop must not treat it as a screen transition.
        screen = FakeScreen(
            name="s",
            width=200,
            height=200,
            elements=[
                Element(
                    cls="android.widget.Switch",
                    resource_id="com.foo:id/sw",
                    bounds=(10, 10, 190, 60),
                    clickable=True,
                    checkable=True,
                    checked=False,
                )
            ],
        )
        before = parse_hierarchy(screen.to_dump())[0]
        screen.elements[0].checked = True
        after = parse_hierarchy(screen.to_dump())[0]
        diff = compare(before, after)
        self.assertEqual(diff.level, "volatile")
        self.assertNotEqual(content_hash(before), content_hash(after))
        self.assertEqual(structure_hash(before), structure_hash(after))


class LoopDetectorTests(unittest.TestCase):
    def _feed(self, detector: LoopDetector, names: list[str]) -> list:
        verdicts = []
        for name in names:
            detector.observe(_screen(named_screen(name)))
            verdicts.append(detector.check())
        return verdicts

    def test_stagnation_fires_after_the_limit(self):
        detector = LoopDetector(stagnation_limit=3, max_period=2, livelock_limit=99)
        verdicts = self._feed(detector, ["a"] * 5)
        self.assertFalse(verdicts[0].stuck)
        self.assertFalse(verdicts[1].stuck)
        self.assertTrue(verdicts[2].stuck)
        self.assertEqual(verdicts[2].kind, "stagnation")

    def test_alternating_screens_are_not_stagnation_but_are_oscillation(self):
        # The hard case: every individual step *is* a change, so a
        # "did anything change?" check reports healthy forever.
        detector = LoopDetector(stagnation_limit=4, max_period=3, livelock_limit=99)
        verdicts = self._feed(detector, ["a", "b"] * 4)
        self.assertTrue(any(v.stuck and v.kind == "oscillation" for v in verdicts))
        self.assertTrue(all(v.kind != "stagnation" for v in verdicts))

    def test_period_two_needs_four_samples_not_two(self):
        detector = LoopDetector(stagnation_limit=9, max_period=3, livelock_limit=99)
        verdicts = self._feed(detector, ["a", "b"])
        self.assertTrue(all(not v.stuck for v in verdicts))

    def test_environment_trips_can_be_suppressed(self):
        # Dry-run: nothing was dispatched, so an unchanged screen is expected.
        detector = LoopDetector(stagnation_limit=2, max_period=2, livelock_limit=99)
        self._feed(detector, ["a", "a"])
        self.assertTrue(detector.check().stuck)
        detector2 = LoopDetector(stagnation_limit=2, max_period=2, livelock_limit=99)
        self._feed(detector2, ["a", "a"])
        self.assertFalse(detector2.check(environment_trips=False).stuck)

    def test_livelock_is_independent_of_environment_trips(self):
        detector = LoopDetector(stagnation_limit=99, max_period=2, livelock_limit=3)
        for name in ("a", "b", "c", "d"):  # every screen distinct, same action each time
            screen = _screen(named_screen(name))
            detector.observe(screen)
            detector.record_action(screen, "tap_index index=0")
        self.assertFalse(detector.check(environment_trips=False).stuck)

        # Now the same action on the same screen repeatedly. Distinct screens
        # earlier must not mask it, and suppressing environment trips must not
        # either: livelock is a property of the policy, not of the device.
        for _ in range(3):
            screen = _screen(named_screen("a"))
            detector.observe(screen)
            detector.record_action(screen, "tap_index index=0")
        verdict = detector.check(environment_trips=False)
        self.assertTrue(verdict.stuck)
        self.assertEqual(verdict.kind, "livelock")

    def test_loading_is_reported_but_is_not_stuck(self):
        # Static wireframe with a ticking label: give it time, do not abort.
        screen = FakeScreen(
            name="load",
            width=100,
            height=100,
            clock_sequence=["0%", "50%", "100%"],
            elements=[Element(cls="android.widget.ProgressBar", resource_id="com.foo:id/pb", bounds=(10, 10, 90, 20))],
        )
        detector = LoopDetector(stagnation_limit=3, max_period=2, livelock_limit=99)
        kinds = []
        for _ in range(4):
            screen.advance_clock()
            detector.observe(parse_hierarchy(screen.to_dump())[0])
            kinds.append(detector.check().kind)
        self.assertIn("loading", kinds)
        self.assertTrue(all(kind != "stagnation" for kind in kinds))

    def test_find_cycle_period_one_is_left_to_stagnation(self):
        self.assertIsNone(LoopDetector.find_cycle(["x", "x"], max_period=3, min_period=2))
        self.assertEqual(LoopDetector.find_cycle(["x", "y", "x", "y"], max_period=3), 2)
        self.assertEqual(LoopDetector.find_cycle(["x", "y", "z", "x", "y", "z"], max_period=4), 3)
        self.assertIsNone(LoopDetector.find_cycle(["a", "b", "c", "d"], max_period=3))

    def test_reset_clears_history(self):
        detector = LoopDetector(stagnation_limit=2, max_period=2, livelock_limit=2)
        self._feed(detector, ["a", "a"])
        detector.reset()
        self.assertEqual(len(detector), 0)
        self.assertFalse(detector.check().stuck)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
