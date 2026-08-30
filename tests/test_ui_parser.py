"""Tests for the hierarchy model and parser."""

from __future__ import annotations

import unittest

from tests.support import SAMPLE_DUMP, SAMPLE_DUMP_RETEXTED

from cloudultron.errors import HierarchyUnavailable
from cloudultron.ui.model import Rect
from cloudultron.ui.parser import parse_hierarchy, sanitise


class RectTests(unittest.TestCase):
    def test_parses_bounds_string(self):
        self.assertEqual(Rect.parse("[10,20][30,40]"), Rect(10, 20, 30, 40))

    def test_garbage_becomes_empty_rect(self):
        for value in (None, "", "bounds", "[1,2]"):
            with self.subTest(value=value):
                self.assertTrue(Rect.parse(value).is_empty)

    def test_inverted_bounds_are_normalised(self):
        rect = Rect.parse("[100,100][10,10]")
        self.assertEqual((rect.x1, rect.y1, rect.x2, rect.y2), (10, 10, 100, 100))
        self.assertEqual(rect.center, (55, 55))

    def test_contains_and_overlaps(self):
        outer, inner = Rect(0, 0, 100, 100), Rect(20, 20, 40, 40)
        self.assertTrue(outer.contains(inner))
        self.assertTrue(outer.overlaps(inner))
        self.assertFalse(inner.contains(outer))
        self.assertFalse(outer.overlaps(Rect(200, 200, 300, 300)))


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.screen, self.report = parse_hierarchy(SAMPLE_DUMP)

    def test_tree_shape_and_depth(self):
        self.assertEqual(self.screen.node_count, 7)
        root = self.screen.root
        self.assertEqual(len(root.children), 1)
        frame = root.children[0]
        self.assertEqual(frame.short_class, "FrameLayout")
        self.assertEqual(frame.depth, 1)
        self.assertEqual(len(frame.children), 6)
        self.assertEqual(frame.children[0].depth, 2)
        self.assertIs(frame.children[0].parent, frame)

    def test_flags_are_booleans_not_strings(self):
        nodes = {n.resource_id: n for n in self.screen.walk()}
        self.assertTrue(nodes["com.foo:id/submit"].clickable)
        self.assertTrue(nodes["com.foo:id/pass"].password)
        # Verify flags are booleans, not strings or other types
        self.assertIsInstance(nodes["com.foo:id/pass"].clickable, bool)
        self.assertIsInstance(nodes["com.foo:id/pass"].password, bool)
        self.assertTrue(nodes["com.foo:id/user"].focused)
        # A disabled node is present but not interactable.
        self.assertFalse(nodes["com.foo:id/dead"].enabled)

    def test_window_size_from_header(self):
        self.assertEqual(self.screen.window_size, (1080, 1920))
        self.assertEqual(self.screen.package, "com.foo")

    def test_label_falls_back_through_desc_and_id(self):
        nodes = {n.resource_id: n for n in self.screen.walk()}
        self.assertEqual(nodes["com.foo:id/title"].label, "Sign in")
        # No text, so content-desc wins:
        self.assertEqual(nodes["com.foo:id/user"].label, "Username")
        # No text or desc, so the resource id basename wins:
        self.assertEqual(nodes["com.foo:id/dead"].label, "dead")

    def test_interactables_exclude_disabled_and_prune_containers(self):
        labels = [n.label for n in self.screen.interactables()]
        self.assertEqual(labels, ["Username", "Password", "Log in", "Forgot password?"])
        self.assertNotIn("dead", labels)

    def test_interactable_pruning_drops_clickable_wrapper(self):
        from cloudultron.testing.fake import Element, FakeScreen

        inner = Element(cls="android.widget.TextView", text="Go", bounds=(20, 20, 80, 60), clickable=True)
        wrapper = Element(cls="android.widget.LinearLayout", bounds=(0, 0, 100, 100), clickable=True, children=[inner])
        screen = FakeScreen(name="x", width=100, height=100, elements=[wrapper]).to_dump()
        parsed, _ = parse_hierarchy(screen)
        self.assertEqual([n.label for n in parsed.interactables()], ["Go"])

    def test_paths_are_stable_and_distinguishable(self):
        paths = [n.path for n in self.screen.walk()]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(paths[0], "")
        self.assertEqual(paths[2], "0/0")


