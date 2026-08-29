"""pytest bootstrap: make ``src/`` importable without an install step.

``python -m unittest`` users should run ``python run_tests.py`` instead, which
does the same path setup. This file only exists so that a developer who *does*
have pytest can type ``pytest`` in a fresh checkout and have it work.
"""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
