import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def ai_summary(motion_events):
    response = client.responses.create(
        model="gpt-5-mini",
        instructions="You are a motion event summariser bot, you will give general comments to the statistics passed to you. You will have to format your response as a message sent to the user directly.",
        input=f"{motion_events} is the motion statistics",
    )
    return response.output_text