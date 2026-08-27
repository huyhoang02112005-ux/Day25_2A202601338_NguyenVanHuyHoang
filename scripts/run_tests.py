from __future__ import annotations

import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

if __name__ == "__main__":
    ret = pytest.main(["-v", "tests/"])
    sys.exit(ret)
