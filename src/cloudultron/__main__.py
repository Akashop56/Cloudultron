"""``python -m cloudultron`` -- same as the installed console script.

Useful on Termux and in a repo checkout, where ``pip install -e .`` may not have
happened yet: ``PYTHONPATH=src python -m cloudultron doctor``.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
