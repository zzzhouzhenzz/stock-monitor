"""Run the monitor from this source checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from stock_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
