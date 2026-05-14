"""Test suite conftest.

Adds the metadata-writer ``src/`` directory to ``sys.path`` so that
module-level tests can ``import validator`` / ``import output_namer``
without the metadata-writer container package layout being on the path.

The metadata-writer source folder is not a Python package at the repo
root (it lives under ``containers/metadata-writer/src/``), but each
module there is importable as a plain top-level module once its
directory is on ``sys.path``. This avoids the ``importlib.util`` dance
used for the hyphenated ``scripts/mirror-images.py`` module.
"""

from __future__ import annotations

import sys
from pathlib import Path

_METADATA_WRITER_SRC = (
    Path(__file__).resolve().parent.parent
    / "containers"
    / "metadata-writer"
    / "src"
)

_src_str = str(_METADATA_WRITER_SRC)
if _src_str not in sys.path:
    sys.path.insert(0, _src_str)


# The submit-run CLI lives at ``scripts/submit-run.py`` and ships helpers as a
# regular Python package at ``scripts/submit_run/`` (hyphen is only in the CLI
# wrapper filename). Putting ``scripts/`` on ``sys.path`` lets tests do
# ``from submit_run import ...`` the same way the wrapper does at runtime.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_scripts_str = str(_SCRIPTS_DIR)
if _scripts_str not in sys.path:
    sys.path.insert(0, _scripts_str)
