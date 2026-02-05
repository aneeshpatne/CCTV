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
            "Goal: summarize motion stats clearly with a bit more detail.\n"
            "Rules:\n"
            "- Keep it to 6-8 short lines (around 80-140 words total).\n"
            "- Plain, friendly tone. No jargon.\n"
            "- Include: yesterday total, all-time average/day, busiest hour yesterday, and one short trend insight.\n"
            "- Mention 2-3 notable hourly comparisons (higher/lower than average).\n"
            "- End with a one-line practical takeaway for the user.\n"
            "- Do not include markdown, JSON, code fences, or analysis notes.\n"
            "- Return only the final user message text.\n"
            "- No preface, no labels, no headings, no explanation, no trailing notes."
        ),
        input=f"{motion_events} is the motion statistics",
    )
    return response.output_text
