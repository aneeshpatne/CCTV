import os
from pathlib import Path
from dotenv import load_dotenv
import asyncio
import json
from telegram import Bot
from telegram.constants import ParseMode
import logging

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# ist = pytz.timezone("Asia/Kolkata")


TOKEN = os.getenv("BOT_TOKEN")
WHITELIST_FILE = Path(__file__).parent / "whitelist.json"


def load_whitelist():
    if WHITELIST_FILE.exists() and WHITELIST_FILE.is_file():
        with open(WHITELIST_FILE, "r") as f:
            return list(json.load(f))
    return []


whitelist = load_whitelist()
logging.info(f"[Telegram] Whitelist loaded: {len(whitelist)} recipient(s)")


async def send_message(message: str):
    try:
        bot = Bot(token=TOKEN)
        for chat_id in whitelist:
            await bot.send_message(
                chat_id=chat_id, text=message, parse_mode=ParseMode.HTML
            )
        logging.info("[Telegram] message sent successfully")
    except Exception as e:
        logging.error("[Telegram] message sending failure", e)


async def send_picture(path):
    try:
        bot = Bot(token=TOKEN)
        for chat_id in whitelist:
            if path:
                with open(path, "rb") as pic:
                    await bot.send_photo(chat_id=chat_id, photo=pic)
    except Exception as e:
        logging.error("[Telegram] message pic failure")


# if __name__ == "__main__":
#     asyncio.run(send_message("Hello"))
