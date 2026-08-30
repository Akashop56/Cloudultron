"""Render a screen as a compact, index-addressable digest.

Why not hand the policy the raw XML
-----------------------------------
A real dump of a mid-size app is 150-400 KB of single-line XML, which is both
too many tokens for a model-based policy and mostly noise. This digest is the
interface boundary that fixes that:

* one line per *interactable* element, prefixed by an integer index;
* the policy says ``tap 7``, never ``tap 540 1188`` -- coordinates stay our
  problem, so a hallucinated number is a bounded error (index out of range)
  rather than an arbitrary pixel tap;
* non-interactable text nodes are included as context but left un-indexed, so
  the policy can read a label without being tempted to click the wrapper.

This is also what makes traces replayable: "tapped index 7 on a screen whose
structure hash was ``ab12...``" is auditable, "tapped 540,1188" is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Screen, UiNode


@dataclass(frozen=True)
class IndexedNode:
    """One addressable element plus the index the policy will use to name it."""

    index: int
    node: UiNode

    @property
    def center(self) -> tuple[int, int]:
        return self.node.bounds.center


def index_screen(screen: Screen, *, max_count: int = 60, max_label_len: int = 48) -> list[IndexedNode]:
    """Assign tap-indices to the interactable nodes of a screen."""
    return [
        IndexedNode(i, node)
        for i, node in enumerate(screen.interactables(max_count=max_count))
    ]


def _clip(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def render_digest(
    screen: Screen,
    *,
    focused_window: str = "",
    max_count: int = 60,
    max_label_len: int = 48,
    max_context_lines: int = 12,
    include_uninteractable: bool = True,
) -> tuple[str, list[IndexedNode]]:
    """Return ``(digest_text, indexed_nodes)``.

    The nodes are returned alongside the text so the executor can resolve an
    index without re-parsing the digest it just produced.
    """
    indexed = index_screen(screen, max_count=max_count, max_label_len=max_label_len)
    addressable = {id(item.node) for item in indexed}

    width, height = screen.window_size
    lines: list[str] = [
        f"# screen {width}x{height} rotation={screen.rotation} nodes={screen.node_count}",
        f"# package={screen.package or '?'}" + (f" window={focused_window}" if focused_window else ""),
    ]
    if indexed:
        lines.append("# [n] class id/desc text (center)  <- tap by index")
        for item in indexed:
            node = item.node
            marks = []
            if node.clickable:
                marks.append("click")
            if node.scrollable:
                marks.append("scroll")
            if node.checkable:
                marks.append("check")
            if node.focused:
                marks.append("focused")
            if node.password:
                marks.append("password")
            if not node.enabled:
                marks.append("disabled")
            ident = node.id_short or ""
            label = _clip(node.text or node.content_desc, max_label_len)
            head = f"[{item.index:>2}] {node.short_class}"
            if ident:
                head += f" #{ident}"
            if label:
                head += f" {label!r}"
            cx, cy = node.bounds.center
            tail = f" @({cx},{cy})" + (f" [{','.join(marks)}]" if marks else "")
            lines.append(head + tail)
    else:
        lines.append("# (no interactable elements found)")

    if include_uninteractable:
        context: list[str] = []
        for node in screen.walk():
            if id(node) in addressable or node is screen.root:
                continue
            text = (node.text or "").strip() or (node.content_desc or "").strip()
            if not text or node.bounds.is_empty:
                continue
            entry = f"     . {node.short_class} {_clip(text, max_label_len)!r}"
            if entry not in context:
                context.append(entry)
            if len(context) >= max_context_lines:
                break
        if context:
            lines.append("# non-interactive text:")
            lines.extend(context)

    return "\n".join(lines), indexed


def render_tree(screen: Screen, *, max_nodes: int = 120, max_label_len: int = 40) -> str:
    """Full indented tree, for ``--verbose`` and post-hoc debugging."""
    lines: list[str] = []
    for node in screen.walk():
        if len(lines) >= max_nodes:
            lines.append(f"... truncated at {max_nodes} of {screen.node_count} nodes")
            break
        label = _clip(node.text or node.content_desc or node.id_short, max_label_len)
        flags = "".join(
            f
            for f, on in (
                ("C", node.clickable),
                ("S", node.scrollable),
                ("E", not node.enabled),
                ("*", node.password),
            )
            if on
        )
        lines.append(
            "  " * node.depth
            + f"{node.short_class}"
            + (f" {label!r}" if label else "")
            + f" {node.bounds}"
            + (f" [{flags}]" if flags else "")
        )
    return "\n".join(lines)
