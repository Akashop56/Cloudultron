"""Tests for the executor: dry-run gating, tripwires, trace, and dispatch.

These run the *real* :class:`Executor` against the fake device, so they cover
the wiring between guard, detector, dispatcher and cache-invalidation rather
than any one of them in isolation.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from tests.support import config, fake_device

from cloudultron.errors import PolicyError
from cloudultron.loop.actions import Action
from cloudultron.loop.engine import Executor, StepOutcome, TerminalReason, wait_for_stable
from cloudultron.loop.policy import ExplorePolicy, NullPolicy, ScriptedPolicy
from cloudultron.safety import Guard
from cloudultron.testing.fake import FakeTransport


def run(policy, *, dry_run=True, steps=6, transport=None, config_overrides=None, until=None, guard=None):
    """Boot an executor, run it, hand back (engine, report, device).

    With no explicit ``guard`` the executor builds one from the config, which is
    the path a library caller uses, so the default here covers the wiring that
    matters most rather than only the hand-built case.
    """
    device, _ = fake_device()
    if transport is not None:
        from cloudultron.adb.device import AndroidDevice

        device = AndroidDevice(transport)
    cfg = config(dry_run=dry_run, max_steps=steps, **(config_overrides or {}))
    engine = Executor(device, policy, config=cfg, guard=guard)
    report = engine.run(max_steps=steps, until=until)
    return engine, report, device


class OneShotPolicy:
    """Emits one action then reports done, so a test asserts on exactly one step.

    A policy that repeats would loop to the step budget instead, and the counts
    would say nothing about the gate under test.
    """

    def __init__(self, action):
        self.pending = action

    def decide(self, observation):
        action, self.pending = self.pending, Action.done("test: single action only")
        return action


class GuardProfileWiringTests(unittest.TestCase):
    """The armed profile decides what a run may do, end to end."""

    @staticmethod
    def _transport():
        return FakeTransport()

    def test_explore_profile_refuses_a_policy_composed_command(self):
        engine, report, _ = run(
            OneShotPolicy(Action.raw_shell("reboot")), dry_run=False, transport=self._transport()
        )
        self.assertEqual(report.blocked, 1)
        self.assertEqual(report.executed, 0)
        self.assertIn("raw shell is disabled", engine.steps[0].detail)
        self.assertTrue(engine.steps[0].guard.startswith("block"), engine.steps[0].guard)

    def test_test_lab_profile_sends_a_lifecycle_command_to_the_device(self):
        transport = self._transport()
        engine, report, device = run(
            OneShotPolicy(Action.raw_shell("reboot")),
            dry_run=False,
            transport=transport,
            config_overrides={"guard_profile": "test-lab"},
        )
        self.assertEqual(report.executed, 1, "a permitted profile must actually reach the device")
        self.assertIn("reboot", transport.calls)
        self.assertTrue(engine.steps[0].guard.startswith("allow [destructive]"), engine.steps[0].guard)

    def test_test_lab_profile_still_refuses_the_wrecking_list(self):
        transport = self._transport()
        engine, report, _ = run(
            OneShotPolicy(Action.raw_shell("rm -rf /sdcard")),
            dry_run=False,
            transport=transport,
            config_overrides={"guard_profile": "test-lab"},
        )
        self.assertEqual(report.executed, 0)
        self.assertEqual(report.blocked, 1)
        self.assertEqual([c for c in transport.calls if c.startswith("rm ")], [])

    def test_operator_mode_executes_and_records_what_it_waived(self):
        # `reboot` is the case worth pinning: strict mode blocks it by verb, the
        # lab permits it, and only the operator runs it -- while still writing
        # down that it was a waiver. The fake has no `rm` handler on purpose, so
        # a delete reaching the device would abort the run instead of being
        # silently accepted by the double.
        transport = self._transport()
        engine, report, _ = run(
            OneShotPolicy(Action.raw_shell("reboot")),
            dry_run=False,
            transport=transport,
            config_overrides={"guard_profile": "operator"},
        )
        self.assertEqual(report.executed, 1, "operator mode must not leave the command unplanned")
        self.assertEqual(report.operator_overrides, 1)
        self.assertIn("operator override", engine.steps[0].detail)
        self.assertIn("reboot", engine.steps[0].detail)
        self.assertIn("reboot", transport.calls)
        self.assertEqual(report.guard_profile, "operator")
        self.assertTrue(report.operator_mode)
        self.assertTrue(
            engine.steps[0].guard.startswith("override [destructive]"), engine.steps[0].guard
        )
        self.assertIn("reboot", engine.steps[0].guard, "the column must say which verb was waived")

    def test_a_run_with_no_operator_waivers_does_not_claim_any(self):
        engine, report, _ = run(
            OneShotPolicy(Action.tap(0)),
            dry_run=False,
            transport=self._transport(),
            config_overrides={"guard_profile": "operator"},
        )
        self.assertEqual(report.executed, 1)
        self.assertEqual(report.operator_overrides, 0)

    def test_summary_names_the_ruleset(self):
        _, report, _ = run(ExplorePolicy(), dry_run=True, config_overrides={"guard_profile": "test-lab"})
        self.assertIn("guard=test-lab", report.summary())

    def test_summary_names_operator_mode_and_its_waivers(self):
        _, report, _ = run(
            OneShotPolicy(Action.raw_shell("reboot")),
            dry_run=False,
            transport=self._transport(),
            config_overrides={"guard_profile": "operator"},
        )
        self.assertIn("guard=operator(overrides=1)", report.summary())


class DryRunTests(unittest.TestCase):
    def test_dry_run_plans_without_touching_the_device(self):
        engine, report, device = run(
            ScriptedPolicy(actions=[Action.tap(0), Action.tap(1)]), dry_run=True, steps=3
        )
        self.assertEqual(report.planned, 2)
        self.assertEqual(report.executed, 0)
        self.assertTrue(report.dry_run)
        # Nothing reached the device except reads.
        self.assertEqual(device.transport.events, [])

    def test_dry_run_still_observes(self):
        engine, report, _ = run(NullPolicy(), dry_run=True, steps=4)
        self.assertEqual(report.observed, 4)
        self.assertEqual(report.steps, 4)

    def test_step_budget_is_a_clean_stop_not_a_failure(self):
        _, report, _ = run(NullPolicy(), dry_run=True, steps=3)
        self.assertEqual(report.terminal, TerminalReason.STEP_BUDGET)
        self.assertEqual(report.status, "ok")

    def test_execute_mode_dispatches_and_navigates(self):
        transport = FakeTransport()
        _, report, _ = run(ScriptedPolicy(actions=[Action.launch_app("com.example.app")]), dry_run=False, steps=2, transport=transport)
        self.assertEqual(report.executed, 1)
        self.assertEqual(transport.screen_name(), "home")

    def test_tap_by_index_actually_moves_screens(self):
        transport = FakeTransport()
        scripted = ScriptedPolicy(actions=[Action.launch_app("com.example.app"), Action.tap(0)])
        _, report, _ = run(scripted, dry_run=False, steps=3, transport=transport)
        self.assertEqual(report.executed, 2)
        self.assertEqual(transport.screen_name(), "detail", "index 0 on home is Continue -> detail")


class TripwireTests(unittest.TestCase):
    def test_stagnation_stops_a_run_that_is_getting_nowhere(self):
        transport = FakeTransport()
        hammer = ScriptedPolicy(actions=[Action.tap_point(540, 1285)] * 8)  # dead "Buy now"
        _, report, _ = run(hammer, dry_run=False, steps=10, transport=transport, config_overrides={"stagnation_limit": 3})
        self.assertEqual(report.terminal, TerminalReason.STAGNATION)
        self.assertLess(report.steps, 10)
        self.assertIn("not changing the screen", report.reason)

    def test_oscillation_is_caught_even_though_every_step_changed(self):
        transport = FakeTransport()
        ping_pong = ScriptedPolicy(
            actions=[Action.launch_app("com.example.app"), Action.tap(0), Action.tap(2), Action.tap(0), Action.tap(2), Action.tap(0)]
        )
        engine, report, _ = run(ping_pong, dry_run=False, steps=12, transport=transport)
        self.assertEqual(report.terminal, TerminalReason.OSCILLATION)
        # The lesson of this test: every step reported a structural change, so a
        # naive "did the screen change?" trip would have declared all of this fine.
        changes = [s.change_level for s in engine.steps if s.change_level == "structural"]
        self.assertGreaterEqual(len(changes), 3)

    def test_livelock_catches_a_repeating_policy_even_in_dry_run(self):
        class Stuck:
            name = "stuck"

            def decide(self, observation):
                return Action.tap(0, rationale="this one looks promising")

        _, report, _ = run(Stuck(), dry_run=True, steps=10, config_overrides={"policy_livelock_limit": 3, "stagnation_limit": 99})
        self.assertEqual(report.terminal, TerminalReason.LIVELOCK)

    def test_repeated_identical_actions_are_recognised_despite_rewording(self):
        class Rewording:
            name = "rewording"

            def __init__(self):
                self.i = 0

            def decide(self, observation):
                self.i += 1
                # Same decision, new prose every step -- exactly how an LLM policy
                # loops while a naive "same command?" check sees novelty.
                return Action.tap(1, rationale=f"attempt number {self.i}")

        _, report, _ = run(Rewording(), dry_run=True, steps=10, config_overrides={"policy_livelock_limit": 3, "stagnation_limit": 99})
        self.assertEqual(report.terminal, TerminalReason.LIVELOCK)

    def test_dry_run_does_not_report_stagnation_for_a_frozen_screen(self):
        # Nothing was dispatched, so "no change" is the expected outcome; calling
        # that a loop would trip on every dry run and teach everyone to ignore it.
        _, report, _ = run(ExplorePolicy(), dry_run=True, steps=3, config_overrides={"stagnation_limit": 2})
        self.assertNotEqual(report.terminal, TerminalReason.STAGNATION)


class FailureHandlingTests(unittest.TestCase):
    def test_unreadable_screen_ends_the_run_after_three_attempts(self):
        transport = FakeTransport()
        transport.dump_fails = True
        engine, report, _ = run(NullPolicy(), dry_run=True, steps=10, transport=transport)
        self.assertEqual(report.terminal, TerminalReason.DUMP_UNREADABLE)
        self.assertEqual(engine.steps[0].outcome, StepOutcome.REFUSED.value)
        self.assertIn("force-stop com.android.shell", report.reason)

    def test_one_failed_dump_is_absorbed_by_the_fallback_ladder(self):
        # hierarchy() makes up to three attempts (dump+read, /dev/tty, default
        # path), so a single failure should be invisible: the step still yields a
        # screen, just via a different route.
        class FlakyTransport(FakeTransport):
            def __init__(self, fail_first=1):
                super().__init__()
                self.remaining = fail_first

            def _on_uiautomator(self, argv):
                if self.remaining > 0:
                    self.remaining -= 1
                    return self._Result(argv, 1, "", "ERROR: could not get idle state.")
                return super()._on_uiautomator(argv)

        engine, report, _ = run(NullPolicy(), dry_run=True, steps=4, transport=FlakyTransport(fail_first=1))
        self.assertEqual(report.terminal, TerminalReason.STEP_BUDGET)
        self.assertEqual(report.observed, 4)
        self.assertEqual(engine.steps[0].method, "dump /dev/tty", "fell through to attempt 2")

    def test_one_unreadable_step_does_not_end_the_run(self):
        # Fail every attempt in one step: the ladder is exhausted, the step is
        # refused, and the run carries on. Only three such steps in a row stop it.
        class FlakyTransport(FakeTransport):
            def __init__(self, fail_first=4):
                super().__init__()
                self.remaining = fail_first

            def _on_uiautomator(self, argv):
                if self.remaining > 0:
                    self.remaining -= 1
                    return self._Result(argv, 1, "", "ERROR: could not get idle state.")
                return super()._on_uiautomator(argv)

        engine, report, _ = run(NullPolicy(), dry_run=True, steps=4, transport=FlakyTransport(fail_first=4))
        self.assertEqual(engine.steps[0].outcome, StepOutcome.REFUSED.value)
        self.assertEqual(report.terminal, TerminalReason.STEP_BUDGET)
        self.assertEqual(report.observed, 3)

    def test_policy_errors_are_counted_and_bounded(self):
        class Broken:
            name = "broken"

            def decide(self, observation):
                raise PolicyError("I could not parse the screen")

        engine, report, _ = run(Broken(), dry_run=True, steps=10)
        self.assertEqual(report.terminal, TerminalReason.POLICY_ERRORS)
        self.assertTrue(all(s.outcome == StepOutcome.REFUSED.value for s in engine.steps))

    def test_a_policy_returning_garbage_is_refused_not_executed(self):
        class Garbage:
            name = "garbage"

            def decide(self, observation):
                return {"op": "tap", "index": 0}  # a dict, not an Action

        engine, report, _ = run(Garbage(), dry_run=True, steps=2)
        self.assertEqual(engine.steps[0].outcome, StepOutcome.REFUSED.value)
        self.assertIn("not an Action", engine.steps[0].detail)
        self.assertEqual(report.executed, 0)

    def test_out_of_range_index_is_refused_before_anything_reaches_the_device(self):
        transport = FakeTransport()
        engine, report, _ = run(ScriptedPolicy(actions=[Action.tap(99)]), dry_run=False, steps=1, transport=transport)
        self.assertEqual(engine.steps[0].outcome, StepOutcome.REFUSED.value)
        self.assertIn("not addressable", engine.steps[0].detail)
        self.assertEqual(transport.events, [], "a refused action must not have been dispatched")
        self.assertEqual(report.refused, 1)

    def test_done_and_abort_are_terminal(self):
        _, done_report, _ = run(ScriptedPolicy(actions=[Action.done("we are done here")]), steps=9)
        self.assertEqual(done_report.terminal, TerminalReason.DONE)
        self.assertEqual(done_report.status, "done")
        self.assertEqual(done_report.steps, 1)

        _, abort_report, _ = run(ScriptedPolicy(actions=[Action.abort("cannot proceed")]), steps=9)
        self.assertEqual(abort_report.terminal, TerminalReason.ABORTED)


class GoalAndRecordingTests(unittest.TestCase):
    def test_until_callback_can_be_the_goal_test(self):
        transport = FakeTransport()
        # Index 0 on the launcher is the Settings icon (sorted by y then x).
        policy = ScriptedPolicy(actions=[Action.tap(0), Action.tap(0)])
        engine, report, _ = run(policy, dry_run=False, steps=10, transport=transport, until=lambda record: record.package == "com.android.settings")
        self.assertEqual(report.terminal, TerminalReason.DONE)
        self.assertIn("goal", report.reason)
        self.assertLess(report.steps, 10)

    def test_trace_is_written_and_reloadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(dry_run=True, max_steps=2, record_dir=tmp, settle_delay=0.0)
            device, _ = fake_device()
            engine = Executor(device, NullPolicy(), config=cfg, guard=Guard(dry_run=True))
            report = engine.run()
            trace = pathlib.Path(report.trace_path)
            self.assertTrue(trace.exists())
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertIn("structure_hash", rows[0])
            self.assertIn("outcome", rows[1])
            # The config is recorded too, so a trace is interpretable standalone.
            recorded = json.loads((trace.parent / "config.json").read_text())
            self.assertIn("dry_run", recorded["config"])
            self.assertTrue(recorded["config"]["dry_run"])
            self.assertTrue((trace.parent / "report.json").exists())

    def test_on_step_hook_sees_every_step(self):
        seen: list[StepOutcome] = []
        cfg = config(dry_run=True, max_steps=3, settle_delay=0.0)
        device, _ = fake_device()
        engine = Executor(device, NullPolicy(), config=cfg, on_step=lambda record: seen.append(record.outcome))
        engine.run()
        self.assertEqual(len(seen), 3)

    def test_structure_changes_excludes_the_baseline_observation(self):
        engine, report, _ = run(NullPolicy(), dry_run=True, steps=3)
        self.assertEqual(report.structure_changes, 0, "the first screen is not a transition")


class StableWaitTests(unittest.TestCase):
    def test_wait_for_stable_returns_immediately_on_a_quiet_screen(self):
        device, _ = fake_device()
        stable, screen = wait_for_stable(device, timeout=2.0, interval=0.01, needed=2)
        self.assertTrue(stable)
        self.assertIsNotNone(screen)

    def test_wait_for_stable_ignores_text_churn_on_a_static_layout(self):
        device, transport = fake_device()
        transport.screens["launcher"].clock_sequence = ["a", "b", "c", "d", "e", "f"]
        # Content-only churn must not defeat it: structure is stable here, which
        # is the whole reason wait_for_stable watches the structure hash and not
        # the content hash. Otherwise every screen with a clock never "settles".
        stable, _ = wait_for_stable(device, timeout=1.0, interval=0.01, needed=2)
        self.assertTrue(stable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
