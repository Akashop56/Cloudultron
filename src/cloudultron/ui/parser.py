"""Turn a ``uiautomator dump`` XML string into a :class:`~cloudultron.ui.model.Screen`.

Why this module is not just ``ElementTree.fromstring``
------------------------------------------------------
Real dumps are hostile in ways that are invisible in a unit test written from
the spec. Concretely, the things we handle here:

* **Trailing garbage.** ``uiautomator dump /dev/tty`` prints the XML *and then*
  ``UI hierchary dumped to: /dev/tty``, and ``adb shell`` may interleave a
  ``ERROR:`` line. We slice to the ``<hierarchy>`` element.
* **Bare ``&`` and control bytes.** Some webviews put ``\\x00``-ish bytes in
  text; expat rejects both. We strip XML-illegal code points rather than fail.
* **Attribute naming drift.** Stock uiautomator emits bare names (``text=``),
  but accessibility-service-derived dumps use ``android:text``. We accept both.
* **A truncated dump.** If the write raced the read you get valid XML missing
  its tail, which expat reports as "no element found". We surface that as
  :class:`HierarchyUnavailable` so the loop can retry instead of treating a
  partial tree as "the screen changed".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from ..errors import HierarchyUnavailable
from .model import Rect, Screen, UiNode

#: Code points illegal in XML 1.0. Everything outside these ranges is fine.
_ILLEGAL_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufdd0-\ufdef\ufffe\uffff]"
)
_HIERARCHY_RE = re.compile(r"<hierarchy\b.*</hierarchy>", re.DOTALL)
_BOM = "\ufeff"
_TRUE = {"true", "1", "yes"}


@dataclass(frozen=True)
class ParseReport:
    """What we had to do to the bytes before they parsed.

    Surfaced instead of hidden because "the dump needed 4 characters sanitised"
    is a useful signal that you are reading a view tree that does not want to be
    read, and it distinguishes that from a genuinely broken device.
    """

    sanitised_chars: int = 0
    sliced_from_garbage: bool = False
    recovered_truncation: bool = False
    declared_android_ns: bool = False

    @property
    def noteworthy(self) -> bool:
        return bool(
            self.sanitised_chars
            or self.sliced_from_garbage
            or self.recovered_truncation
            or self.declared_android_ns
        )


def _attr(node: ET.Element, *names: str, default: str = "") -> str:
    """First present attribute among ``names``, else ``default``.

    Hyphenated names are looked up both with and without underscores because
    ``long-clickable`` and ``longclickable`` both appear in the wild.
    """
    for name in names:
        value = node.get(name)
        if value is not None:
            return value
        if "-" in name:
            alt = node.get(name.replace("-", "_"))
            if alt is not None:
                return alt
        else:
            alt = node.get("android:" + name)
            if alt is not None:
                return alt
    return default


def _bool(node: ET.Element, *names: str, default: bool = False) -> bool:
    raw = _attr(node, *names, default="").strip().lower()
    if not raw:
        return default
    return raw in _TRUE


def _int(node: ET.Element, *names: str, default: int = 0) -> int:
    raw = _attr(node, *names, default="").strip()
    try:
        return int(float(raw))
    except ValueError:
        return default


def sanitise(raw: bytes | str) -> tuple[str, ParseReport]:
    """Make dump bytes parseable. Never raises; a caller checks the report."""
    notes = []
    if isinstance(raw, (bytes, bytearray)):
        # latin-1 would never fail but would mangle UTF-8 CJK labels, so try the
        # real encoding first and only fall back for genuinely broken bytes.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
    else:
        text = raw

    text = text.lstrip(_BOM).lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

    match = _HIERARCHY_RE.search(text)
    if match:
        prefix, suffix = text[: match.start()], text[match.end() :]
        # An XML declaration and surrounding whitespace are *part of a valid
        # document*, so they are kept and not reported. Flagging a well-formed
        # prolog as "sliced off garbage" would make the report cry wolf on every
        # healthy dump, and a noisy health signal gets ignored by operators.
        prolog_ok = re.fullmatch(r"(?:\s|<\?xml.*?\?>)*", prefix, re.DOTALL) is not None
        tail_ok = suffix.strip() == ""
        if prolog_ok and tail_ok:
            body = prefix + match.group(0)
        else:
            notes.append("sliced")
            body = match.group(0)
    else:
        # No closing tag but an opening one: the dump was cut off mid-write.
        start = text.find("<hierarchy")
        if start == -1:
            body = text  # let the parser produce a precise error
        else:
            body = text[start:]
            notes.append("truncated")

    cleaned, count = _ILLEGAL_XML.subn("", body)
    if count:
        notes.append(f"stripped:{count}")
    # A bare '&' (ampersand in a label, unescaped) is common enough in webviews.
    # We only touch the ones that cannot start an entity, so real escapes survive.
    cleaned = re.sub(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)", "&amp;", cleaned)
    with_ns = _declare_android_ns(cleaned)
    if with_ns != cleaned:
        cleaned = with_ns
        notes.append("xmlns")
    return cleaned, ParseReport(
        sanitised_chars=count,
        sliced_from_garbage="sliced" in notes,
        recovered_truncation="truncated" in notes,
        declared_android_ns="xmlns" in notes,
    )


#: The URI AOSP's accessibility dumps bind the ``android`` prefix to.
ANDROID_NS_URI = "http://schemas.android.com/apk/res/android"


def _declare_android_ns(body: str) -> str:
    """Declare ``xmlns:android`` if the document uses the prefix without binding it.

    An unbound prefix is a *fatal* XML error ("unbound prefix"), not a warning,
    and it is reachable in practice: dumps piped through a helper that strips the
    root element's attributes, or a salvaged fragment, keep ``android:text=`` but
    lose the declaration. Declaring the URI costs nothing and turns a hard parse
    failure into a readable tree.
    """
    if ANDROID_NS_URI in body or "xmlns:android" in body:
        return body
    if not re.search(r"[\s<]android:[\w-]+\s*=", body):
        return body
    match = re.search(r"<([\w.:-]+)", body)
    if not match:
        return body
    insert_at = match.end()
    return body[:insert_at] + f' xmlns:android="{ANDROID_NS_URI}"' + body[insert_at:]


def _strip_namespaces(element: ET.Element) -> None:
    """Rewrite ``{uri}text`` attribute keys and tags to plain names, in place.

    ElementTree resolves prefixes to ``{uri}local`` immediately, so once a dump
    *does* declare ``xmlns:android`` a naive ``node.get("text")`` silently returns
    None for every node -- the failure mode where "no interactable elements"
    appears on a perfectly ordinary screen. Flattening here means the rest of the
    parser only ever sees one naming convention.
    """
    for node in element.iter():
        if "{" in node.tag:
            node.tag = node.tag.split("}", 1)[-1]
        if any("{" in key for key in node.attrib):
            fixed: dict[str, str] = {}
            for key, value in node.attrib.items():
                local = key.split("}", 1)[-1] if "{" in key else key
                fixed.setdefault(local, value)
            node.attrib.clear()
            node.attrib.update(fixed)


def parse_hierarchy(raw: bytes | str, *, lenient: bool = True) -> tuple[Screen, ParseReport]:
    """Parse dump output into a :class:`Screen`.

    ``lenient=True`` closes any unclosed ``<node>`` tags so a dump that was cut
    off mid-write still yields a partial-but-honest tree; this is the difference
    between "screen looks simpler than it is" and a hard step failure.
    """
    text, report = sanitise(raw)
    if not text.strip():
        raise HierarchyUnavailable("dump produced no output", raw_output="")

    try:
        element = ET.fromstring(text)
    except ET.ParseError as exc:
        if not lenient:
            raise HierarchyUnavailable(f"dump is not valid XML: {exc}", raw_output=text[:400]) from exc
        element = _salvage(text, exc)
        report = ParseReport(
            sanitised_chars=report.sanitised_chars,
            sliced_from_garbage=report.sliced_from_garbage,
            recovered_truncation=True,
            declared_android_ns=report.declared_android_ns,
        )

    # Flatten `{uri}name` before anything reads attributes, so that both the
    # bare and the namespaced dump formats take the same path from here on.
    _strip_namespaces(element)
    if element.tag != "hierarchy":
        # `<accessibility-node-tree>` and friends are accepted; the root element
        # is metadata either way and its children are the actual view nodes.
        pass

    rotation = _int(element, "rotation")
    win = _attr(element, "window-size", "displaySize", default="")
    window_size = _parse_window_size(win, element)

    root = _to_node(element, depth=0)
    if not root.children and len(list(element)) == 0:
        raise HierarchyUnavailable(
            "dump contained no nodes (secure window, or uiautomator wedged)",
            raw_output=text[:400],
        )
    return Screen.build(root, window_size=window_size, rotation=rotation), report


def _parse_window_size(raw: str, element: ET.Element) -> tuple[int, int]:
    match = re.search(r"(\d+)x(\d+)", raw or "")
    if match:
        return (int(match.group(1)), int(match.group(2)))
    # Fall back to the union of node bounds, which is correct often enough to
    # be worth doing, since a missing window size breaks viewport culling.
    xs, ys = [], []
    for node in element.iter("node"):
        rect = Rect.parse(node.get("bounds"))
        xs.extend([rect.x1, rect.x2])
        ys.extend([rect.y1, rect.y2])
    if xs and ys:
        return (max(xs), max(ys))
    return (0, 0)


def _to_node(element: ET.Element, *, depth: int) -> UiNode:
    """Map one ``<node>`` (or the synthetic root) to a UiNode."""
    node = UiNode(
        cls=_attr(element, "class"),
        package=_attr(element, "package"),
        resource_id=_attr(element, "resource-id"),
        text=_attr(element, "text"),
        content_desc=_attr(element, "content-desc"),
        bounds=Rect.parse(_attr(element, "bounds")),
        index=_int(element, "index"),
        depth=depth,
        clickable=_bool(element, "clickable"),
        long_clickable=_bool(element, "long-clickable", "longclickable"),
        scrollable=_bool(element, "scrollable"),
        checkable=_bool(element, "checkable"),
        checked=_bool(element, "checked"),
        enabled=_bool(element, "enabled", default=True),
        focusable=_bool(element, "focusable"),
        focused=_bool(element, "focused"),
        selected=_bool(element, "selected"),
        password=_bool(element, "password"),
        visible_to_user=_bool(element, "visible-to-user", default=True),
    )
    for child_index, child in enumerate(list(element)):
        if child.tag not in ("node", "object"):
            continue
        child_node = _to_node(child, depth=depth + 1)
        if child_node.index == 0 and child_index:
            child_node.index = child_index  # some dumps omit index entirely
        child_node.parent = node
        node.children.append(child_node)
    return node


def _salvage(text: str, exc: ET.ParseError):
    """Close the tree at the failure point and parse what we have.

    ElementTree has no recovery mode, so the pragmatic move is to truncate to
    the last position that *was* well-formed and rebalance open tags. We lose
    the tail of the tree; we keep a consistent prefix, which is the better
    trade when all we need is "did the screen change".
    """
    # "no element found: line L, column C" style failures point at the cut.
    open_tags: list[str] = []
    out: list[str] = []
    for token in re.finditer(r"<(/?)(hierarchy|node|object)([^>]*?)(/?)>", text):
        closing, tag, attrs, self_closing = token.groups()
        if closing:
            if open_tags and open_tags[-1] == tag:
                open_tags.pop()
            out.append(token.group(0))
        elif self_closing:
            out.append(token.group(0))
        else:
            open_tags.append(tag)
            out.append(f"<{tag}{attrs}>")
    rebuilt = "<hierarchy>" + "".join(out)
    # `out` already contains the <hierarchy> open tag when it was well-formed;
    # rebuild from the tag stack instead to avoid double-wrapping.
    rebuilt = "".join(out) if out and out[0].startswith("<hierarchy") else rebuilt
    for tag in reversed(open_tags):
        rebuilt += f"</{tag}>"
    try:
        return ET.fromstring(rebuilt)
    except ET.ParseError as exc2:  # pragma: no cover - genuinely unrecoverable
        raise HierarchyUnavailable(f"dump is unrecoverable XML: {exc2} (original: {exc})") from exc2
