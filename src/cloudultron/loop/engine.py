"""The executor: observe -> think -> act, with the tripwires wired in.

Ordering inside a step is not arbitrary, and this is the part a naive
implementation gets wrong:

``observe``
    Dump the hierarchy *and* the focused window. A dump failure is a first-class
    outcome, not an exception that ends the run, because "the screen is
    unreadable right now" is common (animation in flight, FLAG_SECURE).
``think``
    Diff against the previous screen, feed the :class:`LoopDetector`, and check
    the budget. **Before** asking the policy -- the checks are cheap and the
    policy call is expensive, so a step that will trip is not worth a model call.
``decide``
    Ask the policy for exactly one action. A policy that raises is recorded and
    counted; ``policy_error_limit`` consecutive raises ends the run rather than
    spinning on a broken policy.
``act``
    Guard check -> if dry-run and mutating, record the *plan* and do nothing.
    Otherwise dispatch, then invalidate the hierarchy cache so the next observe
    cannot be served from a pre-tap cache. That invalidation is the difference
    between "the tap did nothing" and "the tap worked but we looked at a stale
    cache": without it every action looks like a no-op and the loop reports
    stagnation forever.
``settle``
    Sleep only when something actually changed on-device.

Each step appends one JSON object to the trace, so a run is reconstructable
afterwards without a rerun.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

from ..errors import CloudultronError, DeviceError, GuardViolation, HierarchyUnavailable, PolicyError
from ..safety import Guard, describe_verdict, guard_from_config
from ..ui.hashing import LoopDetector, compare, content_hash, structure_hash
from ..ui.render import index_screen, render_digest
from .actions import Action, Dispatcher, Op
from .policy import HistoryEntry, Observation, Policy


class StepOutcome(str, Enum):
    OBSERVED = "observed"
    PLANNED = "planned"  #: dry-run withheld a mutation
    EXECUTED = "executed"
    BLOCKED = "blocked"  #: the guard refused it, dry-run was not the reason
    REFUSED = "refused"  #: index out of range, bad action, policy error
    FAILED = "failed"  #: device raised while executing


class TerminalReason(str, Enum):
    DONE = "done"
    ABORTED = "aborted"
    STEP_BUDGET = "step_budget"
    STAGNATION = "stagnation"
    OSCILLATION = "oscillation"
    LIVELOCK = "livelock"
    DUMP_UNREADABLE = "dump_unreadable"
    POLICY_ERRORS = "policy_errors"


@dataclass
class StepRecord:
    """One turn of the loop, in the shape the trace file stores."""

    step: int
    at: float
    outcome: str = StepOutcome.OBSERVED.value
    method: str = ""
    nodes: int = 0
    package: str = ""
    focused_window: str = ""
    structure_hash: str = ""
    content_hash: str = ""
    change: str = ""
    change_level: str = ""
    decision: str = ""
    decision_source: str = ""
    rationale: str = ""
    target: str = ""
    effect: str = ""
    guard: str = ""
    detail: str = ""
    loop: str = ""
    elapsed_ms: int = 0
    dump_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunReport:
    """Aggregate result of one run; safe to print as JSON."""

    status: str = "ok"
    terminal: TerminalReason | None = None
    reason: str = ""
    steps: int = 0
    observed: int = 0
    #: Mutations the guard withheld. The pair with ``executed`` is the whole
    #: dry-run story: planned-but-not-run versus actually-run.
    planned: int = 0
    #: Mutations actually dispatched. NOOP/WAIT steps are *not* counted here:
    #: this number is what a reviewer scans to answer "did anything happen to the
    #: device?", and letting no-ops inflate it defeats the metric.
    executed: int = 0
    blocked: int = 0
    refused: int = 0
    failed: int = 0
    elapsed_s: float = 0.0
    dry_run: bool = True
    final_package: str = ""
    final_window: str = ""
    structure_changes: int = 0
    trace_path: str = ""
    #: Which ruleset gated this run, and whether the operator waived it. Recorded
    #: so a trace file is interpretable without knowing how it was launched.
    guard_profile: str = ""
    operator_mode: bool = False
    #: Count of actions the profile objected to and the operator arming allowed.
    operator_overrides: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"status={self.status}",
            f"steps={self.steps}",
            f"executed={self.executed}",
            f"planned={self.planned}" if self.dry_run else None,
            f"blocked={self.blocked}" if self.blocked else None,
            f"failed={self.failed}" if self.failed else None,
            f"changes={self.structure_changes}",
            # The ruleset belongs in the summary line: two runs that look
            # identical but armed different profiles did different things, and
            # "blocked=0" alone cannot tell you whether that was safety or luck.
            f"guard={self.guard_profile}(overrides={self.operator_overrides})"
            if self.operator_mode
            else f"guard={self.guard_profile}"
            if self.guard_profile != "explore"
            else None,
            f"{self.elapsed_s:.1f}s",
        ]
        line = " ".join(b for b in bits if b)
        if self.terminal:
            line += f" -> {self.terminal.value}: {self.reason}"
        return line


class Executor:
    """Drives the loop over one device with one policy."""

    def __init__(
        self,
        device: Any,
        policy: Policy,
        *,
        config: Any,
        guard: Guard | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_step: Callable[[StepRecord], None] | None = None,
    ) -> None:
        self.device = device
        self.policy = policy
        self.config = config
        # Built from config when not supplied, so a library caller cannot get the
        # CLI's strict defaults while believing it asked for a profile.
        self.guard = guard if guard is not None else guard_from_config(config)
        self.clock = clock
        self.sleeper = sleeper
        self.on_step = on_step
        self.detector = LoopDetector(
            stagnation_limit=config.stagnation_limit,
            max_period=config.oscillation_max_period,
            livelock_limit=config.policy_livelock_limit,
        )
        self.steps: list[StepRecord] = []
        self.warnings: list[str] = []
        self._history: list[HistoryEntry] = []
        self._previous_screen = None
        self._previous_hash = ""
        self._consecutive_dump_errors = 0
        self._consecutive_policy_errors = 0
        #: How many mutations we have actually put on the wire. Until this is
        #: non-zero the environment is not ours to judge (see _one_step).
        self._dispatched_mutations = 0
        self._trace: list[pathlib.Path] = []
        self._record_dir: pathlib.Path | None = None
        if config.record_dir:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            self._record_dir = pathlib.Path(config.record_dir) / f"run-{stamp}"
            self._record_dir.mkdir(parents=True, exist_ok=True)
            (self._record_dir / "config.json").write_text(
                json.dumps({"config": config.redacted(), "policy": getattr(policy, "name", "?")}, indent=2, default=str),
                encoding="utf-8",
            )

    # ------------------------------------------------------------ main loop

    def run(self, *, max_steps: int | None = None, until: Callable[[StepRecord], bool] | None = None) -> RunReport:
        """Run up to ``max_steps`` iterations, stopping on any tripwire.

        ``until`` is checked after each step so a caller can supply a goal test
        ("we are on the settings screen") without subclassing.
        """
        limit = self.config.max_steps if max_steps is None else int(max_steps)
        report = RunReport(dry_run=self.config.dry_run)
        started = self.clock()

        for step in range(limit):
            record = StepRecord(step=step, at=self.clock())
            try:
                stop_reason = self._one_step(step, record, report)
            except CloudultronError as exc:  # expected failure modes only
                record.outcome = StepOutcome.FAILED.value
                record.detail = f"{type(exc).__name__}: {exc}"
                report.failed += 1
                self._finish_step(record, report)
                if isinstance(exc, GuardViolation):
                    continue  # one blocked action must not end a run
                if self.config.fail_fast_on_dump_error and isinstance(exc, HierarchyUnavailable):
                    return self._finalise(report, TerminalReason.DUMP_UNREADABLE, str(exc), started)
                continue
            except Exception as exc:  # noqa: BLE001 - keep the trace, then stop
                record.outcome = StepOutcome.FAILED.value
                record.detail = f"unexpected {type(exc).__name__}: {exc}"
                report.failed += 1
                self._finish_step(record, report)
                report.warnings.append(f"unexpected error at step {step}; aborting run")
                return self._finalise(report, TerminalReason.ABORTED, str(exc), started)

            self._finish_step(record, report)
            if stop_reason is not None:
                terminal, reason = stop_reason
                return self._finalise(report, terminal, reason, started)
            if until is not None and until(record):
                return self._finalise(report, TerminalReason.DONE, "caller goal satisfied", started)

        return self._finalise(report, TerminalReason.STEP_BUDGET, f"reached max_steps={limit}", started)

    # ------------------------------------------------------------- one step

    def _one_step(self, step: int, record: StepRecord, report: RunReport) -> tuple[TerminalReason, str] | None:
        # ---------------- OBSERVE
        dump_started = self.clock()
        try:
            hierarchy = self.device.hierarchy(force=step > 0)
        except HierarchyUnavailable as exc:
            self._consecutive_dump_errors += 1
            record.outcome = StepOutcome.REFUSED.value
            record.detail = f"dump unavailable: {exc}"
            self.detector.observe(_EmptyScreen())
            if self._consecutive_dump_errors >= 3:
                return TerminalReason.DUMP_UNREADABLE, (
                    f"no readable hierarchy for {self._consecutive_dump_errors} steps "
                    "(secure window? uiautomator wedged? try `am force-stop com.android.shell`)"
                )
            return None
        screen = hierarchy.screen
        self._consecutive_dump_errors = 0
        record.dump_ms = int((self.clock() - dump_started) * 1000)
        record.method = hierarchy.method
        record.nodes = screen.node_count
        record.package = screen.package
        try:
            record.focused_window = self.device.current_focus() or ""
        except Exception:  # noqa: BLE001 - focus is decoration, never fatal
            record.focused_window = ""

        struct = structure_hash(screen)
        content = content_hash(screen)
        record.structure_hash, record.content_hash = struct[:12], content[:12]

        diff = compare(self._previous_screen, screen)
        report.observed += 1
        # The first observation has no baseline; compare() reports it as a change
        # so the policy sees "this is new", but counting it as a transition would
        # put a phantom event in every metrics line.
        if self._previous_screen is not None and diff.structure_changed:
            report.structure_changes += 1
        record.change, record.change_level = diff.summary(), diff.level
        self._previous_screen = screen

        # ---------------- THINK: is the loop going anywhere?
        # Only judge the environment once we have been allowed to affect it.
        self.detector.observe(screen)
        verdict = self.detector.check(environment_trips=self._dispatched_mutations > 0)
        if verdict.stuck:
            record.loop = f"{verdict.kind}: {verdict.detail}"
            return self._tripwire(verdict, step, report)
        elif verdict.kind == "loading":
            # Structure static but content moving: give it a chance, once.
            record.loop = verdict.detail
            self._maybe_wait(screen, record)
        elif verdict.kind == "idle":
            record.loop = verdict.detail
        if verdict.kind not in {"ok", "idle"}:
            self.warnings.append(f"step {step}: {verdict.kind} ({verdict.detail})")

        # ---------------- THINK: ask the policy
        indexed = index_screen(screen, max_count=self.config.max_interactables)
        digest, _ = render_digest(
            screen,
            focused_window=record.focused_window,
            max_count=self.config.max_interactables,
            max_label_len=self.config.max_label_len,
        )
        observation = Observation(
            step=step,
            screen=screen,
            digest=digest,
            diff_summary=diff.summary(),
            diff_level=diff.level,
            focused_window=record.focused_window,
            history=tuple(self._history[-8:]),
            steps_remaining=max(0, self.config.max_steps - step),
            hints=tuple(w for w in self.warnings[-3:]),
            structure_hash=struct,
            content_hash=content,
            extra={"indexed_count": len(indexed), "raw": hierarchy.raw[:2000] if self.config.record_dir else ""},
        )
        try:
            action = self.policy.decide(observation)
            self._consecutive_policy_errors = 0
        except PolicyError as exc:
            self._consecutive_policy_errors += 1
            record.outcome = StepOutcome.REFUSED.value
            record.detail = f"policy error: {exc}"
            if self._consecutive_policy_errors >= 3:
                return TerminalReason.POLICY_ERRORS, f"policy raised {self._consecutive_policy_errors}x in a row"
            return None
        except Exception as exc:  # noqa: BLE001 - a broken policy is not a device fault
            self._consecutive_policy_errors += 1
            record.outcome = StepOutcome.REFUSED.value
            record.detail = f"policy crashed: {type(exc).__name__}: {exc}"
            if self._consecutive_policy_errors >= 3:
                return TerminalReason.POLICY_ERRORS, f"policy kept raising ({exc})"
            return None

        if not isinstance(action, Action):
            record.outcome = StepOutcome.REFUSED.value
            record.detail = f"policy returned {type(action).__name__}, not an Action"
            return None

        record.decision = action.describe()
        record.decision_source = action.source
        record.rationale = action.rationale[:200]
        record.effect = str(action.effect)
        # Only mutating actions feed livelock detection. Repeating NOOP/WAIT is
        # idling, not looping: a policy that is deliberately doing nothing is not
        # "stuck", and reporting it as stuck would make an observe-only run look
        # like a failure.
        if action.is_mutation:
            self.detector.record_action(screen, action.key())

        if action.is_terminal:
            record.outcome = StepOutcome.OBSERVED.value
            return (
                (TerminalReason.DONE, action.rationale or "policy reported done")
                if action.op is Op.DONE
                else (TerminalReason.ABORTED, action.rationale or "policy aborted")
            )

        # ---------------- ACT: guard first, always
        dispatcher = Dispatcher(self.device, screen=screen, indexed=indexed, size=screen.window_size or self._size())
        try:
            record.target = dispatcher.label_for(action)
        except Exception:  # noqa: BLE001 - cosmetic
            record.target = ""

        # One gate for both paths. Classifying and gating in the same place is
        # what lets operator mode record the objection it waived: the verdict is
        # produced once, and ``overridden`` survives into the trace.
        if action.op is Op.RAW_SHELL:
            verdict = self.guard.check_shell(str(action.args.get("command", "")))
            if verdict.overridden:
                report.operator_overrides += 1
        elif action.is_mutation:
            verdict = self.guard.check_typed(action.effect, action.describe())
        else:
            verdict = None

        if verdict is not None:
            record.guard = describe_verdict(verdict)
            if not verdict.allowed:
                if verdict.deferred:
                    # Dry-run: the *plan* is the result, and it is recorded as such.
                    record.outcome = StepOutcome.PLANNED.value
                    record.detail = f"[dry-run] would have: {action.describe()}"
                    report.planned += 1
                    self._remember(record, action, "planned")
                    return None
                record.outcome = StepOutcome.BLOCKED.value
                record.detail = verdict.reason
                report.blocked += 1
                self._remember(record, action, "blocked")
                return None
            if verdict.overridden:
                record.detail = f"operator override: {verdict.objection}"
            elif action.op is Op.RAW_SHELL:
                record.detail = "raw shell permitted by profile"

        try:
            description = dispatcher.dispatch(action)
        except PolicyError as exc:
            record.outcome = StepOutcome.REFUSED.value
            record.detail = str(exc)
            report.refused += 1
            self._remember(record, action, f"refused: {exc}")
            return None
        except (DeviceError, GuardViolation) as exc:
            record.outcome = StepOutcome.FAILED.value
            record.detail = str(exc)
            report.failed += 1
            self._remember(record, action, f"failed: {exc}")
            return None

        if action.is_mutation:
            report.executed += 1
            self._dispatched_mutations += 1
        record.outcome = StepOutcome.EXECUTED.value
        # An operator waiver must not be overwritten by the success text: the
        # whole value of unguarded mode is being able to read afterwards what it
        # let through. The trace line carries both the effect and the waiver.
        if record.detail.startswith("operator override:"):
            record.detail = f"{description}  [{record.detail}]"
        else:
            record.detail = description
        # Stale-cache guard: the post-action observe must not be served a pre-tap dump.
        self.device.invalidate()
        self._remember(record, action, description)
        if isinstance(self.policy, object) and hasattr(self.policy, "note_outcome"):
            try:
                self.policy.note_outcome(action, diff.summary())
            except Exception:  # noqa: BLE001 - feedback must never break the loop
                pass

        if self.config.settle_delay > 0 and self.sleeper is not None:
            self.sleeper(self.config.settle_delay)
        return None

    # -------------------------------------------------------------- helpers

    def _tripwire(self, verdict: Any, step: int, report: RunReport) -> tuple[TerminalReason, str]:
        kind = {
            "stagnation": TerminalReason.STAGNATION,
            "oscillation": TerminalReason.OSCILLATION,
            "livelock": TerminalReason.LIVELOCK,
        }.get(verdict.kind, TerminalReason.STAGNATION)
        guidance = {
            TerminalReason.STAGNATION: "actions are not changing the screen -- check that coordinates are real and that settle_delay exceeds the animation time",
            TerminalReason.OSCILLATION: "two or more screens are bouncing off each other -- the policy needs a 'do not go back here' rule",
            TerminalReason.LIVELOCK: "the same action keeps being chosen on the same screen -- treat this as a policy bug, not a device one",
        }[kind]
        report.warnings.append(guidance)
        return kind, f"{verdict.detail}. {guidance}"

    def _maybe_wait(self, screen: Any, record: StepRecord) -> None:
        """One extra settle while a screen loads, without burning a step."""
        deadline = self.clock() + min(2.0, self.config.stability_timeout)
        while self.clock() < deadline:
            self.sleeper(0.25)
            try:
                fresh = self.device.hierarchy(force=True).screen
            except (HierarchyUnavailable, DeviceError):
                return
            if structure_hash(fresh) != structure_hash(screen):
                record.detail = (record.detail + " " if record.detail else "") + "layout settled during wait"
                return

    def _size(self) -> tuple[int, int]:
        getter = getattr(self.device, "screen_size", None)
        if getter is None:
            return (0, 0)
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return (0, 0)

    def _remember(self, record: StepRecord, action: Action, outcome: str) -> None:
        self._history.append(
            HistoryEntry(
                step=record.step,
                action=action.key(),
                outcome=outcome[:120],
                structure_hash=record.structure_hash,
                effect=record.effect,
            )
        )

    def _finish_step(self, record: StepRecord, report: RunReport) -> None:
        record.elapsed_ms = int((self.clock() - record.at) * 1000)
        self.steps.append(record)
        report.steps = len(self.steps)
        report.final_package = record.package or report.final_package
        report.final_window = record.focused_window or report.final_window
        if self._record_dir is not None:
            trace = self._record_dir / "trace.jsonl"
            with trace.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), default=str) + "\n")
            self._trace.append(trace)
        if self.on_step is not None:
            self.on_step(record)

    def _finalise(self, report: RunReport, terminal: TerminalReason | None, reason: str, started: float) -> RunReport:
        report.terminal = terminal
        report.reason = reason
        report.guard_profile = self.guard.profile.name
        report.operator_mode = self.guard.operator_mode
        if self.guard.operator_mode and not report.operator_overrides:
            # Zero overrides is worth recording too: it says the operator flag was
            # armed and simply never needed, which is different from it never set.
            report.warnings.append("operator mode was armed for this run; no action required an override")
        # STEP_BUDGET is a normal end for "run 25 steps and stop", so it must not
        # be reported the same way as a tripwire: callers key exit codes off this
        # field, and a script that hits its own ceiling is not failing.
        if terminal is None or terminal is TerminalReason.STEP_BUDGET:
            report.status = "ok"
        elif terminal is TerminalReason.DONE:
            report.status = "done"
        else:
            report.status = "stopped"
        report.elapsed_s = self.clock() - started
        if self._record_dir is not None:
            report.trace_path = str(self._record_dir / "trace.jsonl")
            (self._record_dir / "report.json").write_text(json.dumps(_report_dict(report), indent=2, default=str), encoding="utf-8")
        return report

    # ---------------------------------------------------------- diagnostics

    def last_dump_error(self) -> str:
        return getattr(self.device, "last_error", "")

    def trace_tail(self, n: int = 5) -> Sequence[StepRecord]:
        return self.steps[-n:]


def _report_dict(report: RunReport) -> dict[str, Any]:
    data = asdict(report)
    data["terminal"] = report.terminal.value if report.terminal else None
    return data


class _EmptyScreen:
    """Stand-in so a failed dump still counts toward stagnation.

    If a dump failure were invisible to the detector, a device that never
    yields a tree would loop for the entire budget with a clean "ok" verdict
    every step -- the worst failure mode here, since it looks healthy.
    """

    node_count = 0
    package = ""
    window_size = (0, 0)

    @property
    def root(self):  # pragma: no cover - unused by hashing
        from ..ui.model import UiNode

        return UiNode()

    def walk(self):
        from ..ui.model import UiNode

        return iter([UiNode(cls="unreadable", text="")])

    def interactables(self, *, max_count: int | None = None):
        return iter(())


def wait_for_stable(
    device: Any,
    *,
    timeout: float = 8.0,
    interval: float = 0.4,
    needed: int = 2,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[bool, Any]:
    """Poll the hierarchy until ``needed`` consecutive identical structure hashes.

    A reusable primitive for scripts like "open Settings, wait until it is
    really drawn, then act". Returns ``(stable, screen)``; ``stable`` is False
    on timeout, with the last screen we did manage to read.
    """
    deadline = time.monotonic() + timeout
    previous = ""
    streak = 0
    screen = None
    while time.monotonic() < deadline:
        try:
            screen = device.hierarchy(force=True).screen
        except (HierarchyUnavailable, DeviceError):
            sleeper(interval)
            continue
        current = structure_hash(screen)
        if current == previous:
            streak += 1
            if streak >= needed:
                return True, screen
        else:
            streak = 0
            previous = current
        sleeper(interval)
    return False, screen
