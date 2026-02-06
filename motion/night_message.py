from pathlib import Path
import asyncio

PLOTS_DIR = Path(__file__).resolve().parent / "plots"

from motion.day_summary import main as day_summary_main
from ai.ai import ai_summary
from cctv_telegram.message import send_message, send_picture


def main():
    stats = day_summary_main()
    message = ai_summary(stats)
    asyncio.run(send_message(message))
    for file in PLOTS_DIR.iterdir():
        asyncio.run(send_picture(file.absolute()))


if __name__ == "__main__":
    main()
