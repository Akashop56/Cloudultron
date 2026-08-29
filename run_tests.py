#!/usr/bin/env python3
"""Run the whole suite with the standard library only.

On Termux and in minimal images ``pytest`` is often unavailable, and a harness
whose tests require a package install to run is a harness nobody re-runs. So the
entry point is stdlib unittest, with a pytest shim for whoever prefers it::

    python3 run_tests.py            # all tests
    python3 run_tests.py -v         # verbose
    python3 run_tests.py safety     # only tests/test_safety.py
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _filter(suite, needles: list[str]):
    """Keep tests whose id contains any selector, so ``run_tests.py safety`` and
    ``run_tests.py tap_index`` both work without naming files exactly."""
    import unittest as _u

    out = _u.TestSuite()
    for test in _iter(suite):
        ident = test.id()
        if any(needle in ident for needle in needles):
            out.addTest(test)
    return out


def _iter(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter(item)
        else:
            yield item


def main(argv: list[str]) -> int:
    verbosity = 2 if ("-v" in argv or "--verbose" in argv) else 1
    selectors = [a for a in argv if not a.startswith("-")]
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT), pattern="test_*.py")
    if selectors:
        suite = _filter(suite, selectors)
        if suite.countTestCases() == 0:
            print(f"no tests matched {selectors}", file=sys.stderr)
            return 2
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
