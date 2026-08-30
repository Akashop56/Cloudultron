"""Low-level adb invocation. Everything that touches ``subprocess`` lives here.

Two things this layer has to get right, both of which are easy to get wrong and
quietly catastrophic when you do:

**Quoting.** ``adb shell a b c`` sends the *joined* string to a shell on the
device, which re-parses it. So ``device.shell(["input", "text", "hello; rm -rf /"])``
must not become an injection. We build the device-side string with
:func:`shlex.join`, which quotes each token, and never accept a pre-joined
string from a policy.

**stdout integrity.** ``adb shell`` runs through a pty that rewrites ``\\n`` as
``\\n\\r`` and can mangle high bytes; that is fatal for an XML dump. ``adb
exec-out`` exists for exactly this and is preferred, with a documented fallback
for old adb builds that lack it (common on Termux, where the bundled adb varies).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from ..errors import AdbCommandFailed, DeviceTimeout, TransportError, TransportUnavailable

#: Substrings in adb's own stderr that mean "retry in a moment", not "give up".
_TRANSIENT = (
    "device offline",
    "device not found",
    "no devices/emulators found",
    "unauthorized",
    "waiting for device",
    "protocol fault",
    "closed",
)


#: adb subcommands that talk to the *server* rather than a device, and so must
#: not carry ``-s``. Sending `-s host:5555 connect host:5555` is wrong twice
#: over: connect is what makes the serial known in the first place, and some adb
#: builds reject the flag outright for server-level verbs.
SERVER_LEVEL_COMMANDS = frozenset(
    {"connect", "disconnect", "devices", "version", "kill-server", "start-server", "get-state", "reboot"}
)


@dataclass(frozen=True)
class CommandResult:
    """One completed invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    duration_ms: int = 0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def error_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def raise_for_status(self) -> "CommandResult":
        if not self.ok:
            raise AdbCommandFailed(list(self.argv), self.returncode, self.error_text)
        return self

    def lines(self) -> list[str]:
        return [line.strip() for line in self.text.splitlines() if line.strip()]


