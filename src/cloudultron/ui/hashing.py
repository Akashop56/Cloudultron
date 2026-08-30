"""State fingerprints: two hashes, because "changed" needs a definition.

The single most important idea in the harness
---------------------------------------------
A screen is never byte-for-byte static. A clock in the status bar ticks, a
progress spinner adds a node, a toast appears, a ``RecyclerView`` recycles and
renumbers. Hash the raw XML and *every* step looks like a transition, so the
loop concludes the environment is responsive and never fires its anti-loop
trip. Hash only the structure and you miss a login screen that swapped an error
message without moving a pixel -- and that error message is the whole point of
observing.

So we keep both, and they answer different questions:

``structure_hash``
    geometry + classes + interactability. Answers *"did this become a different
    screen?"*. Used for the anti-loop trip and for "did my tap do anything".

``content_hash``
    structure + text/desc/state flags. Answers *"did anything the user can see
    change?"*. Used for "is my action having an effect" and for judging whether
    waiting for stability is worthwhile.

``diff`` (below) answers the third question: *"what, specifically?"*, which is
what a decision-maker actually needs instead of a boolean.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

from .model import Screen


def _digest(lines: Iterable[str]) -> str:
    """sha256 over a newline-joined sequence, truncated to 16 hex chars.

    64 bits of fingerprint, which is far past what a per-run comparison needs
    and short enough to read in a log line. Truncation is why these are called
    fingerprints and not checksums: they detect *a* change, they are not an
    integrity boundary, and nothing security-relevant should hinge on them.
    """
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def structure_hash(screen: Screen) -> str:
    """Wireframe fingerprint. Immune to text-only changes."""
    return _digest(
        f"{n.depth}|{n.short_class}|{n.bounds.x1},{n.bounds.y1},{n.bounds.x2},{n.bounds.y2}|{int(n.clickable)}{int(n.scrollable)}{int(n.enabled)}"
        for n in screen.walk()
    )


def content_hash(screen: Screen) -> str:
    """Wireframe + visible content. Sensitive to text and state flags."""
    return _digest(
        "|".join(str(part) for part in n.content_parts()) for n in screen.walk()
    )


def raw_hash(xml_text: str) -> str:
    """Fingerprint of the dump bytes. Kept for debugging only -- never for logic."""
    return _digest([xml_text])


# --- element identity sets, for "what changed" rather than "did it change" ---


def element_keys(screen: Screen) -> list[str]:
    """One key per node, order preserved."""
    return [n.identity_key() for n in screen.walk()]


def interactable_keys(screen: Screen) -> list[str]:
    """Keys of the subset a policy could act on."""
    return [n.identity_key() for n in screen.interactables()]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


@dataclass
class Diff:
    """Result of comparing two consecutive screens."""

    #: How big the change was. ``NONE`` and ``VOLATILE`` both mean "pressing
    #: the same button again is not going to help you".
    level: str = "none"
    structure_changed: bool = False
    content_changed: bool = False
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: 0.0 = nothing in common, 1.0 = identical element sets.
    similarity: float = 1.0
    #: Node-count delta; a big positive jump usually means a dialog or keyboard.
    node_delta: int = 0
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        """Anything worth re-deciding over.

        ``initial`` counts: on the first observation there is no history to lean
        on, and a caller that treats "no baseline" as "no change" will skip its
        own opening move.
        """
        return self.level in {"initial", "structural", "content"}

    @property
    def is_static(self) -> bool:
        return self.level in {"none", "volatile"}

    def summary(self) -> str:
        """One line for the trace/CLI. Reads like a diffstat on purpose."""
        if self.level == "none":
            return "identical"
        if self.level == "volatile":
            return f"text-only change (+{len(self.added)} -{len(self.removed)})"
        bits = [self.level]
        if self.added:
            bits.append(f"+{len(self.added)}")
        if self.removed:
            bits.append(f"-{len(self.removed)}")
        if self.node_delta:
            bits.append(f"nodes{self.node_delta:+d}")
        bits.append(f"sim={self.similarity:.2f}")
        return " ".join(bits)


def compare(previous: Screen | None, current: Screen) -> Diff:
    """Classify the change between two screens.

    Levels, from least to most interesting:

    ``error``
        We could not read the screen at all.
    ``none``
        Identical fingerprints. The previous action did nothing.
    ``volatile``
        Content moved but the wireframe did not -- a timer, a spinner, a badge.
    ``content``
        The *set of elements* changed identity while the wireframe did not -- for
        instance a button with no resource id whose label changed, so its
        identity key moved but nothing moved on screen. Note that inserting a
        node is **not** in this bucket: a new box in the tree changes the
        structure hash too, so it is reported as ``structural``.
    ``structural``
        Geometry itself moved. New screen, dialog, or keyboard.
    """
    if previous is None:
        return Diff(level="initial", structure_changed=True, content_changed=True, similarity=0.0, node_delta=0)

    prev_struct, cur_struct = structure_hash(previous), structure_hash(current)
    prev_content, cur_content = content_hash(previous), content_hash(current)

    prev_keys = element_keys(previous)
    cur_keys = element_keys(current)
    prev_set, cur_set = set(prev_keys), set(cur_keys)
    added = tuple(k for k in dict.fromkeys(cur_keys) if k not in prev_set)
    removed = tuple(k for k in dict.fromkeys(prev_keys) if k not in cur_set)

    diff = Diff(
        structure_changed=prev_struct != cur_struct,
        content_changed=prev_content != cur_content,
        added=added[:12],
        removed=removed[:12],
        similarity=_jaccard(prev_set, cur_set),
        node_delta=len(cur_keys) - len(prev_keys),
        detail={
            "prev_structure": prev_struct,
            "cur_structure": cur_struct,
            "prev_content": prev_content,
            "cur_content": cur_content,
        },
    )

    if not diff.content_changed and not diff.structure_changed:
        diff.level = "none"
        diff.similarity = 1.0
    elif diff.structure_changed:
        # Geometry moved -- a new screen, a dialog, or a keyboard pushing the
        # layout up. Deliberately *not* conditioned on the element set changing:
        # when a keyboard opens, every node keeps its resource id, so
        # added/removed are both empty and a set-based test would call a
        # half-screen resize "no real change".
        diff.level = "structural"
    elif added or removed:
        diff.level = "content"
    else:
        diff.level = "volatile"
    return diff


# --- cycle / stagnation detection ------------------------------------------


@dataclass(frozen=True)
class LoopVerdict:
    """What the anti-loop mechanism concluded this step."""

    stuck: bool = False
    kind: str = "ok"  # ok | stagnation | oscillation | livelock
    detail: str = ""

    def __bool__(self) -> bool:
        return self.stuck


class LoopDetector:
    """Decides "the loop is going nowhere", using three separate signals.

    These are separate because they have different causes and fixes:

    *stagnation* -- the screen hasn't structurally changed for N steps. Usually
    the action had no effect (tapped a dead coordinate, animation not settled).

    *oscillation* -- the screen cycles with period 2..N (A-B-A-B). Usually two
    screens bouncing off each other, e.g. tap-to-open then back-to-close, where
    each individual step *is* a change, so a stagnation check alone never fires.
    This is the case the original brief asked to be "smart about", and it is the
    one naive "did the screen change?" checks miss entirely.

    *livelock* -- the (screen, action) *pair* repeats even if the screen keeps
    changing. This is a policy bug, not an environment condition, and the fix is
    on our side of the wire.
    """

    def __init__(
        self,
        *,
        stagnation_limit: int = 4,
        max_period: int = 3,
        livelock_limit: int = 3,
    ) -> None:
        self.stagnation_limit = max(2, int(stagnation_limit))
        self.max_period = max(1, int(max_period))
        self.livelock_limit = max(2, int(livelock_limit))
        self._structures: list[str] = []
        self._contents: list[str] = []
        self._pairs: list[str] = []
        #: Set once we warn; lets the caller escalate instead of silently dying.
        self.last_verdict: LoopVerdict = LoopVerdict()

    def __len__(self) -> int:
        return len(self._structures)

    def reset(self) -> None:
        self._structures.clear()
        self._contents.clear()
        self._pairs.clear()

    def observe(self, screen: Screen) -> None:
        """Record a state observation (before any action is chosen)."""
        self._structures.append(structure_hash(screen))
        self._contents.append(content_hash(screen))

    def record_action(self, screen: Screen, action_key: str) -> None:
        """Record "on this screen I chose this action", for livelock detection."""
        self._pairs.append(f"{structure_hash(screen)}::{action_key}")

    # ---------------------------------------------------------------- checks

    @staticmethod
    def find_cycle(hashes: list[str], max_period: int, min_period: int = 2) -> int | None:
        """Smallest period ``p`` such that the tail repeats with that period.

        Starts at period 2 on purpose. Period 1 *is* stagnation -- "the screen
        has not changed" -- and it is governed by ``stagnation_limit``, which
        wants several samples before firing. Letting the cycle check see it at
        two samples would make every run report oscillation the moment a tap
        landed on a screen that was slow to redraw.
        """
        for period in range(min_period, max_period + 1):
            needed = period * 2
            if len(hashes) < needed:
                continue
            tail = hashes[-needed:]
            # A "cycle" over one distinct value is not a cycle, it is a freeze.
            # Without this guard four identical screens satisfy the period-2 test
            # just as well as the period-1 one, and a stuck device gets reported
            # as ping-ponging -- which sends the operator to a dedup rule when
            # the real answer is that the taps are landing nowhere.
            if len(set(tail)) < 2:
                continue
            if all(tail[i] == tail[i + period] for i in range(period)):
                return period
        return None

    def check(self, *, environment_trips: bool = True) -> LoopVerdict:
        """Evaluate the signals against recorded history.

        ``environment_trips=False`` suppresses stagnation and oscillation while
        leaving livelock active. The executor passes False when it has never
        dispatched a mutation (e.g. dry-run), because "the screen did not
        change" is then the *expected* result, not a fault -- and a detector
        that reports expected behaviour as a loop will trip every single run and
        teach the operator to ignore it, which is worse than not having one.
        """
        # Livelock first: it is the most specific, and its fix is different.
        if len(self._pairs) >= self.livelock_limit:
            latest = self._pairs[-1]
            # Contiguous repeats: "did X on screen S" N times in a row.
            run = 0
            for key in reversed(self._pairs):
                if key != latest:
                    break
                run += 1
            if run >= self.livelock_limit:
                return self._set(LoopVerdict(True, "livelock", f"same action chosen {run}x on the same screen"))

        if environment_trips:
            period = self.find_cycle(self._structures, self.max_period)
            if period:
                return self._set(
                    LoopVerdict(True, "oscillation", f"screen cycles with period {period} ({' -> '.join(self._structures[-period:])[:24]})")
                )

        if len(self._structures) >= self.stagnation_limit:
            tail = self._structures[-self.stagnation_limit:]
            if len(set(tail)) == 1:
                # Confirm with content: if content is moving, a spinner is
                # running and the screen is *loading*, not stuck. Report as
                # stagnation but say so, so the caller can choose to wait.
                content_tail = self._contents[-self.stagnation_limit:]
                moving = len(set(content_tail)) > 1
                if not environment_trips:
                    return self._set(LoopVerdict(False, "idle", "nothing dispatched yet, so no change is expected"))
                return self._set(
                    LoopVerdict(
                        stuck=not moving,
                        kind="stagnation" if not moving else "loading",
                        detail=(
                            "structure unchanged for "
                            + (f"{self.stagnation_limit} steps while content moved (likely loading)")
                            if moving
                            else f"{self.stagnation_limit} steps with no change at all"
                        ),
                    )
                )
        return self._set(LoopVerdict(False, "ok", ""))

    def _set(self, verdict: LoopVerdict) -> LoopVerdict:
        self.last_verdict = verdict
        return verdict
