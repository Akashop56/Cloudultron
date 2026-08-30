"""Tests for the action vocabulary, the script grammar, and the dispatcher."""

from __future__ import annotations

import unittest

from tests.support import fake_device

from cloudultron.errors import PolicyError
from cloudultron.loop.actions import Action, Dispatcher, Op
from cloudultron.loop.policy import ExplorePolicy, NullPolicy, ScriptedPolicy, parse_line
from cloudultron.safety import Effect
from cloudultron.ui.hashing import structure_hash
from cloudultron.ui.parser import parse_hierarchy
from cloudultron.ui.render import index_screen


class ActionTests(unittest.TestCase):
    def test_only_mutations_are_mutations(self):
        self.assertFalse(Action.noop().is_mutation)
        self.assertFalse(Action.wait(1).is_mutation)
        self.assertTrue(Action.tap(0).is_mutation)
        self.assertTrue(Action.back().is_mutation)
        self.assertTrue(Action.text("hi").is_mutation)

    def test_effects_match_the_guard_tiers(self):
        self.assertEqual(Action.noop().effect, Effect.READ)
        self.assertEqual(Action.tap(0).effect, Effect.WRITE)

    def test_key_ignores_rationale_and_source(self):
        # This is what makes loop detection work on an LLM policy that rewords
        # itself every step.
        a = Action.tap(3, rationale="first attempt")
        b = Action.tap(3, rationale="surely this time works")
        self.assertEqual(a.key(), b.key())
        self.assertNotEqual(a.key(), Action.tap(4).key())

    def test_key_includes_arguments(self):
        self.assertNotEqual(Action.text("a").key(), Action.text("b").key())
        self.assertNotEqual(Action.keyevent(4).key(), Action.keyevent(3).key())

    def test_terminals(self):
        self.assertTrue(Action.done().is_terminal)
        self.assertTrue(Action.abort("x").is_terminal)
        self.assertFalse(Action.back().is_terminal)

    def test_describe_is_readable(self):
        self.assertEqual(Action.back().describe(), "back")
        self.assertIn("tap_index", Action.tap(7).describe())

    def test_scroll_direction_is_validated(self):
        with self.assertRaises(PolicyError):
            Action.scroll("sideways")

    def test_to_dict_round_trips_the_op_name(self):
        payload = Action.tap(2, "reason").to_dict()
        self.assertEqual(payload["op"], "tap_index")
        self.assertEqual(payload["args"], {"index": 2})


