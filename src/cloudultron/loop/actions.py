"""The action vocabulary the policy may choose from.

This is the capability boundary of the whole harness. A policy can express
exactly these things and nothing else, which is what lets
:mod:`cloudultron.safety` be short: you do not need to filter an
:class:`Action.Tap` for a ``rm -rf`` in it.

``TapIndex`` vs ``TapPoint`` is not redundancy. Index addressing is the
default because it makes a decision *explainable after the fact* ("tapped the
node whose id was btn_send") and bounds a bad decision; raw coordinates exist
for the cases a tree genuinely cannot express -- a canvas, a game, a custom
View with no children -- and are tagged ``unanchored`` in the trace so they are
easy to audit later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..errors import PolicyError
from ..safety import Effect
from ..ui.model import Screen


class Op(str, Enum):
    """Verb set. Kept as strings so trace JSON stays diffable."""

    NOOP = "noop"
    TAP_INDEX = "tap_index"
    TAP_POINT = "tap_point"
    LONG_PRESS_INDEX = "long_press_index"
    SWIPE = "swipe"
    SCROLL = "scroll"
    TEXT = "text"
    KEYEVENT = "keyevent"
    BACK = "back"
    HOME = "home"
    LAUNCH_APP = "launch_app"
    START_ACTIVITY = "start_activity"
    OPEN_URL = "open_url"
    WAIT = "wait"
    DONE = "done"
    ABORT = "abort"
    RAW_SHELL = "raw_shell"  # only honoured when the guard permits it


@dataclass(frozen=True)
class Action:
    """One intended step. Immutable: it is also the trace record."""

    op: Op
    args: Mapping[str, Any] = field(default_factory=dict)
    #: The decision-maker's own justification, recorded verbatim. Not used for
    #: control flow -- an LLM explaining itself is not a safety property.
    rationale: str = ""
    #: Origin tag for the trace ("scripted", "explore", "llm", "fallback").
    source: str = "policy"

    # ------------------------------------------------------------ factories

    @staticmethod
    def noop(rationale: str = "") -> "Action":
        return Action(Op.NOOP, {}, rationale)

    @staticmethod
    def tap(index: int, rationale: str = "") -> "Action":
        return Action(Op.TAP_INDEX, {"index": int(index)}, rationale)

    @staticmethod
    def tap_point(x: int, y: int, rationale: str = "") -> "Action":
        return Action(Op.TAP_POINT, {"x": int(x), "y": int(y)}, rationale)

    @staticmethod
    def long_press(index: int, duration_ms: int = 700, rationale: str = "") -> "Action":
        return Action(Op.LONG_PRESS_INDEX, {"index": int(index), "duration_ms": int(duration_ms)}, rationale)

    @staticmethod
    def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, rationale: str = "") -> "Action":
        return Action(Op.SWIPE, {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms}, rationale)

    @staticmethod
    def scroll(direction: str = "down", rationale: str = "") -> "Action":
        if direction not in {"up", "down", "left", "right"}:
            raise PolicyError(f"unknown scroll direction {direction!r}")
        return Action(Op.SCROLL, {"direction": direction}, rationale)

    @staticmethod
    def text(value: str, rationale: str = "") -> "Action":
        return Action(Op.TEXT, {"value": value}, rationale)

    @staticmethod
    def keyevent(code: int | str, rationale: str = "") -> "Action":
        return Action(Op.KEYEVENT, {"code": code}, rationale)

    @staticmethod
    def back(rationale: str = "") -> "Action":
        return Action(Op.BACK, {}, rationale)

    @staticmethod
    def home(rationale: str = "") -> "Action":
        return Action(Op.HOME, {}, rationale)

    @staticmethod
    def launch_app(package: str, rationale: str = "") -> "Action":
        return Action(Op.LAUNCH_APP, {"package": package}, rationale)

    @staticmethod
    def start_activity(component: str, rationale: str = "") -> "Action":
        return Action(Op.START_ACTIVITY, {"component": component}, rationale)

    @staticmethod
    def open_url(url: str, rationale: str = "") -> "Action":
        return Action(Op.OPEN_URL, {"url": url}, rationale)

    @staticmethod
    def wait(seconds: float = 1.0, rationale: str = "") -> "Action":
        return Action(Op.WAIT, {"seconds": float(seconds)}, rationale)

    @staticmethod
    def done(rationale: str = "goal reached") -> "Action":
        return Action(Op.DONE, {}, rationale, source="terminal")

    @staticmethod
    def abort(rationale: str) -> "Action":
        return Action(Op.ABORT, {}, rationale, source="terminal")

    @staticmethod
    def raw_shell(command: str, rationale: str = "") -> "Action":
        """Escape hatch. Always routed through the guard, and blocked by default."""
        return Action(Op.RAW_SHELL, {"command": command}, rationale)

    # -------------------------------------------------------------- metadata

    @property
    def effect(self) -> Effect:
        """Nothing that only looks is dangerous; everything that touches is."""
        if self.op in (Op.NOOP, Op.WAIT, Op.DONE, Op.ABORT):
            return Effect.READ
        return Effect.WRITE

    @property
    def is_mutation(self) -> bool:
        return self.effect >= Effect.WRITE

    @property
    def is_terminal(self) -> bool:
        return self.op in (Op.DONE, Op.ABORT)

    def key(self) -> str:
        """Canonical identity for loop detection.

        Excludes ``rationale`` and ``source``: a policy that rewords the same
        decision every step must still be recognised as looping, which is the
        specific way an LLM-driven executor fails in practice.
        """
        parts = [self.op.value]
        for name in sorted(self.args):
            parts.append(f"{name}={self.args[name]}")
        return " ".join(parts)

    def describe(self) -> str:
        """Human one-liner for logs."""
        if not self.args:
            return self.op.value
        inner = " ".join(f"{k}={_short(v)}" for k, v in self.args.items())
        return f"{self.op.value}({inner})"

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op.value, "args": dict(self.args), "rationale": self.rationale, "source": self.source}


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Dispatcher:
    """Turns an :class:`Action` into device calls. The one writer of state.

    Index resolution happens here rather than in the policy, so an out-of-range
    index raises :class:`PolicyError` *before* any coordinate reaches the device
    and the step is recorded as a refusal instead of a mis-tap.
    """

    def __init__(self, device: Any, *, screen: Screen, indexed: list[Any], size: tuple[int, int]) -> None:
        self.device = device
        self.screen = screen
        self.indexed = {item.index: item for item in indexed}
        self.size = size

    def resolve_index(self, index: int) -> tuple[int, int]:
        item = self.indexed.get(int(index))
        if item is None:
            raise PolicyError(
                f"index {index} is not addressable on this screen "
                f"(valid: 0..{max(self.indexed) if self.indexed else -1})"
            )
        x, y = item.center
        if x <= 0 and y <= 0:
            raise PolicyError(f"index {index} has no usable bounds")
        return (x, y)

    def label_for(self, action: Action) -> str:
        """What the policy was aiming at, for the trace."""
        if action.op in (Op.TAP_INDEX, Op.LONG_PRESS_INDEX):
            item = self.indexed.get(int(action.args.get("index", -1)))
            if item is None:
                return "unresolved"
            node = item.node
            return node.id_short or node.label
        return ""

    # ------------------------------------------------------------------ run

    def dispatch(self, action: Action) -> str:
        """Execute ``action``. Returns a short description of what was done.

        Only called when the guard has allowed the action and dry-run is off, so
        every branch here is a real device call.
        """
        op = action.op
        args = action.args
        if op is Op.NOOP:
            return "no-op"
        if op is Op.WAIT:
            time_sleep(float(args.get("seconds", 1.0)))
            return f"waited {args.get('seconds', 1.0)}s"
        if op is Op.TAP_INDEX:
            x, y = self.resolve_index(args["index"])
            self.device.tap(x, y)
            return f"tap @({x},{y})"
        if op is Op.LONG_PRESS_INDEX:
            x, y = self.resolve_index(args["index"])
            self.device.long_press(x, y, int(args.get("duration_ms", 700)))
            return f"long_press @({x},{y})"
        if op is Op.TAP_POINT:
            x, y = int(args["x"]), int(args["y"])
            self._assert_in_view(x, y)
            self.device.tap(x, y)
            return f"tap @({x},{y}) [unanchored]"
        if op is Op.SWIPE:
            self.device.swipe(*(int(args[k]) for k in ("x1", "y1", "x2", "y2")), int(args.get("duration_ms", 300)))
            return "swipe"
        if op is Op.SCROLL:
            self._scroll(args.get("direction", "down"))
            return f"scroll {args.get('direction', 'down')}"
        if op is Op.TEXT:
            self.device.input_text(str(args.get("value", "")))
            return f"text {len(str(args.get('value', '')))} chars"
        if op is Op.KEYEVENT:
            self.device.keyevent(args["code"])
            return f"keyevent {args['code']}"
        if op is Op.BACK:
            self.device.press_back()
            return "back"
        if op is Op.HOME:
            self.device.press_home()
            return "home"
        if op is Op.LAUNCH_APP:
            self.device.launch_app(str(args["package"]))
            return f"launch {args['package']}"
        if op is Op.START_ACTIVITY:
            self.device.start_activity(str(args["component"]))
            return f"start {args['component']}"
        if op is Op.OPEN_URL:
            self.device.start_url(str(args["url"]))
            return f"open {args['url']}"
        if op is Op.RAW_SHELL:
            # Reaching here means the guard already allowed the string: the gate
            # in the engine refuses RAW_SHELL before dispatch in every profile
            # that forbids it. Run it verbatim -- re-tokenising would defeat the
            # point of a composed command.
            command = str(args.get("command", "")).strip()
            if not command:
                raise PolicyError("raw shell action carries no command")
            self.device.shell_raw(command)
            return f"raw shell: {command[:60]}"
        raise PolicyError(f"dispatcher has no handler for {op.value}")

    def _assert_in_view(self, x: int, y: int) -> None:
        width, height = self.size
        if width and height and not (0 <= x <= width and 0 <= y <= height):
            raise PolicyError(f"raw coordinate ({x},{y}) is outside {width}x{height}")

    def _scroll(self, direction: str) -> None:
        width, height = self.size
        if not (width and height):
            # Without a known size, a swipe would be built from zeros -- that is
            # a silent no-op, so fail loudly instead.
            raise PolicyError("cannot scroll: screen size unknown")
        cx, cy = width // 2, height // 2
        span = int(height * 0.32)
        if direction == "down":
            self.device.swipe(cx, cy + span, cx, cy - span, 420)
        elif direction == "up":
            self.device.swipe(cx, cy - span, cx, cy + span, 420)
        elif direction == "left":
            self.device.swipe(int(width * 0.8), cy, int(width * 0.2), cy, 420)
        else:
            self.device.swipe(int(width * 0.2), cy, int(width * 0.8), cy, 420)


def time_sleep(seconds: float) -> None:
    """Indirection so the fake device can make waiting free in tests."""
    import time

    time.sleep(max(0.0, min(seconds, 30.0)))
