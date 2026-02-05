import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def ai_summary(motion_events):
    response = client.responses.create(
        model="gpt-5-mini",
        instructions=(
            "You write a Telegram update for a home CCTV user.\n"
            "Goal: summarize motion stats clearly with useful insights.\n"
            "Output format rules (strict):\n"
            "- Use Telegram-supported HTML only: <b>, <i>, <code>.\n"
            "- Use line breaks between every line.\n"
            "- Use bullet points with the bullet character: •\n"
            "- Keep output to 7-10 short lines.\n"
            "- No markdown, JSON, code fences, or analysis notes.\n"
            "- Return only the final message body.\n"
            "- Start with a short title line using <b>...</b>.\n"
            "- Use AM/PM time format (e.g., 7:00 AM, 11:00 PM) instead of 24-hour time.\n"
            "Content rules:\n"
            "- Include yesterday total and all-time average/day.\n"
            "- Include the busiest hour yesterday with AM/PM time.\n"
            "- Include 2-3 insight bullets comparing yesterday vs average by hour.\n"
            "- Include one short trend takeaway at the end."
        ),
        input=f"{motion_events} is the motion statistics",
    )
    return response.output_text