class ScriptGrammarTests(unittest.TestCase):
    CASES = {
        "tap 3": (Op.TAP_INDEX, {"index": 3}),
        "point 300 400": (Op.TAP_POINT, {"x": 300, "y": 400}),
        "back": (Op.BACK, {}),
        "home": (Op.HOME, {}),
        "scroll down": (Op.SCROLL, {"direction": "down"}),
        "text hello world": (Op.TEXT, {"value": "hello world"}),
        "keyevent 66": (Op.KEYEVENT, {"code": 66}),
        "keyevent KEYCODE_ENTER": (Op.KEYEVENT, {"code": "KEYCODE_ENTER"}),
        "wait 2": (Op.WAIT, {"seconds": 2.0}),
        "launch com.android.settings": (Op.LAUNCH_APP, {"package": "com.android.settings"}),
        "start com.foo/.Main": (Op.START_ACTIVITY, {"component": "com.foo/.Main"}),
        "swipe 10 20 30 40": (Op.SWIPE, {"x1": 10, "y1": 20, "x2": 30, "y2": 40}),
        "done": (Op.DONE, {}),
        "shell ls": (Op.RAW_SHELL, {"command": "ls"}),
    }

    def test_each_verb_parses(self):
        for line, (op, args) in self.CASES.items():
            with self.subTest(line=line):
                action = parse_line(line)
                self.assertEqual(action.op, op)
                for key, value in args.items():
                    self.assertEqual(action.args[key], value)

    def test_numbers_and_ids_survive_text_parsing(self):
        self.assertEqual(parse_line("text C:\\Users\\x; rm -rf /").args["value"], "C:\\Users\\x; rm -rf /")

    def test_comments_and_blank_lines_are_skipped(self):
        policy = ScriptedPolicy.from_text("# a comment\n\ntap 0\n\n# another\nback\n")
        self.assertEqual([a.op for a in policy.actions], [Op.TAP_INDEX, Op.BACK])

    def test_unknown_verb_is_a_policy_error(self):
        with self.assertRaises(PolicyError):
            parse_line("teleport to moon")

    def test_missing_argument_is_a_policy_error_not_a_crash(self):
        with self.assertRaises(PolicyError):
            parse_line("tap")
        with self.assertRaises(PolicyError):
            parse_line("point 10")

    def test_json_forms(self):
        policy = ScriptedPolicy.from_json({"actions": [{"op": "tap_index", "args": {"index": 1}}, "back"]})
        self.assertEqual(len(policy.actions), 2)
        self.assertEqual(policy.actions[0].op, Op.TAP_INDEX)
        bare = ScriptedPolicy.from_json([{"op": "back"}])
        self.assertEqual(bare.actions[0].op, Op.BACK)

    def test_bad_json_shape_is_reported(self):
        with self.assertRaises(PolicyError):
            ScriptedPolicy.from_json({"nope": 1})
        with self.assertRaises(PolicyError):
            ScriptedPolicy.from_json([{"op": "launch_rocket"}])

    def test_script_advances_and_then_reports_done(self):
        policy = ScriptedPolicy.from_text("tap 0\ntap 1")
        obs = _obs()
        self.assertEqual(policy.decide(obs).op, Op.TAP_INDEX)
        self.assertEqual(policy.decide(obs).op, Op.TAP_INDEX)
        self.assertEqual(policy.decide(obs).op, Op.DONE, "an exhausted script must stop the run")
        self.assertEqual(policy.decide(obs).op, Op.DONE)


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.device, self.transport = fake_device()
        self.hierarchy = self.device.hierarchy(force=True)
        self.indexed = index_screen(self.hierarchy.screen)
        self.dispatcher = Dispatcher(
            self.device, screen=self.hierarchy.screen, indexed=self.indexed, size=self.hierarchy.screen.window_size
        )

    def test_indices_resolve_to_element_centres(self):
        item = self.indexed[0]
        self.assertEqual(self.dispatcher.resolve_index(0), item.center)

    def test_labels_are_reported_for_the_trace(self):
        self.assertEqual(self.dispatcher.label_for(Action.tap(1)), "icon")

    def test_unknown_index_raises_before_dispatch(self):
        with self.assertRaises(PolicyError):
            self.dispatcher.resolve_index(99)

    def test_dispatch_tap_navigates(self):
        before = structure_hash(self.hierarchy.screen)
        description = self.dispatcher.dispatch(Action.tap(1))  # ExampleApp icon
        self.assertIn("tap @", description)
        self.assertNotEqual(structure_hash(self.device.hierarchy(force=True).screen), before)

    def test_raw_point_outside_the_viewport_is_rejected(self):
        with self.assertRaises(PolicyError):
            self.dispatcher.dispatch(Action.tap_point(99999, 5))

    def test_scroll_without_a_known_size_is_rejected(self):
        bare = Dispatcher(self.device, screen=self.hierarchy.screen, indexed=[], size=(0, 0))
        with self.assertRaises(PolicyError):
            bare.dispatch(Action.scroll("down"))

    def test_empty_raw_shell_is_rejected(self):
        # Refusing here is about a malformed request, not permission: the guard
        # answers permission. An empty string would otherwise "succeed" having
        # sent nothing.
        with self.assertRaises(PolicyError):
            self.dispatcher.dispatch(Action.raw_shell("   "))

    def test_allowed_raw_shell_runs_verbatim(self):
        # Once the guard has let a composed command through, it must not be
        # re-tokenised -- that is the whole reason a raw string exists.
        from cloudultron.testing.fake import FakeTransport
        from cloudultron.adb.device import AndroidDevice

        transport = FakeTransport()
        device = AndroidDevice(transport)
        dispatcher = Dispatcher(device, screen=self.hierarchy.screen, indexed=[], size=(1080, 1920))
        dispatcher.dispatch(Action.raw_shell("input keyevent 3"))
        self.assertEqual(transport.screen_name(), "launcher")
        self.assertIn(("keyevent", "3"), transport.events)


