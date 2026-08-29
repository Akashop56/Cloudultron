"""Tests for the device verb layer, against the fake adb responder."""

from __future__ import annotations

import unittest

from tests.support import fake_device

from cloudultron.errors import DeviceError, HierarchyUnavailable
from cloudultron.testing.fake import FakeTransport


class HierarchyTests(unittest.TestCase):
    def setUp(self):
        self.device, self.transport = fake_device()

    def test_dump_is_written_then_read_back(self):
        hierarchy = self.device.hierarchy(force=True)
        self.assertEqual(hierarchy.method, "dump+read")
        self.assertIn("uiautomator dump /sdcard/window_dump.xml", self.transport.calls)
        self.assertIn("cat /sdcard/window_dump.xml", self.transport.calls)
        self.assertGreater(hierarchy.screen.node_count, 0)

    def test_cache_collapses_rapid_rereads(self):
        # The loop observes right after acting; without this it would pay for two
        # dumps per step over a WiFi link.
        before = len([c for c in self.transport.calls if c.startswith("uiautomator")])
        self.device.hierarchy(force=True)
        self.device.hierarchy(force=False)
        self.device.hierarchy(force=False)
        after = len([c for c in self.transport.calls if c.startswith("uiautomator")])
        self.assertEqual(after - before, 1)

    def test_invalidate_forces_a_fresh_dump(self):
        self.device.hierarchy(force=True)
        self.device.invalidate()
        before = len([c for c in self.transport.calls if c.startswith("uiautomator")])
        self.device.hierarchy(force=False)
        self.assertEqual(len([c for c in self.transport.calls if c.startswith("uiautomator")]) - before, 1)

    def test_falls_back_to_dev_tty_when_the_file_is_unreadable(self):
        class NoCat(FakeTransport):
            def _on_cat(self, argv):
                return self._Result(argv, 1, "", "cat: /sdcard/window_dump.xml: Permission denied")

        device = _device_with(NoCat())
        hierarchy = device.hierarchy(force=True)
        self.assertEqual(hierarchy.method, "dump /dev/tty")
        self.assertGreater(hierarchy.screen.node_count, 0)

    def test_dump_failure_raises_unreadable_with_context(self):
        self.transport.dump_fails = True
        with self.assertRaises(HierarchyUnavailable) as ctx:
            self.device.hierarchy(force=True)
        self.assertIn("could not get idle state", str(ctx.exception))

    def test_dumped_xml_parses_cleanly(self):
        hierarchy = self.device.hierarchy(force=True)
        self.assertFalse(hierarchy.report.noteworthy, "the fake emits well-formed XML; repair should be unnecessary")


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.device, self.transport = fake_device()

    def test_current_focus_names_package_and_activity(self):
        self.assertEqual(self.device.current_focus(), "com.android.launcher/com.android.launcher.LauncherActivity")

    def test_focus_tracks_navigation(self):
        self.transport.tap_by_label("ExampleApp")
        self.assertEqual(self.device.current_focus(), "com.example.app/com.example.app.MainActivity")

    def test_screen_size_from_wm(self):
        self.assertEqual(self.device.screen_size(), (1080, 1920))

    def test_wait_for_device_uses_boot_completed(self):
        self.assertTrue(self.device.wait_for_device(timeout=1.0))


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.device, self.transport = fake_device()

    def test_tap_lands_on_the_fake_and_changes_screen(self):
        self.assertEqual(self.transport.screen_name(), "launcher")
        self.device.tap(520, 430)  # ExampleApp icon centre
        self.assertEqual(self.transport.screen_name(), "home")
        self.assertIn(("tap", "520", "430"), self.transport.events)

    def test_tap_on_dead_control_changes_nothing(self):
        # The behaviour the anti-loop detector exists to notice.
        self.device.start_activity("com.example.app/.MainActivity")
        before = self.transport.screen_name()
        self.device.tap(540, 1285)  # "Buy now": clickable, leads nowhere
        self.assertEqual(self.transport.screen_name(), before)

    def test_text_spaces_become_percent_s(self):
        self.device.start_activity("com.example.app/.DetailActivity")
        self.device.input_text("Ada Lovelace")
        # `input text` wants a literal %s for a space -- %20 is URL encoding and
        # would be typed verbatim by adb shell input.
        self.assertIn(("text", "Ada%sLovelace"), self.transport.events)
        element = next(e for e in _walk(self.transport.screens["detail"].elements) if e.focused)
        self.assertEqual(element.text, "Ada Lovelace")

    def test_non_ascii_is_refused_rather_than_mangled(self):
        with self.assertRaises(DeviceError):
            self.device.input_text("café")

    def test_keyevent_back_pops_navigation_history(self):
        self.device.tap(520, 430)
        self.assertEqual(self.transport.screen_name(), "home")
        self.device.press_back()
        self.assertEqual(self.transport.screen_name(), "launcher")

    def test_activity_component_dot_is_expanded(self):
        self.device.start_activity("com.example.app/.MainActivity")
        self.assertEqual(self.transport.screen_name(), "home")
        # `calls` entries are already joined strings; re-joining them interleaves
        # spaces between characters.
        self.assertIn("am start -n com.example.app/com.example.app.MainActivity", self.transport.calls[-1])

    def test_start_activity_rejects_a_malformed_component(self):
        with self.assertRaises(DeviceError):
            self.device.start_activity("MainActivity")

    def test_unknown_activity_reports_the_device_error(self):
        with self.assertRaises(DeviceError):
            self.device.start_activity("com.example.app/.Nope")

    def test_swipe_reveals_content_on_the_fake(self):
        self.device.swipe(540, 1200, 540, 400, 400)
        self.assertIn(("swipe", "540", "1200", "540", "400", "400"), self.transport.events)
        self.assertTrue(any("Camera" in (e.text or "") for e in _walk(self.transport.screens["launcher"].elements)))

    def test_shell_token_list_is_reachable_for_reads(self):
        result = self.device.shell_tokens(["dumpsys", "window"])
        self.assertIn("mCurrentFocus", result.text)


def _device_with(transport):
    from cloudultron.adb.device import AndroidDevice

    return AndroidDevice(transport)


def _walk(elements):
    for element in elements:
        yield element
        yield from _walk(element.children)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