class HostileInputTests(unittest.TestCase):
    def test_trailing_garbage_after_closing_tag_is_ignored(self):
        raw = SAMPLE_DUMP + "\nUI hierchary dumped to: /dev/tty\n"
        screen, report = parse_hierarchy(raw)
        self.assertEqual(screen.node_count, 7)
        self.assertTrue(report.sliced_from_garbage)

    def test_leading_error_line_is_ignored(self):
        raw = "ERROR: getEvents failed\n" + SAMPLE_DUMP
        screen, report = parse_hierarchy(raw)
        self.assertEqual(screen.node_count, 7)
        self.assertTrue(report.sliced_from_garbage)

    def test_control_bytes_are_stripped_not_fatal(self):
        raw = SAMPLE_DUMP.replace('text="Sign in"', 'text="Sign\x01 in\x00"')
        screen, report = parse_hierarchy(raw)
        self.assertEqual(screen.node_count, 7)
        self.assertGreater(report.sanitised_chars, 0)

    def test_bare_ampersand_is_escaped(self):
        raw = SAMPLE_DUMP.replace('text="Sign in"', 'text="Tom & Jerry"')
        screen, _ = parse_hierarchy(raw)
        title = next(n for n in screen.walk() if n.resource_id == "com.foo:id/title")
        self.assertEqual(title.text, "Tom & Jerry")

    def test_truncated_dump_is_salvaged_as_prefix(self):
        raw = SAMPLE_DUMP.split('bounds="[60,700][1020,830]"')[0]
        screen, report = parse_hierarchy(raw)
        self.assertTrue(report.recovered_truncation)
        # We keep the head of the tree instead of losing the whole step.
        self.assertGreater(screen.node_count, 3)
        self.assertLess(screen.node_count, 7)

    def test_empty_and_non_xml_raise_unreadable(self):
        for raw in ("", "   ", "adb shell: command not found", "<hierarchy></hierarchy>"):
            with self.subTest(raw=raw):
                with self.assertRaises(HierarchyUnavailable):
                    parse_hierarchy(raw)

    def test_sanitise_handles_bytes_and_bom(self):
        text, _ = sanitise(("\ufeff" + SAMPLE_DUMP).encode("utf-8"))
        self.assertIn("<hierarchy", text)
        self.assertNotIn("\ufeff", text)

    def test_invalid_utf8_does_not_raise(self):
        text, report = sanitise(SAMPLE_DUMP.encode() + b"\xff\xfe")
        self.assertIn("</hierarchy>", text)
        self.assertIsInstance(report.sanitised_chars, int)

    def test_namespaced_attributes_are_accepted(self):
        raw = """<hierarchy rotation="0"><node android:class="android.widget.Button"
        android:text="Hi" android:clickable="true" android:long-clickable="true"
        android:bounds="[1,2][3,4]" /></hierarchy>"""
        screen, _ = parse_hierarchy(raw)
        node = list(screen.walk())[1]
        self.assertEqual(node.text, "Hi")
        self.assertTrue(node.clickable)
        self.assertTrue(node.long_clickable)


class RetextureTests(unittest.TestCase):
    def test_text_change_does_not_change_interactable_geometry(self):
        before, _ = parse_hierarchy(SAMPLE_DUMP)
        after, _ = parse_hierarchy(SAMPLE_DUMP_RETEXTED)
        self.assertEqual([n.bounds.as_tuple() for n in before.interactables()], [n.bounds.as_tuple() for n in after.interactables()])
        self.assertNotEqual([n.label for n in before.walk()], [n.label for n in after.walk()])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
