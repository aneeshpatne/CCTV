from pathlib import Path
import sys
from datetime import datetime

# Allow running this file directly: `python motion/day_summary.py`
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utilities.motion_db import get_motion_events_daytime

def load_events():
    return get_motion_events_daytime(datetime.now())
    

def main():
    events = load_events()
    print(events)


if __name__ == "__main__":
    main()