class DangerFilterTests(unittest.TestCase):
    """The explorer's label filter is a suite-level allowance, not a lock.

    Each case uses a screen whose only target is the dangerous one, so the
    assertion is about permission and not about which button sorts first.
    """

    @staticmethod
    def _buttons(*labels: str) -> "object":
        from cloudultron.testing.fake import Element, FakeScreen

        elements = [
            Element(
                cls="android.widget.Button",
                text=label,
                bounds=(60, 300 + 200 * i, 1000, 430 + 200 * i),
                clickable=True,
                taps_to="next",
            )
            for i, label in enumerate(labels)
        ]
        return parse_hierarchy(FakeScreen(name="shop", width=1080, height=1920, elements=elements).to_dump())[0]

    def test_a_solitary_dangerous_label_is_not_clicked(self):
        policy = ExplorePolicy()
        self.assertTrue(policy.decide(_obs(self._buttons("Buy now"), 10)).is_terminal)

    def test_an_authorised_label_becomes_clickable(self):
        policy = ExplorePolicy(allow_labels=("Buy now",))
        action = policy.decide(_obs(self._buttons("Buy now"), 10))
        self.assertEqual(action.op, Op.TAP_INDEX)
        self.assertIn("Buy now", action.rationale)

    def test_authorisation_is_per_label_not_a_blank_cheque(self):
        # Releasing "Buy now" must not also release an unrelated "Delete
        # account" sitting on the same screen.
        screen = self._buttons("Buy now", "Delete account")
        policy = ExplorePolicy(allow_labels=("Buy now",))
        first = policy.decide(_obs(screen, 10))
        self.assertEqual(first.args.get("index"), 0)
        second = policy.decide(_obs(screen, 9, diff_level="none"))
        self.assertNotEqual(second.args.get("index"), 1, "Delete account is still out of scope")

    def test_the_filter_can_be_turned_off_for_a_suite(self):
        policy = ExplorePolicy(filter_danger=False)
        self.assertEqual(policy.decide(_obs(self._buttons("Buy now"), 10)).op, Op.TAP_INDEX)
        self.assertFalse(policy._looks_dangerous("Delete account"))

    def test_app_specific_words_extend_the_default_set(self):
        # Extends rather than replaces: passing a custom word must not drop the
        # built-in ones along with it.
        policy = ExplorePolicy(extra_danger_labels=("transfer",))
        self.assertTrue(policy.decide(_obs(self._buttons("Transfer money"), 10)).is_terminal)
        self.assertTrue(policy.decide(_obs(self._buttons("Delete account"), 10)).is_terminal)
        self.assertFalse(policy._looks_dangerous("Browse catalogue"))

    def test_danger_and_dismiss_are_matched_differently_on_purpose(self):
        # Deliberate asymmetry: matching is loose for danger, so a button called
        # "Delete later" is blocked by the word "delete" even though it sounds
        # harmless, because the cost of a wrong guess is the device's data.
        # Dismissal uses an exact set so "Continue shopping" is never mistaken
        # for an "OK" button and auto-pressed.
        policy = ExplorePolicy()
        self.assertTrue(policy._looks_dangerous("Permanently delete account"))
        self.assertTrue(policy._looks_dangerous("Delete later"), "substring matching is on purpose")
        self.assertNotIn("continue shopping", policy.DISMISS_LABELS)
        self.assertIn("continue", policy.DISMISS_LABELS)
        # ...and a danger word from the operator's own list is honoured too.
        self.assertTrue(ExplorePolicy(extra_danger_labels=("Sweep",))._looks_dangerous("sweep the floor"))


class NullPolicyTests(unittest.TestCase):
    def test_never_acts_and_never_self_terminates(self):
        policy = NullPolicy()
        for _ in range(3):
            action = policy.decide(_obs(remaining=1))
            self.assertEqual(action.op, Op.NOOP)
            self.assertFalse(action.is_terminal)


