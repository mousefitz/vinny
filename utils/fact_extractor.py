import discord
import re
import json
import logging
from google.genai import types

async def extract_facts_from_message(bot_instance, message_or_str: discord.Message | str, author_name: str = None):
    """
    Analyzes a user message OR a string to extract personal facts.
    If message_or_str is a string, author_name must be provided.
    """
    if isinstance(message_or_str, discord.Message):
        user_name = message_or_str.author.display_name
        user_message = message_or_str.content
    else: # It's a string
        user_name = author_name
        user_message = str(message_or_str)

    fact_extraction_prompt = (
        f"You are a highly accurate fact-extraction system. The user '{user_name}' wrote the following message. "
        "Your task is to analyze the message and identify any personal facts about the subject of the sentence. "
        "Your output must be a valid JSON object.\n\n"
        "## Rules:\n"
        "1.  **Identify the Subject:** The subject of the fact could be the author ('I', 'my') or another person mentioned in the text. The author's name is '{user_name}'.\n"
        "2.  **Determine the Key:** The key for the JSON object should be a descriptive noun or attribute (e.g., 'pet', 'favorite color', 'hometown').\n"
        "3.  **Handle Pronouns Neutrally:** When the subject is 'I' or 'my', convert the fact to a gender-neutral, third-person perspective.\n"
        "4.  **Return JSON:** If no facts are found, return an empty JSON object: {}.\n\n"
        "## Examples:\n"
        "-   Author: 'Mouse', Message: 'I have a cat named chumba' -> {\"pet\": \"a cat named chumba\"}\n"
        "-   Author: 'Mouse', Message: 'enraged is my boyfriend' -> {\"relationship\": \"is their boyfriend\"}\n\n"
        f"## User Message to Analyze:\n"
        f"Author: '{user_name}', Message: \"{user_message}\""
    )
    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[types.Content(role='user', parts=[types.Part(text=fact_extraction_prompt)])],
            config=bot_instance.GEMINI_TEXT_CONFIG
        )
        
        # --- THIS IS THE CHECK ---
        if not response: 
            logging.error("Fact extraction failed (API call aborted or failed).")
            return None 
        
        raw_text = response.text.strip()
        json_match = re.search(r'```json\s*(\{.*?\})\s*```|(\{.*?\})', raw_text, re.DOTALL)
        if json_match:
            json_string = json_match.group(1) or json_match.group(2)
            return json.loads(json_string)
    except Exception:
        logging.error(f"Fact extraction from message failed.", exc_info=True)
    return None