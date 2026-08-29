"""Tests for the adb transport: argv construction, quoting, retries, fallbacks.

``subprocess.run`` is patched throughout. These tests are about what we *send*,
because that is the part that can be wrong in a way the device never tells you
about -- a mis-quoted argument looks exactly like a working command until it is a
``rm``.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from cloudultron.adb.transport import AdbTransport
from cloudultron.errors import DeviceTimeout, TransportUnavailable


def completed(stdout=b"", stderr=b"", rc=0):
    return subprocess.CompletedProcess(args=["adb"], returncode=rc, stdout=stdout, stderr=stderr)


class Recorder:
    """Stand-in for subprocess.run that records argv and replays canned results."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        # Consume the canned results in order, then keep returning the last one,
        # so a test that asserts "exactly one retry happened" does not IndexError.
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class TransportConstructionTests(unittest.TestCase):
    def make(self, **kwargs) -> AdbTransport:
        kwargs.setdefault("adb_path", sys.executable)
        kwargs.setdefault("retries", 0)
        return AdbTransport(**kwargs)

    def test_serial_becomes_dash_s(self):
        transport = self.make(serial="emulator-5554")
        self.assertEqual(transport.build_argv(["shell", "ls"]), [sys.executable, "-s", "emulator-5554", "shell", "ls"])

    def test_no_serial_omits_dash_s(self):
        transport = self.make(serial=None)
        self.assertEqual(transport.build_argv(["devices"]), [sys.executable, "devices"])

    def test_missing_adb_is_reported_at_construction_not_first_use(self):
        # Failing in __post_init__ means `doctor` can explain the problem before
        # a run has started, instead of dying on step 1.
        with self.assertRaises(TransportUnavailable) as ctx:
            self.make(adb_path="definitely-not-an-adb-binary")
        self.assertIn("not found", str(ctx.exception))

    def test_absolute_adb_path_that_exists_is_accepted(self):
        transport = self.make(adb_path=sys.executable)
        self.assertEqual(transport.adb_path, sys.executable)

    def test_absolute_adb_path_that_does_not_exist_is_rejected(self):
        with self.assertRaises(TransportUnavailable):
            self.make(adb_path="/nonexistent/bin/adb")


class QuotingTests(unittest.TestCase):
    def make(self, **kwargs):
        kwargs.setdefault("adb_path", sys.executable)
        kwargs.setdefault("retries", 0)
        return AdbTransport(**kwargs)

    def test_tokens_are_quoted_into_one_device_side_string(self):
        recorder = Recorder([completed(stdout=b"ok")])
        transport = self.make()
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.shell(["input", "text", "hello world"])
        sent = recorder.calls[0][-1]
        self.assertEqual(sent, "input text 'hello world'")

    def test_semicolons_cannot_become_command_separators(self):
        recorder = Recorder([completed()])
        transport = self.make()
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.shell(["input", "text", "a;rm -rf /sdcard"])
        sent = recorder.calls[0][-1]
        # The payload is quoted, so the device shell sees it as one argument.
        self.assertTrue(sent.endswith("'a;rm -rf /sdcard'"), sent)
        self.assertNotIn(" rm ", sent.replace("'a;rm -rf /sdcard'", ""))

    def test_shell_rejects_a_prejoined_string(self):
        transport = self.make()
        with self.assertRaises(TypeError):
            transport.shell("input tap 1 2")  # type: ignore[arg-type]

    def test_android_serial_is_exported_for_adb(self):
        recorder = Recorder([completed()])
        transport = self.make(serial="10.0.0.5:5555")
        transport._connected = True  # skip the connect step for this test
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder), mock.patch.dict("os.environ", {}, clear=True):
            transport.shell(["ls"])
        self.assertEqual(recorder.kwargs[0]["env"]["ANDROID_SERIAL"], "10.0.0.5:5555")

    def test_stdin_is_closed_so_adb_cannot_hang_on_it(self):
        recorder = Recorder([completed()])
        transport = self.make()
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.shell(["ls"])
        self.assertIs(recorder.kwargs[0]["stdin"], subprocess.DEVNULL)


