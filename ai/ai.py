import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def ai_summary(motion_events):
    response = client.responses.create(
        model="gpt-5-mini",
        instructions="You are a motion event summariser bot, you will give general comments to the statistics passed to you.",
        input="How do I check if a Python object is an instance of a class?",
    )

    print(response.output_text)
    return response.output_text