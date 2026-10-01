"""Vercel entrypoint. Vercel looks for a top-level ``app`` in ./app.py.

The package lives under src/, so put that on the import path first; this works
whether or not the build installs the package itself.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from execution_cost_tracker.api import app  # noqa: E402,F401
