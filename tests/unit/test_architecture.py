"""The layer rules are executable: a forbidden import fails this test."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_layer_contracts_hold() -> None:
    exe = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    assert exe is not None, "import-linter is not installed"
    result = subprocess.run([exe], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
