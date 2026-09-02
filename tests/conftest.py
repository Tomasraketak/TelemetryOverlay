"""Shared fixtures and paths for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

EXAMPLES = ROOT / "examples"
PRESETS = ROOT / "presets"
CSV_SAMPLE = EXAMPLES / "static_fire_thrust.csv"
XLSX_SAMPLE = EXAMPLES / "burn_001_data.xlsx"


def write_csv(path: Path, text: str) -> Path:
    """Write a CSV using CRLF, the way logger firmwares usually do."""
    path.write_text(text.replace("\n", "\r\n"), encoding="utf-8")
    return path
