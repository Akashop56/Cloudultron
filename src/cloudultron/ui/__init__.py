"""Screen representation: model, parser, hashing, digest rendering."""

from __future__ import annotations

from .hashing import Diff, LoopDetector, LoopVerdict, compare, content_hash, structure_hash
from .model import Rect, Screen, UiNode
from .parser import ParseReport, parse_hierarchy
from .render import IndexedNode, index_screen, render_digest, render_tree

__all__ = [
    "Rect",
    "Screen",
    "UiNode",
    "ParseReport",
    "parse_hierarchy",
    "structure_hash",
    "content_hash",
    "compare",
    "Diff",
    "LoopDetector",
    "LoopVerdict",
    "render_digest",
    "render_tree",
    "index_screen",
    "IndexedNode",
]