class ConnectTests(unittest.TestCase):
    def test_network_serial_triggers_adb_connect_once(self):
        recorder = Recorder([completed(stdout=b"connected to 10.0.0.5:5555"), completed(stdout=b"")])
        transport = AdbTransport(adb_path=sys.executable, serial="10.0.0.5:5555", retries=0)
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.shell(["ls"])
            transport.shell(["ls"])
        self.assertEqual(recorder.calls[0][:2], [sys.executable, "connect"])
        self.assertEqual(len([c for c in recorder.calls if "connect" in c]), 1)

    def test_server_level_commands_omit_the_device_selector(self):
        # `adb -s <serial> connect <serial>` is self-defeating: the serial is what
        # connect is establishing. Regression test for that exact argv.
        recorder = Recorder([completed(stdout=b"connected")])
        transport = AdbTransport(adb_path=sys.executable, serial="10.0.0.5:5555", retries=0)
        argv = transport.build_argv(["connect", "10.0.0.5:5555"])
        self.assertNotIn("-s", argv)
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.run(["connect", "10.0.0.5:5555"])
        self.assertEqual(recorder.calls[0], [sys.executable, "connect", "10.0.0.5:5555"])

    def test_usb_serial_never_connects(self):
        recorder = Recorder([completed()])
        transport = AdbTransport(adb_path=sys.executable, serial="emulator-5554", retries=0)
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.shell(["ls"])
        self.assertNotIn("connect", recorder.calls[0])


class RetryTests(unittest.TestCase):
    def make(self, **kwargs):
        kwargs.setdefault("adb_path", sys.executable)
        kwargs.setdefault("backoff", 0.0)
        return AdbTransport(**kwargs)

    def test_transient_device_state_is_retried(self):
        recorder = Recorder([completed(stderr=b"adb: device offline", rc=1), completed(stdout=b"ok")])
        transport = self.make(retries=2)
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            result = transport.shell(["ls"])
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(recorder.calls), 2)

    def test_real_errors_are_not_retried(self):
        recorder = Recorder([completed(stderr=b"error: no permissions", rc=1)])
        transport = self.make(retries=3)
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            result = transport.shell(["ls"])
        self.assertFalse(result.ok)
        self.assertEqual(len(recorder.calls), 1)

    def test_unauthorized_is_transient(self):
        recorder = Recorder([completed(stderr=b"error: device unauthorized.", rc=1), completed()])
        transport = self.make(retries=1)
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            result = transport.shell(["ls"])
        self.assertTrue(result.ok, "an unaccepted RSA prompt should retry, not fail the run")

    def test_timeout_becomes_device_timeout(self):
        def raise_timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

        transport = self.make(retries=0)
        with mock.patch("cloudultron.adb.transport.subprocess.run", raise_timeout):
            with self.assertRaises(DeviceTimeout):
                transport.shell(["uiautomator", "dump"])

    def test_oserror_becomes_transport_error(self):
        def boom(argv, **kwargs):
            raise OSError(8, "Exec format error")

        transport = self.make(retries=0)
        with mock.patch("cloudultron.adb.transport.subprocess.run", boom):
            with self.assertRaises(Exception) as ctx:
                transport.shell(["ls"])
        self.assertIn("could not run", str(ctx.exception))


class ExecOutTests(unittest.TestCase):
    def make(self, **kwargs):
        kwargs.setdefault("adb_path", sys.executable)
        kwargs.setdefault("retries", 0)
        return AdbTransport(**kwargs)

    def test_exec_out_is_preferred_once_supported(self):
        recorder = Recorder([completed(stdout=b"ok"), completed(stdout=b"<hierarchy/>")])
        transport = self.make()
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            transport.exec_out_shell(["uiautomator", "dump", "/dev/tty"])
        self.assertEqual(recorder.calls[0][1:], ["exec-out", "--help"])
        # shlex.quote leaves path-safe tokens (`/`, `.`) bare, so `/dev/tty` is
        # not quoted -- what matters is that a token needing quoting gets it.
        self.assertEqual(recorder.calls[1][1:], ["exec-out", "uiautomator dump /dev/tty"])

    def test_fallback_strips_the_shell_crlf_translation(self):
        # One canned result: with support already known to be absent there is no
        # probe call, so the shell fallback is the only invocation.
        recorder = Recorder([completed(stdout=b"<hierarchy>\r\n<node/>\r\n</hierarchy>\r\n")])
        transport = self.make()
        transport._supports_exec_out = False
        with mock.patch("cloudultron.adb.transport.subprocess.run", recorder):
            result = transport.exec_out_shell(["cat", "/sdcard/window_dump.xml"])
        self.assertNotIn("\r", result.text)
        self.assertIn("<hierarchy>", result.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
