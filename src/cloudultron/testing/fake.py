"""A fake that speaks adb, so the stack can be tested without a phone.

The choice that matters
----------------------
The fake implements the *transport* protocol, not the device protocol. That means
:mod:`cloudultron.adb.device` really runs: it issues ``uiautomator dump``,
decides to ``cat`` the file back, parses the answer, handles the fallbacks. If we
mocked at the device level we would be testing a shape we invented; mocking one
level lower means the tests fail when the real code path breaks, which is the
only kind of test worth writing.

Three screens, one deliberate trap
----------------------------------
``launcher`` -> ``home`` -> ``settings`` are ordinary. ``home`` also contains an
element labelled "Buy now" that leads nowhere, so the default
``dry_run``/explore path has something that *does not respond* -- which is what
makes the stagnation tripwire demonstrable rather than merely unit-tested.
"""

from __future__ import annotations

import re
import shlex
import xml.sax.saxutils as saxutils
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Element:
    """One node in a fake screen, rendered to uiautomator XML on demand."""

    cls: str = "android.widget.TextView"
    resource_id: str = ""
    text: str = ""
    desc: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    enabled: bool = True
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    focused: bool = False
    password: bool = False
    package: str = ""
    children: list["Element"] = field(default_factory=list)
    #: Screen name to jump to when this element is tapped. ``None`` = no-op,
    #: which is exactly the broken-app behaviour the loop must notice.
    taps_to: str | None = None
    #: Extra literal XML to emit instead of generating attributes (for tests
    #: that need malformed or exotic dumps).
    raw: str = ""

    def to_xml(self, index: int = 0, depth: int = 0) -> str:
        if self.raw:
            return self.raw
        pad = "  " * depth
        x1, y1, x2, y2 = self.bounds
        attrs = {
            "index": str(index),
            "text": self.text,
            "resource-id": self.resource_id,
            "class": self.cls,
            "package": self.package,
            "content-desc": self.desc,
            "checkable": _b(self.checkable),
            "checked": _b(self.checked),
            "clickable": _b(self.clickable),
            "enabled": _b(self.enabled),
            "focusable": _b(self.focused or self.clickable),
            "focused": _b(self.focused),
            "scrollable": _b(self.scrollable),
            "long-clickable": _b(False),
            "password": _b(self.password),
            "selected": _b(False),
            "bounds": f"[{x1},{y1}][{x2},{y2}]",
        }
        rendered = " ".join(f'{k}="{saxutils.escape(v)}"' for k, v in attrs.items())
        head = f"{pad}<node {rendered}>"
        if not self.children:
            return f"{pad}<node {rendered}/>"
        body = "\n".join(child.to_xml(i, depth + 1) for i, child in enumerate(self.children))
        return f"{head}\n{body}\n{pad}</node>"


def _b(value: bool) -> str:
    return "true" if value else "false"


@dataclass
class FakeScreen:
    """A screen: its window identity and its node list."""

    name: str
    package: str = "com.example.app"
    activity: str = "MainActivity"
    width: int = 1080
    height: int = 1920
    elements: list[Element] = field(default_factory=list)
    #: Rotating status-bar text, to prove volatile changes are not transitions.
    clock: str = ""
    clock_sequence: list[str] = field(default_factory=list)
    _tick: int = field(default=0, init=False, repr=False)

    def to_dump(self) -> str:
        clock = self.clock
        if self.clock_sequence:
            clock = self.clock_sequence[self._tick % len(self.clock_sequence)]
            self._tick += 1
        status = Element(
            cls="android.widget.TextView",
            resource_id="com.example.app:id/clock",
            text=clock,
            bounds=(40, 20, 160, 60),
            package="com.android.systemui",
        )
        body_elements = [status, *self.elements]
        children = "\n".join(el.to_xml(i, 2) for i, el in enumerate(body_elements))
        root_attrs = f'resource-id="" class="android.widget.FrameLayout" package="{self.package}" bounds="[0,0][{self.width},{self.height}]"'
        return (
            '<?xml version=\'1.0\' encoding=\'UTF-8\'?>\n'
            f'<hierarchy rotation="0">\n'
            f'  <node {root_attrs}>\n'
            f"{children}\n"
            f"  </node>\n"
            f"</hierarchy>"
        )

    def advance_clock(self) -> None:
        """Force the next dump to differ in text only (tests volatile diffs)."""
        self._tick += 1

    def find_at(self, x: int, y: int) -> Element | None:
        """The deepest clickable element containing a point."""
        hits: list[Element] = []

        def visit(nodes: Iterable[Element], depth: int = 0) -> None:
            for node in nodes:
                x1, y1, x2, y2 = node.bounds
                if x1 <= x < x2 and y1 <= y < y2:
                    if node.clickable:
                        hits.append((depth, node))  # type: ignore[arg-type]
                    visit(node.children, depth + 1)

        visit(self.elements)
        if not hits:
            return None
        hits.sort(key=lambda pair: -pair[0])  # deepest wins
        return hits[0][1]


