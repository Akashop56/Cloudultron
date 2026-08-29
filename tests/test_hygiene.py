"""Repo-wide structural checks.

These are the "a future edit will quietly break the design" guards, as opposed
to behavioural tests. Cheap to run, and they fail on the change that would
otherwise be invisible in review:

* the stdlib-only promise that makes Termux viable;
* no shipped TODO/stub markers in a scaffold meant to be run;
* no orphaned string expression, which is what a docstring becomes when a
  statement gets inserted above it -- the function keeps looking documented to a
  reader and returns a dead constant to ``help()``. (This was a real regression
  introduced and caught during development of this repo.)
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _python_files(root: pathlib.Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


class StdlibOnlyTests(unittest.TestCase):
    def test_package_imports_nothing_third_party(self):
        stdlib = set(sys.stdlib_module_names)
        offenders: set[str] = set()
        for path in _python_files(SRC):
            for match in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", path.read_text(encoding="utf-8"), re.MULTILINE):
                top = match.group(1).split(".")[0]
                if top not in stdlib and top != "cloudultron":
                    offenders.add(f"{path.name}: {top}")
        self.assertEqual(
            offenders,
            set(),
            "cloudultron must stay stdlib-only: it is meant to install on Termux, "
            "where compiling lxml or pyyaml is the difference between working and not",
        )

    def test_no_runtime_dependency_declared(self):
        pyproject = (SRC.parent / "pyproject.toml").read_text(encoding="utf-8")
        block = re.search(r"^dependencies\s*=\s*(\[[^\]]*\])", pyproject, re.MULTILINE)
        self.assertIsNotNone(block, "pyproject should declare a dependencies list")
        self.assertEqual(block.group(1).replace(" ", ""), "[]", "runtime deps must stay empty")


class PlaceholderTests(unittest.TestCase):
    MARKERS = ("TODO", "FIXME", "XXX", "HACK:", "NotImplementedError", "pass  # stub")

    def test_no_placeholders_in_source(self):
        offenders = []
        for path in _python_files(SRC):
            text = path.read_text(encoding="utf-8")
            for marker in self.MARKERS:
                if marker in text:
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [], "this scaffold should ship with no unfilled holes")


class DocstringIntegrityTests(unittest.TestCase):
    def test_no_orphaned_docstrings(self):
        offenders = []
        for path in _python_files(SRC):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                body = node.body
                if len(body) < 2:
                    continue
                # A string expression anywhere other than position 0 does nothing.
                for stmt in body[1:]:
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                        # Ignore the common intentional cases: a module-style
                        # comment-substitute inside a try/except or a docstring in
                        # a nested def. Only flag top-level-of-body orphans.
                        offenders.append(f"{path}:{stmt.lineno} {node.name}")
                        break
        self.assertEqual(
            offenders,
            [],
            "a bare string after the first statement is dead code -- usually a docstring "
            "that an inserted statement pushed out of position",
        )

    def test_public_entrypoints_are_documented(self):
        # Targeted, not blanket: the API surface a human will call.
        required = {
            "engine.py": ["Executor", "run", "wait_for_stable"],
            "policy.py": ["Observation", "ExplorePolicy", "ScriptedPolicy", "NullPolicy", "parse_line"],
            "actions.py": ["Action", "Dispatcher", "dispatch"],
            "device.py": ["AndroidDevice", "hierarchy", "current_focus"],
            "hashing.py": ["compare", "structure_hash", "content_hash", "LoopDetector"],
            "parser.py": ["parse_hierarchy"],
            "safety.py": ["Guard", "check_shell", "check_typed"],
        }
        missing = []
        for filename, names in required.items():
            candidates = [p for p in _python_files(SRC) if p.name == filename]
            self.assertTrue(candidates, f"{filename} should exist")
            tree = ast.parse(candidates[0].read_text(encoding="utf-8"))
            found = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found.setdefault(node.name, node)
            for name in names:
                node = found.get(name)
                if node is None:
                    missing.append(f"{filename}:{name} missing")
                elif ast.get_docstring(node) is None:
                    missing.append(f"{filename}:{name} undocumented")
        self.assertEqual(missing, [])


class ImportDirectionTests(unittest.TestCase):
    #: What each layer may reach *outward* to. The point of this map is what is
    #: absent: ``loop`` cannot import ``adb``, so the executor keeps taking an
    #: injected device and stays testable against the fake; ``ui`` cannot import
    #: anything but ``errors``, so a saved dump can always be parsed with no
    #: device, transport, or guard in the process.
    ALLOWED = {
        "errors": set(),
        "config": set(),
        "safety": {"errors"},
        "ui": {"errors"},
        "adb": {"errors", "ui", "config"},
        "loop": {"errors", "ui", "safety", "config"},
        "testing": {"errors", "ui", "adb"},
        "cli": {"errors", "config", "safety", "ui", "adb", "loop", "testing"},
        "__init__": {"errors", "config", "safety", "ui", "adb", "loop", "testing", "cli"},
        "__main__": {"cli"},
    }

    def test_layers_import_in_one_direction_only(self):
        package = SRC / "cloudultron"
        offenders: list[str] = []
        for path in _python_files(package):
            rel = path.relative_to(package)
            # A file in ui/ belongs to layer "ui"; ui/model.py to "ui" too.
            source = rel.parts[0] if len(rel.parts) > 1 else path.stem
            allowed = self.ALLOWED.get(source)
            if allowed is None:
                offenders.append(f"{path}: unknown layer {source!r} (add it to ALLOWED)")
                allowed = set()
            in_subpackage = len(rel.parts) > 1
            for match in re.finditer(r"^[ \t]*from[ \t]+(\.+)([a-zA-Z_]\w*)", path.read_text(encoding="utf-8"), re.MULTILINE):
                dots, target = len(match.group(1)), match.group(2)
                if dots == 1 and in_subpackage:
                    continue  # `from .model import` inside ui/ is intra-layer
                if target not in self.ALLOWED:
                    continue  # a module inside this same package (e.g. .actions)
                if target not in allowed:
                    offenders.append(f"{path.name}: {source} -> {target}")
        self.assertEqual(
            offenders,
            [],
            f"unexpected import direction: {offenders}. If a layer genuinely needs the "
            "other, prefer injecting the collaborator over importing it.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
