"""Test/double support: the fake device that speaks adb.

Not imported by the package ``__init__``, so shipping the library does not drag
test doubles into the import graph.
"""

from __future__ import annotations

from .fake import Element, FakeScreen, FakeTransport, build_demo_device, demo_screens

__all__ = ["Element", "FakeScreen", "FakeTransport", "build_demo_device", "demo_screens"]