def build_demo_device(width: int = 1080, height: int = 1920) -> dict[str, FakeScreen]:
    """A small but representative app graph for demos and tests."""

    def screen(name: str, package: str, activity: str, elements: list[Element]) -> FakeScreen:
        return FakeScreen(name=name, package=package, activity=activity, width=width, height=height, elements=elements, clock="9:41")

    launcher = screen(
        "launcher",
        "com.android.launcher",
        "LauncherActivity",
        [
            Element(
                cls="android.widget.LinearLayout",
                bounds=(0, 200, width, height - 200),
                scrollable=True,
                package="com.android.launcher",
                children=[
                    Element(
                        cls="android.widget.TextView",
                        resource_id="com.android.launcher:id/icon",
                        text="Settings",
                        desc="Settings",
                        bounds=(60, 300, 300, 560),
                        clickable=True,
                        taps_to="settings",
                        package="com.android.launcher",
                    ),
                    Element(
                        cls="android.widget.TextView",
                        resource_id="com.android.launcher:id/icon",
                        text="ExampleApp",
                        desc="ExampleApp",
                        bounds=(400, 300, 640, 560),
                        clickable=True,
                        taps_to="home",
                        package="com.android.launcher",
                    ),
                ],
            )
        ],
    )

    home = screen(
        "home",
        "com.example.app",
        "MainActivity",
        [
            Element(
                cls="android.widget.TextView",
                resource_id="com.example.app:id/title",
                text="Welcome to ExampleApp",
                bounds=(60, 120, width - 60, 200),
                package="com.example.app",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="com.example.app:id/btn_continue",
                text="Continue",
                bounds=(60, 900, width - 60, 1030),
                clickable=True,
                taps_to="detail",
                package="com.example.app",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="com.example.app:id/btn_settings",
                text="Open settings",
                bounds=(60, 1060, width - 60, 1190),
                clickable=True,
                taps_to="settings",
                package="com.example.app",
            ),
            # The trap: enabled and clickable, but leads nowhere.
            Element(
                cls="android.widget.Button",
                resource_id="com.example.app:id/buy",
                text="Buy now",
                bounds=(60, 1220, width - 60, 1350),
                clickable=True,
                taps_to=None,
                package="com.example.app",
            ),
        ],
    )

    detail = screen(
        "detail",
        "com.example.app",
        "DetailActivity",
        [
            Element(
                cls="android.widget.EditText",
                resource_id="com.example.app:id/input_name",
                text="",
                desc="Your name",
                bounds=(60, 300, width - 60, 420),
                clickable=True,
                focused=True,
                package="com.example.app",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="com.example.app:id/btn_submit",
                text="Submit",
                bounds=(60, 500, width - 60, 620),
                clickable=True,
                taps_to="confirm",
                package="com.example.app",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="com.example.app:id/btn_back",
                text="Back",
                bounds=(60, 650, width - 60, 770),
                clickable=True,
                taps_to="home",
                package="com.example.app",
            ),
        ],
    )

    confirm = screen(
        "confirm",
        "com.example.app",
        "ConfirmActivity",
        [
            Element(
                cls="android.widget.TextView",
                resource_id="com.example.app:id/msg",
                text="Thanks! Your submission was received.",
                bounds=(60, 700, width - 60, 800),
                package="com.example.app",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="android:id/button1",
                text="OK",
                bounds=(width - 400, 900, width - 80, 1000),
                clickable=True,
                taps_to="home",
                package="com.example.app",
            ),
        ],
    )

    settings = screen(
        "settings",
        "com.android.settings",
        "Settings",
        [
            Element(
                cls="android.widget.Switch",
                resource_id="com.android.settings:id/switch_wifi",
                text="Wi-Fi",
                bounds=(60, 300, width - 60, 440),
                clickable=True,
                checkable=True,
                package="com.android.settings",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="com.example.app:id/buy",
                text="Buy now",
                bounds=(60, 1500, width - 60, 1630),
                clickable=True,
                taps_to=None,
                package="com.android.settings",
            ),
        ],
    )

    return {name: scr for name, scr in ((s.name, s) for s in (launcher, home, detail, confirm, settings))}


