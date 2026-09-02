"""Pytest configuration for the raava-internal overlay.

Inserts paths on sys.path so that under pytest:
  - ``import centaur_sdk`` resolves (repo root)
  - ``from raava_outreach.client import ...`` resolves (overlay tools dir)
  - ``from raava_supermemory.client import ...`` resolves (overlay tools dir)
  - ``from raava_rbe.client import ...`` resolves (overlay tools dir)
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent           # overlays/raava-internal
_REPO_ROOT = _HERE.parents[1]                      # centaur repo root
_TOOLS_DIR = _HERE / "tools"                       # overlays/raava-internal/tools

for p in (_REPO_ROOT, _TOOLS_DIR):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
