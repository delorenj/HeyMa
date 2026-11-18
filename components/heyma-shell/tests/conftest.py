import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
for entry in (HERE, HERE.parent / "wax" / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
