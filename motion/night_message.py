from pathlib import Path
import sys
import asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TELEGRAM_DIR = REPO_ROOT / "telegram"
if str(TELEGRAM_DIR) not in sys.path:
    sys.path.insert(0, str(TELEGRAM_DIR))

PLOTS_DIR = Path(__file__).resolve().parent / "plots"

from day_summary import main as day_summary_main
from ai.ai import ai_summary
from message import send_message, send_picture


def main():
    stats = day_summary_main()
    message = ai_summary(stats)
    asyncio.run(send_message(message))
    for file in PLOTS_DIR.iterdir():
        asyncio.run(send_picture(file.absolute()))



if __name__ == "__main__":
    main()
