"""Policies: the decision half of the loop, behind a two-method protocol.

The engine never imports a model client and never knows what "intelligence" is.
A policy receives an :class:`Observation` -- the digest, the diff since last
step, the recent action history, the remaining budget -- and returns one
:class:`Action`. That is the entire contract.

The three concrete policies here are each useful for a different reason:

:class:`NullPolicy`
    Observe-only. The right default, and it means ``cloudultron run`` out of the
    box is a screen monitor that cannot change anything.
:class:`ScriptedPolicy`
    Deterministic action list from a file. Lets you regression-test the *engine*
    (dry-run gating, loop detection, trace format) without any model involved,
    and doubles as a macro recorder's playback half.
:class:`ExplorePolicy`
    A greedy novelty-seeking heuristic with no model at all: click the first
    interactable element it has not already exercised, back out when the screen
    changes in a way that looks like a new screen, stop when the queue is empty.
    This is what makes ``--mock --policy explore`` a real demonstration, and it
    is also the fallback an LLM policy wants when the model call fails.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from ..errors import PolicyError
from ..safety import Effect
from ..ui.model import Screen
from .actions import Action, Op


@dataclass
class HistoryEntry:
    """What happened on one past step, as much as a policy should be told."""

    step: int
    action: str
    outcome: str
    structure_hash: str
    effect: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "action": self.action, "outcome": self.outcome, "effect": self.effect}


@dataclass
class Observation:
    """Everything the policy is allowed to know.

    Keeping this an explicit dataclass rather than "here's the whole engine" is
    what makes it feasible to swap in a remote model later: the payload is
    bounded and serialisable.
    """

    step: int
    screen: Screen
    digest: str
    diff_summary: str
    diff_level: str
    focused_window: str
    history: Sequence[HistoryEntry] = ()
    steps_remaining: int = 0
    hints: Sequence[str] = ()
    #: Screen structure/content fingerprints, for policies that want to key state.
    structure_hash: str = ""
    content_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        """JSON-safe view, ready to hand to an LLM-backed policy."""
        return {
            "step": self.step,
            "focused_window": self.focused_window,
            "package": self.screen.package,
            "change": self.diff_summary,
            "change_level": self.diff_level,
            "steps_remaining": self.steps_remaining,
            "hints": list(self.hints),
            "recent_actions": [h.to_dict() for h in self.history[-6:]],
            "screen": self.digest,
        }


class Policy(Protocol):
    """The decision interface."""

    name: str

    def decide(self, observation: Observation) -> Action: ...

    def note_outcome(self, action: Action, outcome: str) -> None:  # pragma: no cover - optional
        """Optional feedback so stateful policies can learn within a run."""
        ...


@dataclass
class NullPolicy:
    """Never acts. The loop observes until ``max_steps`` and stops cleanly.

    Useful beyond a demo: it is the way to run the harness against a live device
    purely as a *screen change monitor*, which is the cheapest way to prove your
    diffing works before any mutation is on the table.
    """

    name: str = "null"
    rationale: str = "observe only"

    def decide(self, observation: Observation) -> Action:
        # Deliberately never self-terminates. Guessing "one step left, I should
        # say done" would put budget policy in two places and make a budget-limited
        # run report DONE instead of STEP_BUDGET, which is a lie in the trace.
        return Action.noop(self.rationale)


@dataclass
class ScriptedPolicy:
    """Replay a fixed list of actions, then declare done.

    Loaded from JSON (``{"actions": [{"op": "tap_index", "args": {...}}]}``) or
    a plain newline list (``tap 3``, ``back``, ``text hello``), because when you
    are hand-writing a repro you do not want to type JSON.
    """

    actions: list[Action]
    name: str = "scripted"
    cursor: int = 0

    @classmethod
    def from_file(cls, path: str | pathlib.Path) -> "ScriptedPolicy":
        raw = pathlib.Path(path).read_text(encoding="utf-8")
        return cls.from_text(raw)

    @classmethod
    def from_text(cls, raw: str) -> "ScriptedPolicy":
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return cls.from_json(json.loads(stripped))
        return cls(actions=[parse_line(line) for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")])

    @classmethod
    def from_json(cls, payload: Any) -> "ScriptedPolicy":
        items = payload.get("actions") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise PolicyError("script JSON must be {'actions': [...]} or [...]")
        actions: list[Action] = []
        for item in items:
            if isinstance(item, str):
                actions.append(parse_line(item))
                continue
            try:
                op = Op(str(item["op"]))
            except KeyError as exc:
                raise PolicyError(f"scripted action missing {exc}") from None
            except ValueError as exc:
                raise PolicyError(f"unknown op {item.get('op')!r}") from exc
            actions.append(Action(op, dict(item.get("args") or {}), str(item.get("rationale", "")), source="script"))
        return cls(actions=actions)

    def decide(self, observation: Observation) -> Action:
        while self.cursor < len(self.actions):
            action = self.actions[self.cursor]
            self.cursor += 1
            if action.op is not Op.NOOP:
                return action
        return Action.done("script finished")

    def note_outcome(self, action: Action, outcome: str) -> None:
        return None


@dataclass
class ExplorePolicy:
    """Greedy novelty search over the interactable set. No model required.

    Strategy per step:
      1. If a dialog-looking thing is up, dismiss it (buttons labelled OK/Allow/
         Skip) -- otherwise every subsequent screen is the same permission sheet.
      2. If we are deep in an unknown screen with nothing untried, go back.
      3. Otherwise tap the first element not yet tried on a structurally similar
         screen, or scroll if we have run out and the view scrolls.

    The point is not that it is clever. It is that it produces *real* loop
    traffic -- taps, screen changes, back-outs, an eventual stop -- so the
    engine, the diffing, and the anti-loop trip can all be exercised end to end
    with zero dependencies and zero nondeterminism.
    """

    name: str = "explore"
    max_depth: int = 4
    allow_scroll: bool = True
    _tried: set[str] = field(default_factory=set, repr=False)
    _depth: int = field(default=0, repr=False)
    _scroll_attempts: int = field(default=0, repr=False)

    DISMISS_LABELS = ("ok", "allow", "got it", "skip", "close", "accept", "dismiss", "no thanks", "not now", "continue")
    DANGER_LABELS = ("delete", "remove", "uninstall", "factory reset", "erase", "power off", "reboot", "sell", "buy", "pay", "send", "purchase")

    def _looks_dangerous(self, label: str) -> bool:
        """Substring match on purpose, and only for the danger list.

        "Delete account" must not be considered tappable just because the button
        says something longer than "delete". The asymmetry with DISMISS_LABELS
        (which is matched exactly) is deliberate: guessing wrong about a dismiss
        button wastes a step, guessing wrong about a destructive one spends an
        account. So danger is matched loosely and dismissal tightly.
        """
        return any(word in label for word in self.DANGER_LABELS)

    def decide(self, observation: Observation) -> Action:
        from ..ui.render import index_screen  # local import: keeps module import order flat

        screen = observation.screen
        indexed = index_screen(screen, max_count=max(10, screen.node_count))

        def key_of(item: Any) -> str:
            # slot_key, not identity_key: sibling rows share a resource id, and
            # using it alone would mark the whole list as visited on the first tap.
            return item.node.slot_key()

        def tried(item: Any) -> bool:
            return key_of(item) in self._tried

        # 1. permission/consent dialogs first, and never a destructive-labelled one.
        for item in indexed:
            label = item.node.label.strip().lower()
            if self._looks_dangerous(label) or label not in self.DISMISS_LABELS or tried(item):
                continue
            self._tried.add(key_of(item))
            return Action.tap(item.index, f"dismiss dialog via {label!r}")

        # 2. nothing new here: back out, or finish.
        fresh = [item for item in indexed if not tried(item)]
        if not fresh:
            # A scroll that produced no change is not a scroll to repeat. This is
            # the policy obeying the same rule the executor enforces from
            # outside -- and obeying it internally is cheaper, because it costs a
            # step less. `diff_level == "none"` is exactly the signal that the
            # previous action did nothing.
            last = observation.history[-1] if observation.history else None
            futile_scroll = bool(last and last.action.startswith("scroll") and observation.diff_level == "none")
            if not futile_scroll and self._scroll_attempts < 3 and self.allow_scroll and any(n.scrollable for n in screen.walk()):
                self._scroll_attempts += 1
                return Action.scroll("down", "no untried elements on screen; scrolling")
            self._scroll_attempts = 0
            if self._depth > 0:
                self._depth = max(0, self._depth - 1)
                return Action.back("screen exhausted; going back")
            return Action.done("explored every reachable screen in budget")

        # 3. prefer shallow, on-screen, enabled, non-destructive targets.
        scored = []
        for item in fresh:
            node = item.node
            label = node.label.strip().lower()
            if self._looks_dangerous(label) or not node.enabled:
                continue
            score = node.bounds.y1 / 1000.0 + node.depth / 100.0
            if node.bounds.area and node.bounds.height < 24:
                score += 5  # tiny targets are usually decoration
            if any(word in label for word in ("settings", "profile", "account", "log", "help")):
                score -= 1  # mildly prefer content over chrome
            scored.append((score, item))
        if not scored:
            if self._depth > 0:
                self._depth -= 1
                return Action.back("every remaining candidate looks destructive; going back")
            return Action.done("only destructive-looking targets remained")
        scored.sort(key=lambda pair: pair[0])
        chosen = scored[0][1]
        self._tried.add(key_of(chosen))
        if chosen.node.bounds.area > 0.35 * max(1, screen.window_size[0] * screen.window_size[1]):
            self._depth += 1
        return Action.tap(chosen.index, f"explore {chosen.node.label!r}")

    def note_outcome(self, action: Action, outcome: str) -> None:
        # A tap that produced a structural change is "we navigated somewhere".
        if action.op is Op.TAP_INDEX and "structural" in outcome:
            return None
        return None


# ---------------------------------------------------------------- utilities


def parse_line(line: str) -> Action:
    """Parse one scripted step. Grammar is deliberately tiny.

    ``tap 3`` / ``back`` / ``home`` / ``scroll down`` / ``text hello world`` /
    ``keyevent 66`` / ``launch com.android.settings`` / ``start pkg/.Main`` /
    ``wait 2`` / ``point 300 400`` / ``done``
    """
    text = line.strip()
    if not text:
        raise PolicyError("empty scripted action")
    verb, _, rest = text.partition(" ")
    rest = rest.strip()
    table: dict[str, Any] = {
        "noop": lambda: Action.noop("script"),
        "back": lambda: Action.back("script"),
        "home": lambda: Action.home("script"),
        "done": lambda: Action.done("script complete"),
        "tap": lambda: Action.tap(int(rest), "script"),
        "point": lambda: Action.tap_point(int(rest.split()[0]), int(rest.split()[1]), "script"),
        "longpress": lambda: Action.long_press(int(rest), 700, "script"),
        "text": lambda: Action.text(rest, "script"),
        "keyevent": lambda: Action.keyevent(int(rest) if rest.isdigit() else rest, "script"),
        "scroll": lambda: Action.scroll(rest or "down", "script"),
        "wait": lambda: Action.wait(float(rest or 1), "script"),
        "launch": lambda: Action.launch_app(rest, "script"),
        "start": lambda: Action.start_activity(rest, "script"),
        "url": lambda: Action.open_url(rest, "script"),
        "swipe": lambda: Action.swipe(*(int(v) for v in rest.split()[:4]), rationale="script"),
        "shell": lambda: Action.raw_shell(rest, "script"),
    }
    builder = table.get(verb.lower())
    if builder is None:
        raise PolicyError(f"unknown scripted verb {verb!r} (line: {text!r})")
    try:
        return builder()
    except (ValueError, IndexError) as exc:
        raise PolicyError(f"cannot parse scripted line {text!r}: {exc}") from exc


def actions_from_iterable(items: Iterable[Action | str]) -> list[Action]:
    out: list[Action] = []
    for item in items:
        out.append(item if isinstance(item, Action) else parse_line(item))
    return out


def effect_of(action: Action) -> str:
    return str(Effect.READ if not action.is_mutation else Effect.WRITE)
