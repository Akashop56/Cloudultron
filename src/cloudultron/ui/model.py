"""Parsed representation of a uiautomator window hierarchy.

uiautomator dumps are flat-ish XML of ``<node>`` elements whose geometry is a
*string* (``bounds="[0,0][1080,1920]"``) rather than numeric attributes, and
whose booleans are ``"true"``/``"false"``. This module normalises all of that
into frozen dataclasses so the rest of the harness never touches the XML again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class Rect:
    """A screen rectangle in device pixels: ``(x1, y1)`` top-left inclusive."""

    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0

    @classmethod
    def parse(cls, raw: str | None) -> "Rect":
        """Parse ``[x1,y1][x2,y2]``; return a null rect on garbage input."""
        if not raw:
            return cls()
        match = _BOUNDS_RE.search(raw)
        if not match:
            return cls()
        x1, y1, x2, y2 = (int(g) for g in match.groups())
        # Normalise so a bogus inverted rect never poisons area math.
        return cls(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.width // 2, self.y1 + self.height // 2)

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def is_empty(self) -> int | bool:
        return self.area == 0

    def contains(self, other: "Rect") -> bool:
        return self.x1 <= other.x1 and self.y1 <= other.y1 and self.x2 >= other.x2 and self.y2 >= other.y2

    def overlaps(self, other: "Rect") -> bool:
        return not (other.x2 <= self.x1 or other.x1 >= self.x2 or other.y2 <= self.y1 or other.y1 >= self.y2)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    def __str__(self) -> str:
        return f"({self.x1},{self.y1})-({self.x2},{self.y2})"


#: Attributes that describe *behaviour*, and therefore belong in the structure
#: hash. Text does not: a clock or a "2 new messages" badge changes constantly.
_VOLATILE_ATTRS = ("text", "content-desc")


@dataclass
class UiNode:
    """One node in the hierarchy. Children are populated by the parser."""

    cls: str = ""
    package: str = ""
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    bounds: Rect = field(default_factory=Rect)
    index: int = 0
    depth: int = 0
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    selected: bool = False
    password: bool = False
    visible_to_user: bool = True
    children: list["UiNode"] = field(default_factory=list)
    parent: "UiNode | None" = field(default=None, repr=False)

    # -------------------------------------------------------------- basics

    @property
    def is_interactable(self) -> bool:
        """Something the user could plausibly act on."""
        if not self.enabled:
            return False
        return bool(
            self.clickable
            or self.long_clickable
            or self.scrollable
            or self.checkable
            or self.focused
            or "EditText" in self.cls
        )

    @property
    def short_class(self) -> str:
        """``android.widget.TextView`` -> ``TextView``."""
        return self.cls.rsplit(".", 1)[-1] if self.cls else "?"

    @property
    def id_short(self) -> str:
        """``com.foo:id/btn_send`` -> ``btn_send``."""
        if not self.resource_id:
            return ""
        return self.resource_id.rsplit("/", 1)[-1]

    @property
    def label(self) -> str:
        """Best human handle for this node, chosen in usefulness order.

        Fall-through to the id/class matters: in a real dump half the visible
        labels live in ``content-desc`` (icons) and a bare ImageButton has
        neither, in which case the resource id *is* the semantic name.
        """
        for candidate in (self.text, self.content_desc, self.id_short):
            cleaned = (candidate or "").strip()
            if cleaned:
                return cleaned
        return self.short_class

    @property
    def path(self) -> str:
        """Index path from the root, e.g. ``0/2/1``. Cheap and layout-stable."""
        parts: list[int] = []
        node: UiNode | None = self
        while node is not None and node.parent is not None:
            parts.append(node.index)
            node = node.parent
        return "/".join(str(p) for p in reversed(parts))

    # ------------------------------------------------------------ traversal

    def walk(self) -> Iterator["UiNode"]:
        """Depth-first, self included."""
        yield self
        for child in self.children:
            yield from child.walk()

    def descendants(self) -> Iterator["UiNode"]:
        for child in self.children:
            yield from child.walk()

    def find_by_id(self, needle: str) -> list["UiNode"]:
        needle = needle.lower()
        return [n for n in self.walk() if needle in n.resource_id.lower()]

    def find_by_text(self, needle: str, exact: bool = False) -> list["UiNode"]:
        needle = needle.strip()
        out: list[UiNode] = []
        for node in self.walk():
            haystacks = (node.text, node.content_desc)
            if exact and any(h == needle for h in haystacks):
                out.append(node)
            elif not exact and any(needle.lower() in h.lower() for h in haystacks):
                out.append(node)
        return out

    # ---------------------------------------------------------- hash pieces

    def structure_parts(self) -> tuple:
        """Shape only: what would change if the user could see a wireframe.

        Deliberately excludes text and content-desc, so a ticking clock or a
        notification badge does not register as a screen transition.
        """
        return (
            self.depth,
            self.short_class,
            self.bounds.as_tuple(),
            self.clickable,
            self.scrollable,
            self.enabled,
        )

    def content_parts(self) -> tuple:
        """Shape *plus* user-visible content and state flags."""
        return self.structure_parts() + (
            self.text,
            self.content_desc,
            self.resource_id,
            self.checked,
            self.selected,
            self.focused,
        )

    def identity_key(self) -> str:
        """Stable-ish key used to match a node across two consecutive dumps.

        Preference order is the whole point: resource ids survive re-layouts,
        bounds shift when a keyboard opens, and a bare ``TextView`` with only
        text as its identity moves. We therefore fall back to text before
        falling back to geometry.
        """
        if self.resource_id:
            return f"id:{self.resource_id}"
        label = self.text.strip() or self.content_desc.strip()
        if label:
            return f"label:{self.short_class}:{label[:60]}"
        return f"geom:{self.short_class}:{self.bounds.as_tuple()}"

    def slot_key(self) -> str:
        """Identity *plus* where it sits: distinguishes siblings from each other.

        Two keys are needed because two questions are being asked. "Did the
        element set change?" must ignore movement, so :meth:`identity_key` drops
        geometry. "Have I already clicked *this* one?" must not, because every row
        in a ``RecyclerView`` shares one resource id -- identity_key alone makes
        clicking row 1 mark rows 2..n as visited, and an explorer then concludes a
        long list has exactly one item in it.

        The trade-off is deliberate: an element that genuinely moved gets
        reconsidered, which is the right answer for a list that scrolled.
        """
        return f"{self.identity_key()}@{self.bounds.as_tuple()}"


@dataclass
class Screen:
    """A parsed dump: the tree plus the metadata the dump header carries."""

    root: UiNode
    window_size: tuple[int, int] = (0, 0)
    rotation: int = 0
    node_count: int = 0
    package: str = ""

    @classmethod
    def build(cls, root: UiNode, *, window_size: tuple[int, int] = (0, 0), rotation: int = 0) -> "Screen":
        # The dump's <hierarchy> element is metadata, not a view, so it is
        # excluded from the count (but stays as the tree root, which keeps
        # multi-window dumps with several top-level nodes working).
        count = sum(1 for _ in root.walk()) - (1 if root.cls == "hierarchy" else 0)
        # The frontmost package is whatever the deepest nodes belong to; the
        # root node is usually a FrameLayout owned by the system UI.
        packages = [n.package for n in root.walk() if n.package]
        front = max(set(packages), key=packages.count) if packages else ""
        screen = cls(root=root, window_size=window_size, rotation=rotation, node_count=count, package=front)
        return screen

    def walk(self) -> Iterator[UiNode]:
        return self.root.walk()

    def interactables(self, *, max_count: int | None = None) -> list[UiNode]:
        """Nodes worth acting on, top-to-bottom / left-to-right, nested dupes pruned.

        Pruning matters for realism: a ``LinearLayout`` marked clickable that
        wraps a clickable ``TextView`` is *one* target, and uiautomator will
        happily hand you both. Without this, a naive "click everything
        clickable" policy double-clicks the same button forever.

        The *container* is the one dropped, so the deepest node wins: a tap on a
        child lands on the child either way, but a tap attributed to the
        container hides which control was really meant. Getting this direction
        backwards is easy -- it looks like working pruning -- and shows up as a
        policy that keeps selecting full-width rows instead of buttons.
        """
        viewport = Rect(0, 0, self.window_size[0] or 1 << 30, self.window_size[1] or 1 << 30)
        candidates = [
            node
            for node in self.root.walk()
            if node is not self.root and node.is_interactable and not node.bounds.is_empty and viewport.overlaps(node.bounds)
        ]
        # Mark every ancestor of every candidate in one upward pass: O(n*depth)
        # instead of testing each candidate against its whole subtree.
        blocked: set[int] = set()
        for node in candidates:
            parent = node.parent
            while parent is not None and id(parent) not in blocked:
                blocked.add(id(parent))
                parent = parent.parent

        kept = [node for node in candidates if id(node) not in blocked]
        kept.sort(key=lambda n: (n.bounds.y1, n.bounds.x1, n.depth))
        if max_count:
            kept = kept[:max_count]
        return kept
