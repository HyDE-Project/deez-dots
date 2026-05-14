#!/usr/bin/env python3
"""Python module entrypoint for deez-dots.

This wrapper allows running the repository with:
    python -m deez [args]
"""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> None:
    script_path = Path(__file__).resolve().parent / "deez"
    runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    main()
