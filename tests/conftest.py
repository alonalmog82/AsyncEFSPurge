"""Pytest configuration to ensure tests use local source code."""

import sys
from pathlib import Path

# Add src directory to Python path to ensure tests use local source code
# instead of installed package
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))
