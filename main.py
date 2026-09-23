"""
Futaba — Main Application Launcher
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "hermes"))

if __name__ == "__main__":
    from futaba.core.app import run
    run()
