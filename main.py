#!/usr/bin/env python3
"""Root entry point to run GitAcross directly from source."""

import sys
from pathlib import Path

# Ensure src/ is in sys.path when running from the repo root
src_path = Path(__file__).resolve().parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from gitacross.main import main

if __name__ == "__main__":
    main()
