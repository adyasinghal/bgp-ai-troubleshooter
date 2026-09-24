import sys
from pathlib import Path

# Ensure repo root is on sys.path for test discovery and imports
ROOT_DIR = Path(__file__).parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
