import os
import json
import asyncio
from dotenv import load_dotenv
from telegram import Bot
from telegram.request import HTTPXRequest

# Load environment variables from .env file
load_dotenv()

# Telegram bot token
TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# File to store user IDs
WHITELIST_FILE = "whitelist.json"

# Load existing whitelist
def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r") as f:
            return set(json.load(f))
    return set()

async def send_message_to_all(message: str, video_path: str | None = None):
    """Send a message to all whitelisted users, optionally with a video"""
    # Create bot with longer timeout for video uploads
    request = HTTPXRequest(connection_pool_size=8, read_timeout=60, write_timeout=60, connect_timeout=30)
    bot = Bot(token=TOKEN, request=request)
    whitelist = load_whitelist()
    
    if not whitelist:
        print("❌ No users in whitelist. Run the bot first to add users.")
        return
    
    if video_path and not os.path.exists(video_path):
        print(f"❌ Video file not found: {video_path}")
        return
    
    print(f"📤 Sending message to {len(whitelist)} user(s)...")
    if video_path:
        print(f"📹 Including video: {video_path}")
    
    success_count = 0
    fail_count = 0
    
    for user_id in whitelist:
        try:
            if video_path:
                with open(video_path, 'rb') as video_file:
                    await bot.send_video(chat_id=user_id, video=video_file, caption=message)  # type: ignore
            else:
                await bot.send_message(chat_id=user_id, text=message)  # type: ignore
            print(f"✅ Sent to user {user_id}")
            success_count += 1
        except Exception as e:
            print(f"❌ Failed to send to user {user_id}: {e}")
            fail_count += 1
    
    print(f"\n📊 Summary: {success_count} successful, {fail_count} failed")

def main():
    # Your message here
    message = """
🚨 Alert from CCTV System

This is a test message.
Replace this with your actual message.
"""
    
    # Path to video file (set to None to send only text)
    video_path = "2025-11-10 05_57_25.123500.mp4"
    
    asyncio.run(send_message_to_all(message, video_path))

if __name__ == "__main__":
    main()
