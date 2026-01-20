import discord
import re
import json
import logging
from google.genai import types

async def extract_facts_from_message(bot_instance, message_or_str: discord.Message | str, author_name: str = None, image_bytes: bytes = None, mime_type: str = None):
    """
    Analyzes a user message (and optional image) to extract personal facts.
    """
    if isinstance(message_or_str, discord.Message):
        user_name = message_or_str.author.display_name
        user_message = message_or_str.content
    else: 
        user_name = author_name
        user_message = str(message_or_str)

    # 1. Base Prompt (Text Only)
    fact_extraction_prompt = (
        f"You are a highly accurate fact-extraction system. The user '{user_name}' wrote the following message (and optionally provided an image). "
        f"Your task is to identify personal facts **about the user '{user_name}' ONLY**.\n"
        "Your output must be a valid JSON object.\n\n"
        "## Rules:\n"
        "1.  **Subject:** The subject MUST be the author ('I', 'my', 'me'). Ignore third-party facts.\n"
        "2.  **Visuals:** If an image is provided, analyze it ONLY if the user's text implies it is them (e.g., 'this is me', 'my face', 'selfie'). Extract physical traits like 'hair color', 'eye color', 'beard', 'glasses', 'clothing style'.\n"
        "3.  **Format:** Return a JSON object where keys are attributes (e.g., 'hair_color', 'pet', 'hometown') and values are short strings.\n"
        "4.  **Empty:** If no facts are found, return {}.\n\n"
        "## Examples:\n"
        "- Text: 'I have a cat named Toast' -> {\"pet\": \"a cat named Toast\"}\n"
        "- Text: 'This is me' (Image: Man with beard) -> {\"gender\": \"male\", \"facial_hair\": \"full beard\", \"hair_color\": \"dark\"}\n"
        "- Text: 'Look at this dog' (Image: Dog) -> {} (Ignore, not the user)\n\n"
        f"## User Input:\n"
        f"Author: '{user_name}'\nMessage: \"{user_message}\""
    )
    
    parts = [types.Part(text=fact_extraction_prompt)]
    
    # 2. Add Image Data if present
    if image_bytes and mime_type:
        parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes)))

    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[types.Content(role='user', parts=parts)],
            config=bot_instance.GEMINI_TEXT_CONFIG
        )
        
        if not response or not response.text: 
            return None 
        
        raw_text = response.text.strip()
        json_match = re.search(r'```json\s*(\{.*?\})\s*```|(\{.*?\})', raw_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1) or json_match.group(2)
            return json.loads(json_str)
            
    except Exception:
        logging.error("Fact extraction failed.", exc_info=True)

    return None