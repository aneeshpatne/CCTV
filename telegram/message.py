import os
import json
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load environment variables from .env file
load_dotenv()

# Telegram bot token (replace or set BOT_TOKEN in your environment)
TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
print(TOKEN)
# File to store user IDs
WHITELIST_FILE = "whitelist.json"

# Load existing whitelist
def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r") as f:
            return set(json.load(f))
    return set()

# Save whitelist to file
def save_whitelist(user_ids):
    with open(WHITELIST_FILE, "w") as f:
        json.dump(list(user_ids), f, indent=2)

# Start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or "Unknown"

    # Load and update whitelist
    whitelist = load_whitelist()
    if user_id not in whitelist:
        whitelist.add(user_id)
        save_whitelist(whitelist)
        print(f"✅ New user added: {username} ({user_id})")
        await update.message.reply_text(
            f"Hi {username}! You’ve been added to the whitelist ✅"
        )
    else:
        print(f"User {username} ({user_id}) already in whitelist.")
        await update.message.reply_text("You’re already on the list.")

# Run the bot
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()

if __name__ == "__main__":
    main()
