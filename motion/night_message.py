from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from day_summary import main as day_summary_main
from ai.ai import ai_summary
def main():
    stats = day_summary_main()
    message = ai_summary(stats)



if __name__ == "__main__":
    main()