class Transport(Protocol):
    """What the rest of the harness needs from a transport.

    Deliberately tiny: the fake transport used in tests and ``--mock`` only has
    to satisfy this, which keeps the executor honest about what it may assume.
    """

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult: ...

    def shell(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult: ...

    def shell_raw(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Send an already-assembled device-side string.

        Only the guard-checked escape hatch uses this, but it is part of the
        protocol rather than an ``AdbTransport`` extra, so a fake that omits it
        fails loudly instead of falling off a test-only code path.
        """
        ...  # pragma: no cover


@dataclass
class AdbTransport:
    """Runs ``adb`` against one serial, with retries for transient device states.

    Parameters
    ----------
    serial:
        Value for ``-s``. ``None`` uses adb's default device, which is exactly
        what you want on a single-emulator Termux box.
    auto_connect:
        For ``host:port`` serials, issue ``adb connect`` once before first use.
    exec_out:
        Prefer ``adb exec-out`` for stdout-sensitive reads. Auto-detected: we try
        it once and permanently fall back to ``adb shell`` if adb says no.
    """

    serial: str | None = None
    adb_path: str = "adb"
    timeout: float = 30.0
    retries: int = 2
    backoff: float = 1.5
    auto_connect: bool = True
    exec_out: bool = True
    _supports_exec_out: bool | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        resolved = shutil.which(self.adb_path)
        if resolved:
            self.adb_path = resolved
        elif os.sep in self.adb_path:
            if not os.path.exists(self.adb_path):
                raise TransportUnavailable(f"adb not found at {self.adb_path!r}")
        else:
            raise TransportUnavailable(
                f"adb binary {self.adb_path!r} not found on PATH. On Termux: "
                "pkg install android-tools. On a workstation: any platform-tools."
            )

    # ----------------------------------------------------------- lifecycle

    def check(self) -> dict[str, object]:
        """Diagnostics for ``cloudultron doctor``. Never mutates the device."""
        info: dict[str, object] = {"adb_path": self.adb_path, "serial": self.serial}
        try:
            version = self.run(["version"], timeout=10.0)
            info["adb_version"] = version.text.strip().splitlines()[0] if version.text.strip() else "?"
        except (TransportError, OSError) as exc:
            info["error"] = str(exc)
            return info
        try:
            devices = self.run(["devices"], timeout=10.0)
            rows = [l for l in devices.text.splitlines()[1:] if l.strip()]
            info["devices"] = [l.split("\t") for l in rows]
            if self.serial:
                matching = [r for r in info["devices"] if isinstance(r, list) and r and r[0] == self.serial]
                info["serial_state"] = matching[0][1] if matching else "absent"
            elif rows:
                info["note"] = "no serial set; adb will use the sole device"
            else:
                info["note"] = "no devices attached"
        except TransportError as exc:
            info["error"] = str(exc)
        return info

    def ensure_connected(self) -> None:
        """Best-effort ``adb connect`` for network serials, called before first use."""
        if self._connected or not self.serial or ":" not in self.serial:
            self._connected = True
            return
        self._connected = True
        try:
            result = self.run(["connect", self.serial], timeout=min(15.0, self.timeout))
            out = (result.text + result.error_text).lower()
            if "unable to" in out or "failed to connect" in out or "connection refused" in out:
                raise TransportUnavailable(
                    f"adb connect {self.serial} refused. Is the emulator up, and has "
                    "`adb tcpip 5555` (or an emulator with -no-window) been run on it?"
                )
        except OSError as exc:  # pragma: no cover - adb vanished mid-run
            raise TransportUnavailable(f"adb connect failed: {exc}") from exc

    # ------------------------------------------------------------- running

    def build_argv(self, prefix: Sequence[str]) -> list[str]:
        argv = [self.adb_path]
        first = str(prefix[0]) if len(prefix) else ""
        # -s is a *device* selector, so it only applies to the commands that take
        # a device. Passing it to `adb connect` (see the constant) is how you end
        # up with a target that cannot be reached because of the flag meant to
        # reach it.
        if self.serial and first not in SERVER_LEVEL_COMMANDS:
            argv += ["-s", self.serial]
        argv.extend(prefix)
        return argv

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        """Execute ``adb <argv>`` directly (for ``devices``, ``connect``, ...)."""
        return self._execute(self.build_argv(argv), timeout=timeout)

    def shell(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        """Execute a device-side command as a *token list*."""
        if isinstance(argv, str):  # guard against the one mistake that matters
            raise TypeError("shell() takes a list of argv tokens, not a shell string")
        quoted = " ".join(_quote(token) for token in argv)
        return self._execute(self.build_argv(["shell", quoted]), timeout=timeout)

    def shell_raw(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Send an already-assembled shell string. Guard-checked callers only."""
        return self._execute(self.build_argv(["shell", command]), timeout=timeout)

    def exec_out_shell(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        """``adb exec-out`` when available, else ``adb shell`` with \\r stripped.

        Used for anything where stdout fidelity matters (the XML dump, screencap).
        """
        if self._supports_exec_out is None:
            probe = self.run(["exec-out", "--help"], timeout=10.0)
            # Old adb prints a usage error for unknown verbs; accept either
            # "usage: adb exec-out" or a clean exit as support.
            self._supports_exec_out = probe.ok or "exec-out" in (probe.text + probe.error_text).lower()
        quoted = " ".join(_quote(token) for token in argv)
        if self._supports_exec_out:
            try:
                return self._execute(self.build_argv(["exec-out", quoted]), timeout=timeout)
            except AdbCommandFailed:
                self._supports_exec_out = False
        result = self.shell(argv, timeout=timeout)
        return CommandResult(
            argv=result.argv,
            returncode=result.returncode,
            stdout=result.stdout.replace(b"\r\n", b"\n"),
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            attempts=result.attempts,
        )

    # ------------------------------------------------------------- internals

    def _execute(self, argv: list[str], *, timeout: float | None = None, attempt: int = 0) -> CommandResult:
        budget = self.timeout if timeout is None else timeout
        if not self._connected:
            self.ensure_connected()
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=budget,
                check=False,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or b""
            raise DeviceTimeout(f"timeout after {budget:.1f}s: {' '.join(argv)}{': ' + partial.decode('utf-8', 'replace')[:200] if partial else ''}") from exc
        except OSError as exc:  # adb removed, EACCES, ENOMEM
            raise TransportError(f"could not run {argv[0]!r}: {exc}") from exc

        duration = int((time.monotonic() - started) * 1000)
        result = CommandResult(
            argv=tuple(argv),
            returncode=proc.returncode,
            stdout=proc.stdout or b"",
            stderr=proc.stderr or b"",
            duration_ms=duration,
            attempts=attempt + 1,
        )
        if not result.ok and attempt < self.retries and self._is_transient(result):
            time.sleep(self.backoff * (attempt + 1))
            return self._execute(argv, timeout=timeout, attempt=attempt + 1)
        return result

    @staticmethod
    def _is_transient(result: CommandResult) -> bool:
        blob = (result.text + result.error_text).lower()
        return any(marker in blob for marker in _TRANSIENT)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.serial:
            # Belt-and-braces: some adb subcommands ignore -s but honour ANDROID_SERIAL.
            env.setdefault("ANDROID_SERIAL", self.serial)
        return env


def _quote(token: object) -> str:
    """Quote one device-side token.

    ``input text`` additionally treats a space as an argument separator, which
    is why :meth:`AndroidDevice.input_text` converts spaces to ``%s`` itself;
    here we only ensure the *shell* cannot split or interpret the token.
    """
    import shlex

    return shlex.quote(str(token))
