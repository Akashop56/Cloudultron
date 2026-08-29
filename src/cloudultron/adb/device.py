"""Device-facing verbs: the only place the harness names an adb operation.

:class:`AndroidDevice` is intentionally *dumb but complete*: it knows how to
get a hierarchy dump out of a reluctant device and how to synthesise input
events, and nothing about policy, retries of actions, or state. That separation
is what lets the fake in :mod:`cloudultron.testing.fake` substitute for a phone
without touching the executor.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Sequence

from ..errors import DeviceError, HierarchyUnavailable
from ..ui.model import Screen
from ..ui.parser import ParseReport, parse_hierarchy
from .transport import CommandResult, Transport

#: On-device scratch path for the hierarchy dump.
DUMP_PATH = "/sdcard/window_dump.xml"

#: uiautomator prints this on success; it is noise for the parser, a signal here.
_DUMP_OK = re.compile(r"UI hierchary dumped to", re.IGNORECASE)


@dataclass
class Hierarchy:
    """A parsed dump plus the provenance needed to debug it."""

    screen: Screen
    raw: str
    report: ParseReport
    #: Which rung of the fallback ladder produced this, e.g. ``dump+read``.
    method: str
    #: Whole fetch cost, not just the parse: "the dump is slow" and "the parse is
    #: slow" are different bugs on different machines.
    elapsed_ms: int = 0
    #: What the earlier rungs said, so a successful fallback still shows the
    #: trouble it worked around.
    attempts: list[str] = field(default_factory=list)

    @property
    def noteworthy(self) -> bool:
        return self.report.noteworthy


class AndroidDevice:
    """High-level operations over a :class:`Transport`.

    Parameters
    ----------
    transport:
        Anything satisfying the ``run``/``shell`` protocol.
    dump_path:
        Where to write ``uiautomator dump`` on the device.
    hierarchy_cache_ms:
        Collapse duplicate dumps within this window. The loop dumps right after
        an action *and* again to verify it; both reads can legitimately share one
        dump, which halves adb traffic on a slow WiFi link.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        dump_path: str = DUMP_PATH,
        timeout: float = 30.0,
        hierarchy_cache_ms: int = 120,
    ) -> None:
        self.transport = transport
        self.dump_path = dump_path
        self.timeout = timeout
        self.hierarchy_cache_ms = hierarchy_cache_ms
        self._cached: Hierarchy | None = None
        self._cached_at = 0.0
        self.last_error: str = ""

    # ---------------------------------------------------------- observation

    def hierarchy(self, *, force: bool = False) -> Hierarchy:
        """Fetch and parse the current window hierarchy.

        Tries, in order:

        1. ``uiautomator dump <path>`` then read the file back. The reliable one,
           because it separates "generate" from "transfer".
        2. ``uiautomator dump /dev/tty`` (via exec-out) to avoid the file, useful
           when ``/sdcard`` is unavailable or FUSE is slow.
        3. ``uiautomator dump`` with no argument, reading the default path.

        Failures are reported as :class:`HierarchyUnavailable` with the raw output
        attached, because "empty" and "ERROR: null root" need different fixes.
        """
        started = time.monotonic()
        if (
            not force
            and self._cached is not None
            and (started - self._cached_at) * 1000 < self.hierarchy_cache_ms
        ):
            return self._cached

        attempts: list[str] = []

        # 1. dump to file, read back via exec-out for byte fidelity.
        result = self._try_shell(["uiautomator", "dump", self.dump_path])
        if result is not None and (result.ok or _DUMP_OK.search(result.text)):
            body = self._read_dump_file()
            if body:
                return self._finish(body, "dump+read", attempts, started)
            attempts.append("wrote dump but could not read it back")
        elif result is not None:
            attempts.append(_compress(result.error_text or result.text))

        # 2. /dev/tty straight to stdout.
        out = self._try_exec_out(["uiautomator", "dump", "/dev/tty"])
        if out and "<hierarchy" in out:
            return self._finish(_strip_dump_trailer(out), "dump /dev/tty", attempts, started)
        if out:
            attempts.append(_compress(out))

        # 3. no path: some builds refuse an explicit one, and this also proves
        #    the file exists for the next caller.
        result = self._try_shell(["uiautomator", "dump"])
        if result is not None and result.ok:
            body = self._read_dump_file()
            if body:
                return self._finish(body, "dump default path", attempts, started)
        elif result is not None:
            attempts.append(_compress(result.error_text or result.text))

        raise HierarchyUnavailable(
            "could not obtain a window hierarchy: " + ("; ".join(attempts) or "uiautomator unavailable"),
            raw_output="\n".join(attempts),
        )

    def _finish(self, body: str, method: str, attempts: list[str], started: float) -> Hierarchy:
        screen, report = parse_hierarchy(body)
        hierarchy = Hierarchy(
            screen=screen,
            raw=body,
            report=report,
            method=method,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            attempts=list(attempts),
        )
        self._cached = hierarchy
        self._cached_at = time.monotonic()
        return hierarchy

    def _read_dump_file(self) -> str:
        result = self._try_exec_out(["cat", self.dump_path])
        if result and "<hierarchy" in result:
            return result
        # Some Termux/rootless setups cannot read /sdcard; fall back to /data/local/tmp.
        alt = "/data/local/tmp/window_dump.xml"
        self._try_shell(["uiautomator", "dump", alt])
        result = self._try_exec_out(["cat", alt])
        return result if result and "<hierarchy" in result else ""

    # ---------------------------------------------------------- foreground

    def current_focus(self) -> str:
        """``package/activity`` of the focused window, or ``""``.

        Tokenises the window record instead of regex-matching a fixed shape.
        AOSP has emitted at least three layouts for this line across versions::

            mCurrentFocus=Window{2c4c429 u0 com.foo/com.foo.Bar}
            mCurrentFocus=Window{2c4c429 com.foo/com.foo.Bar}
            mCurrentFocus=Window{com.foo/com.foo.Bar}

        All three put ``package/activity`` as the last whitespace-separated token
        inside the braces, so we read that field and skip the token-count problem
        entirely. A pattern for any one layout silently returns "" on another,
        which is the kind of failure that looks like "the app never launched".
        """
        for args in (
            ["dumpsys", "window"],
            ["dumpsys", "activity", "activities"],
        ):
            result = self._try_shell(args)
            if not result or not result.ok:
                continue
            for key in ("mCurrentFocus", "mFocusedApp", "mResumedActivity", "topResumedActivity"):
                value = _extract_window_token(result.text, key)
                if value:
                    return value
        return ""

    def screen_size(self) -> tuple[int, int]:
        """Physical-ish ``width, height`` from ``wm size``, for swipe geometry."""
        result = self._try_shell(["wm", "size"])
        if result and result.ok:
            match = re.search(r"(\d+)\s*x\s*(\d+)", result.text)
            if match:
                return (int(match.group(1)), int(match.group(2)))
        return (0, 0)

    def wait_for_device(self, timeout: float = 20.0) -> bool:
        """Poll ``getprop sys.boot_completed``. True once the device is ready."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._try_shell(["getprop", "sys.boot_completed"])
            if result and result.ok and "1" in result.text:
                return True
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------ mutation

    def tap(self, x: int, y: int) -> CommandResult:
        return self._require(self._try_shell(["input", "tap", str(int(x)), str(int(y))]), "tap")

    def long_press(self, x: int, y: int, duration_ms: int = 700) -> CommandResult:
        # `input touch` with a swipe of equal endpoints is the portable way to
        # get a hold: `input swipe x y x y t` works on 8+, `touch` on 6-7.
        return self._require(
            self._try_shell(["input", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(int(duration_ms))]),
            "long_press",
        )

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> CommandResult:
        return self._require(
            self._try_shell(
                ["input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms))]
            ),
            "swipe",
        )

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 900) -> CommandResult:
        return self._require(
            self._try_shell(
                ["input", "draganddrop", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms))]
            ),
            "drag",
        )

    def input_text(self, text: str) -> CommandResult:
        """Type a literal string.

        ``input text`` splits on whitespace, so spaces become ``%s``. Shell
        metacharacters are handled by the transport's quoting, but ``input``
        itself still chokes on a handful of bytes, so those go through
        :func:`_sanitize_input_text`. Non-ASCII is rejected by AOSP's ``input``
        on most Android versions -- we surface that instead of typing garbage.
        """
        cleaned = _sanitize_input_text(text)
        if cleaned is None:
            raise DeviceError("input text cannot encode this string (non-ASCII needs ADBKeyboard/ime)")
        return self._require(self._try_shell(["input", "text", cleaned.replace(" ", "%s")]), "input_text")

    def keyevent(self, code: int | str) -> CommandResult:
        return self._require(self._try_shell(["input", "keyevent", str(code)]), "keyevent")

    def press_back(self) -> CommandResult:
        return self.keyevent(4)

    def press_home(self) -> CommandResult:
        return self.keyevent(3)

    def clear_text_field(self, max_chars: int = 64) -> CommandResult:
        """Select-all then delete: the only portable "clear field" on Android.

        Using KEYCODE_MOVE_END + repeated DEL would be slower and, unlike this,
        is not idempotent when the field is already empty.
        """
        self.keyevent("KEYCODE_MOVE_END")
        self.keyevent(29)  # Ctrl-A via meta state below
        return self._require(
            self._try_shell(["input", "keyevent", "--meta-state", "1", "29"]),
            "select_all",
        )

    def start_activity(self, component: str, *, action: str | None = None, flags: Sequence[str] = ()) -> CommandResult:
        """``component`` is ``pkg/.Activity`` or ``pkg/pkg.Activity``.

        Normalising a leading ``.`` to the package-qualified name is required --
        ``am`` does it for a human, but we are building an argv list and want the
        same behaviour explicitly.
        """
        if not component:
            raise DeviceError("start_activity requires a component")
        if "/" not in component:
            raise DeviceError(f"component {component!r} must be 'package/activity'")
        package, activity = component.split("/", 1)
        if activity.startswith("."):
            activity = package + activity
        argv = ["am", "start", "-n", f"{package}/{activity}"]
        if action:
            argv += ["-a", action]
        for flag in flags:
            argv += ["-f", flag]
        argv += ["--activity-clear-task"]
        return self._require(self._try_shell(argv), "start_activity")

    def start_url(self, url: str) -> CommandResult:
        return self._require(
            self._try_shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", url]),
            "start_url",
        )

    def force_stop(self, package: str) -> CommandResult:
        return self._require(self._try_shell(["am", "force-stop", package]), "force_stop")

    def launch_app(self, package: str) -> CommandResult:
        """Start a package's main activity without knowing its component name."""
        result = self._try_shell(["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        if result is None or not result.ok:
            raise DeviceError(f"could not launch {package}: {(result.error_text if result else 'no response').strip()[:200]}")
        return result

    def set_orientation(self, value: str = "natural") -> CommandResult:
        return self._require(self._try_shell(["cmd", "user", "settings", "put", "secure", "user_rotation", {"natural": "0", "left": "1", "upsidedown": "2", "right": "3"}.get(value, "0")]), "set_orientation")

    # ---------------------------------------------------------- low level

    def shell_tokens(self, argv: Sequence[str]) -> CommandResult:
        """For guarded, policy-invisible reads (e.g. a policy wanting `dumpsys`)."""
        return self._require(self._try_shell(list(argv)), "shell")

    def invalidate(self) -> None:
        """Drop the hierarchy cache. Call after any dispatched mutation."""
        self._cached = None
        self._cached_at = 0.0

    def _try_shell(self, argv: Sequence[str]) -> CommandResult | None:
        try:
            return self.transport.shell(argv)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised by caller
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def _try_exec_out(self, argv: Sequence[str]) -> str:
        getter = getattr(self.transport, "exec_out_shell", None)
        if getter is None:
            result = self._try_shell(argv)
            return result.text if result else ""
        try:
            return getter(argv).text
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""

    def _require(self, result: CommandResult | None, what: str) -> CommandResult:
        if result is None:
            raise DeviceError(f"{what} failed: {self.last_error or 'transport error'}")
        if not result.ok and "Warning" not in result.error_text:
            raise DeviceError(f"{what} failed (exit {result.returncode}): {result.error_text.strip()[:200]}")
        return result


def _extract_window_token(text: str, key: str) -> str:
    """Pull ``package/activity`` out of a ``dumpsys`` line naming ``key``.

    Prefers the token *inside* the braces; falls back to a ``cmp=`` field, which
    is how ``dumpsys activity activities`` writes it on some Android 12+ builds.
    """
    for line in text.splitlines():
        if key not in line:
            continue
        brace = re.search(r"\{([^}]*)\}?", line)
        if brace:
            tokens = brace.group(1).split()
            for token in reversed(tokens):
                cleaned = token.strip("}")
                if "/" in cleaned:
                    package, _, activity = cleaned.partition("/")
                    if package and activity:
                        return f"{package}/{activity}"
        match = re.search(r"cmp=(\S+/\S+)", line)
        if match:
            return match.group(1)
    return ""


def _sanitize_input_text(text: str) -> str | None:
    """Return ``text`` usable by ``input text``, or ``None`` if it cannot be."""
    if not text:
        return ""
    if any(ord(ch) > 126 for ch in text):
        return None
    # Drop bytes AOSP's input reader rejects outright.
    return "".join(ch for ch in text if ch == " " or not ch.isspace() or ch == " ")


def _strip_dump_trailer(text: str) -> str:
    """Remove ``UI hierchary dumped to: ...`` from a /dev/tty dump."""
    match = re.search(r"</hierarchy>", text)
    return text[: match.end()] if match else text


def _compress(text: str, limit: int = 140) -> str:
    joined = " ".join((text or "").split())
    return joined[:limit]
