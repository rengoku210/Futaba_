"""
Futaba — Main module entry point.
Enables running the application via `python -m futaba`.
"""

import sys
from pathlib import Path

# Ensure root paths are accessible
src_dir = Path(__file__).resolve().parent.parent
root_dir = src_dir.parent
hermes_dir = root_dir / "hermes"

for p in [str(src_dir), str(hermes_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from futaba.core.app import run

if __name__ == "__main__":
    run()
