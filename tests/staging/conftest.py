"""Make the ER staging modules importable by name (they live under pipelines/)."""

from __future__ import annotations

import sys
from pathlib import Path

STAGING_DIR = Path(__file__).resolve().parents[2] / "pipelines" / "endpoints" / "ER" / "staging"
if str(STAGING_DIR) not in sys.path:
    sys.path.insert(0, str(STAGING_DIR))