class FakeTransport:
    """Answers adb invocations by mutating a fake screen graph.

    Understands only the handful of device-side verbs the harness emits, and
    raises loudly on anything else -- an unrecognised verb in a test should be a
    failure, not a silently empty result.
    """

    #: Verbs we will serve. Anything else is a bug in the caller.
    # Includes the routine lifecycle verbs so a test-lab run can be observed
    # actually reaching the device rather than being swallowed by the fake. `rm`
    # is deliberately absent: a delete that ever arrives here is news, and the
    # AssertionError below is the tripwire that makes it loud.
    KNOWN = ("uiautomator", "cat", "input", "am", "dumpsys", "wm", "getprop", "monkey", "pm", "ime", "settings", "cmd", "reboot", "shutdown", "chmod", "chown", "svc")

    def __init__(
        self,
        screens: dict[str, FakeScreen] | None = None,
        *,
        start: str = "launcher",
        dump_path: str = "/sdcard/window_dump.xml",
    ) -> None:
        self.screens = screens if screens is not None else build_demo_device()
        for name, screen in self.screens.items():
            screen.name = name
        self.current = start
        self.history: list[str] = []
        self.dump_path = dump_path
        self.staged: str | None = None  # what `uiautomator dump <path>` wrote
        # Device-side filesystem. Must be per-instance: a class-level dict would
        # be shared by every FakeTransport in the process and leak across tests.
        self._files: dict[str, str] = {}
        self.calls: list[str] = []
        self.commands: list[list[str]] = []
        #: Toggle to simulate an unreadable screen (FLAG_SECURE / wedged dump).
        self.dump_fails = False
        #: Toggle to simulate a slow device timing out.
        self.shell_fails = False
        #: Every input event seen, for assertions in tests.
        self.events: list[tuple[str, ...]] = []
        self.field_text: dict[str, str] = {}

    # ------------------------------------------------------- transport api

    class _Result:
        def __init__(self, argv, returncode, stdout, stderr, duration_ms=1, attempts=1):
            self.argv = tuple(argv)
            self.returncode = returncode
            self.stdout = stdout.encode("utf-8")
            self.stderr = stderr.encode("utf-8")
            self.duration_ms = duration_ms
            self.attempts = attempts

        @property
        def ok(self) -> bool:
            return self.returncode == 0

        @property
        def text(self) -> str:
            return self.stdout.decode("utf-8")

        @property
        def error_text(self) -> str:
            return self.stderr.decode("utf-8")

        def raise_for_status(self):
            return self

    def run(self, argv, *, timeout: float | None = None) -> "FakeTransport._Result":
        argv = list(argv)
        verb = argv[0] if argv else ""
        if verb in ("version",):
            return self._Result(argv, 0, "Android Debug Bridge version 1.0.41 (fake)\n", "")
        if verb in ("devices",):
            return self._Result(argv, 0, "List of devices attached\nfake-device\tdevice\n", "")
        if verb in ("connect",):
            return self._Result(argv, 0, f"connected to {argv[1] if len(argv) > 1 else '?'}\n", "")
        if verb in ("shell", "exec-out"):
            # adb-level passthrough used by exec_out_shell fallbacks.
            return self.shell_string(argv[1] if len(argv) > 1 else "", timeout=timeout)
        return self._Result(argv, 0, "", "")

    def shell(self, argv, *, timeout: float | None = None) -> "FakeTransport._Result":
        argv = list(argv)
        # The real transport passes a single already-joined string; accept both.
        if len(argv) == 1 and isinstance(argv[0], str) and " " in argv[0]:
            return self.shell_string(argv[0], timeout=timeout)
        return self._dispatch([str(a) for a in argv])

    def shell_string(self, command: str, *, timeout: float | None = None) -> "FakeTransport._Result":
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return self._Result([command], 1, "", f"sh: parse error: {exc}")
        return self._dispatch(argv)

    def shell_raw(self, command: str, *, timeout: float | None = None) -> "FakeTransport._Result":
        """Parity with AdbTransport: the operator-authorized raw path needs this."""
        return self.shell_string(command, timeout=timeout)

    # ---------------------------------------------------------- simulation

    def _dispatch(self, argv: list[str]) -> "FakeTransport._Result":
        self.calls.append(" ".join(argv))
        if self.shell_fails:
            return self._Result(argv, 1, "", "error: device offline")
        if not argv:
            return self._Result(argv, 1, "", "usage: adb shell <command>")
        verb = argv[0]
        if verb not in self.KNOWN:
            raise AssertionError(f"FakeTransport received an unexpected device command: {' '.join(argv)!r}")
        handler = getattr(self, f"_on_{verb}", None)
        if handler is None:
            return self._Result(argv, 0, "", "")
        return handler(argv)

    def _on_uiautomator(self, argv: list[str]) -> "FakeTransport._Result":
        if self.dump_fails:
            return self._Result(argv, 0, "ERROR: could not get idle state.\n", "")
        self.staged = self.screens[self.current].to_dump()
        target = argv[2] if len(argv) > 2 else self.dump_path
        if target == "/dev/tty":
            return self._Result(argv, 0, self.staged + "\nUI hierchary dumped to: /dev/tty\n", "")
        self._files[target] = self.staged
        return self._Result(argv, 0, f"UI hierchary dumped to: {target}\n", "")

    def _on_cat(self, argv: list[str]) -> "FakeTransport._Result":
        path = argv[1] if len(argv) > 1 else self.dump_path
        body = self._files.get(path, getattr(self, "staged", None) or "")
        if not body:
            return self._Result(argv, 1, "", f"cat: {path}: No such file or directory")
        return self._Result(argv, 0, body, "")

    def _on_input(self, argv: list[str]) -> "FakeTransport._Result":
        kind = argv[1] if len(argv) > 1 else ""
        screen = self.screens[self.current]
        self.events.append(tuple(argv[1:]))
        if kind == "tap" and len(argv) >= 4:
            x, y = int(argv[2]), int(argv[3])
            target = screen.find_at(x, y)
            if target is not None:
                if target.resource_id.endswith("switch_wifi"):
                    # A switch toggles in place: content changes, wireframe does
                    # not. This is the case a naive "did anything change?" hash
                    # over-reads and a naive structure hash under-reads.
                    target.checked = not target.checked
                    return self._Result(argv, 0, "", "")
                if target.taps_to:
                    self._goto(target.taps_to)
                # else: clickable but dead -- deliberately no state change.
            return self._Result(argv, 0, "", "")
        if kind == "text" and len(argv) >= 3:
            value = argv[2].replace("%s", " ")
            focused = next((el for el in _walk(screen.elements) if el.focused), None)
            if focused is not None:
                self.field_text[focused.resource_id] = self.field_text.get(focused.resource_id, "") + value
                focused.text = self.field_text[focused.resource_id]
            return self._Result(argv, 0, "", "")
        if kind == "keyevent" and len(argv) >= 3:
            code = argv[2]
            if code in ("4", "KEYCODE_BACK"):
                if self.history:
                    self.current = self.history.pop()
                return self._Result(argv, 0, "", "")
            if code in ("3", "KEYCODE_HOME"):
                self._goto("launcher")
                self.history.clear()
            return self._Result(argv, 0, "", "")
        if kind == "swipe" and len(argv) >= 6:
            # A swipe down the middle of the launcher "scrolls" the icon row: we
            # model that as revealing an extra icon, i.e. a structural change.
            if self.current == "launcher":
                if not any(getattr(el, "revealed", False) for el in _walk(screen.elements)):
                    screen.elements.append(
                        Element(
                            cls="android.widget.TextView",
                            resource_id="com.android.launcher:id/icon",
                            text="Camera",
                            desc="Camera",
                            bounds=(740, 300, 980, 560),
                            clickable=True,
                            taps_to="home",
                            package="com.android.launcher",
                        )
                    )
            return self._Result(argv, 0, "", "")
        return self._Result(argv, 0, "", "")

    def _on_am(self, argv: list[str]) -> "FakeTransport._Result":
        if len(argv) > 1 and argv[1] == "start":
            joined = " ".join(argv)
            match = re.search(r"-n\s+([\w.]+)/([\w.$]+)", joined)
            if match:
                package, activity = match.group(1), match.group(2)
                if activity.startswith("."):
                    activity = package + activity
                for name, screen in self.screens.items():
                    if screen.package != package:
                        continue
                    # `am start pkg/.Act` resolves to `pkg/pkg.Act` on a real
                    # device, so accept the short and the qualified spelling.
                    if activity in (screen.activity, f"{package}.{screen.activity}"):
                        self._goto(name)
                        return self._Result(argv, 0, "Starting: Intent { cmp=... }\n", "")
                return self._Result(argv, 1, "", "Error: Activity class does not exist.")
            if "-d" in argv:
                self._goto("home")
                return self._Result(argv, 0, "", "")
        if len(argv) > 1 and argv[1] == "force-stop":
            return self._Result(argv, 0, "", "")
        return self._Result(argv, 0, "", "")

    def _on_dumpsys(self, argv: list[str]) -> "FakeTransport._Result":
        screen = self.screens[self.current]
        # Literal braces around the window token, so they are concatenated rather
        # than interpolated -- an f-string would need them doubled.
        return self._Result(
            argv,
            0,
            "WINDOW SERVICE dump\n  mCurrentFocus=Window{1a2b3c4 u0 "
            + f"{screen.package}/{screen.package}.{screen.activity}"
            + "}\n",
            "",
        )

    def _on_wm(self, argv: list[str]) -> "FakeTransport._Result":
        screen = self.screens[self.current]
        return self._Result(argv, 0, f"Physical size: {screen.width}x{screen.height}\n", "")

    def _on_getprop(self, argv: list[str]) -> "FakeTransport._Result":
        return self._Result(argv, 0, "1\n", "")

    def _on_monkey(self, argv: list[str]) -> "FakeTransport._Result":
        if "-p" in argv:
            package = argv[argv.index("-p") + 1]
            for name, screen in self.screens.items():
                if screen.package == package:
                    self._goto(name)
                    return self._Result(argv, 0, "Monkey finished\n", "")
            return self._Result(argv, 1, "", f"No launcher activity for {package}")
        return self._Result(argv, 0, "", "")

    def _on_pm(self, argv: list[str]) -> "FakeTransport._Result":
        if len(argv) > 1 and argv[1] in ("list", "path"):
            names = sorted({s.package for s in self.screens.values()})
            return self._Result(argv, 0, "".join(f"package:{n}\n" for n in names), "")
        return self._Result(argv, 0, "", "")

    def _on_cmd(self, argv: list[str]) -> "FakeTransport._Result":
        return self._Result(argv, 0, "", "")

    def _on_ime(self, argv: list[str]) -> "FakeTransport._Result":
        return self._Result(argv, 0, "", "")

    def _on_settings(self, argv: list[str]) -> "FakeTransport._Result":
        return self._Result(argv, 0, "", "")

    # ------------------------------------------------------------ internals

    def _goto(self, name: str) -> None:
        if name not in self.screens:
            raise AssertionError(f"fake screen {name!r} does not exist")
        self.history.append(self.current)
        self.current = name
        self.staged = None  # the old dump must not be readable after navigation
        self._files.clear()

    # ------------------------------------------------------- test helpers

    def tap_by_label(self, label: str) -> None:
        """Drive a click through the same coordinates the parser would produce."""
        screen = self.screens[self.current]
        for element in _walk(screen.elements):
            if element.clickable and label in (element.text, element.desc, element.resource_id):
                x1, y1, x2, y2 = element.bounds
                self.shell(["input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2)])
                return
        raise AssertionError(f"no clickable {label!r} on {screen.name}")

    def screen_name(self) -> str:
        return self.current


def _walk(elements: list[Element]):
    for element in elements:
        yield element
        yield from _walk(element.children)


def demo_screens() -> dict[str, FakeScreen]:
    """Public accessor so the CLI and tests build identical graphs."""
    return build_demo_device()