class ExplorePolicyTests(unittest.TestCase):
    def test_it_taps_something_on_a_screen_with_targets(self):
        device, _ = fake_device()
        screen = device.hierarchy(force=True).screen
        policy = ExplorePolicy()
        action = policy.decide(_obs(screen, 10))
        self.assertEqual(action.op, Op.TAP_INDEX)

    def test_it_does_not_repeat_a_target_it_already_tried(self):
        device, _ = fake_device()
        screen = device.hierarchy(force=True).screen
        policy = ExplorePolicy()
        first = policy.decide(_obs(screen, 10))
        second = policy.decide(_obs(screen, 9))
        self.assertNotEqual(first.args["index"], second.args["index"])

    def test_it_refuses_to_press_a_destructive_looking_target(self):
        from cloudultron.testing.fake import Element, FakeScreen

        screen = parse_hierarchy(
            FakeScreen(
                name="danger",
                width=200,
                height=200,
                elements=[
                    Element(cls="android.widget.Button", text="Delete account", bounds=(10, 10, 190, 60), clickable=True, taps_to="other")
                ],
            ).to_dump()
        )[0]
        policy = ExplorePolicy()
        action = policy.decide(_obs(screen, 10))
        self.assertNotEqual(action.op, Op.TAP_INDEX, "a lone 'Delete account' button must not be explored")
        self.assertTrue(action.is_terminal)

    def test_it_stops_when_the_screen_has_nothing_left(self):
        from cloudultron.testing.fake import FakeScreen

        screen = parse_hierarchy(FakeScreen(name="empty", width=100, height=100, elements=[]).to_dump())[0]
        policy = ExplorePolicy()
        self.assertTrue(policy.decide(_obs(screen, 5)).is_terminal)

    def test_it_does_not_scroll_forever_when_scrolling_changed_nothing(self):
        from cloudultron.loop.policy import HistoryEntry
        from cloudultron.testing.fake import Element, FakeScreen

        screen = parse_hierarchy(
            FakeScreen(
                name="list",
                width=200,
                height=200,
                elements=[Element(cls="android.widget.ScrollView", resource_id="x", bounds=(0, 0, 200, 200), scrollable=True)],
            ).to_dump()
        )[0]
        policy = ExplorePolicy()

        first = policy.decide(_obs(screen, 5))
        self.assertEqual(first.op, Op.TAP_INDEX)

        # Nothing untried remains, so it tries the one remaining way to make progress.
        second = policy.decide(_obs(screen, 4, diff_level="none"))
        self.assertEqual(second.op, Op.SCROLL)

        # A scroll that changed nothing is not a scroll to repeat, and the policy
        # stops rather than pushing the executor's livelock trip for it.
        history = (HistoryEntry(step=1, action="scroll direction=down", outcome="swipe", structure_hash="x"),)
        third = policy.decide(_obs(screen, 3, history=history, diff_level="none"))
        self.assertNotEqual(third.op, Op.SCROLL)
        # Backing out or finishing are both correct here -- the container tap that
        # preceded this counted as a navigation for the depth heuristic, so it
        # tries to retreat before declaring the screen exhausted.
        self.assertIn(third.op, (Op.BACK, Op.DONE))

    def test_siblings_that_share_a_resource_id_are_tried_separately(self):
        # Every row in a RecyclerView carries the same resource id. If "already
        # visited" were keyed on the id, exploring row 0 would mark the whole list
        # done and a 50-item feed would yield exactly one tap.
        from cloudultron.testing.fake import Element, FakeScreen

        rows = [
            Element(
                cls="android.widget.TextView",
                resource_id="com.foo:id/row",
                text=f"Item {i}",
                bounds=(0, 100 + i * 90, 200, 180 + i * 90),
                clickable=True,
            )
            for i in range(4)
        ]
        screen = parse_hierarchy(FakeScreen(name="feed", width=200, height=600, elements=rows).to_dump())[0]
        policy = ExplorePolicy()
        picked = [policy.decide(_obs(screen, 10 - i, diff_level="none")).args["index"] for i in range(4)]
        self.assertEqual(len(set(picked)), 4, f"expected four distinct taps, got {picked}")
        self.assertTrue(policy.decide(_obs(screen, 6, diff_level="none")).is_terminal)


def _obs(screen=None, remaining=10, history=(), diff_level="initial"):
    from cloudultron.loop.policy import Observation

    if screen is None:
        device, _ = fake_device()
        screen = device.hierarchy(force=True).screen
    return Observation(
        step=0,
        screen=screen,
        digest="",
        diff_summary="x",
        diff_level=diff_level,
        focused_window="",
        history=tuple(history),
        steps_remaining=remaining,
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
