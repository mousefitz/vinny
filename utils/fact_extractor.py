import discord
import re
import json
import logging
from google.genai import types

async def extract_facts_from_message(bot_instance, message_or_str: discord.Message | str, author_name: str = None, image_bytes: bytes = None, mime_type: str = None):
    """
    Analyzes a user message to extract personal facts.
    Uses 'OFF' safety settings to prevent API errors on Gemini Flash.
    """
    if isinstance(message_or_str, discord.Message):
        user_name = message_or_str.author.display_name
        user_message = message_or_str.content
    else: 
        user_name = author_name
        user_message = str(message_or_str)

    # 1. Base Prompt
    fact_extraction_prompt = (
        f"You are a highly accurate fact-extraction system. The user '{user_name}' wrote the following message. "
        f"Your task is to identify personal facts **about the user '{user_name}' ONLY**.\n"
        "Your output must be a valid JSON object.\n\n"
        "## Rules:\n"
        "1.  **Subject:** The subject MUST be the author ('I', 'my', 'me'). Ignore third-party facts.\n"
        "2.  **Visuals:** If an image is provided, analyze it if:\n"
        "    - The user claims it is them (e.g., 'me', 'my selfie').\n"
        "    - The user implies it is a reference for their appearance (e.g., 'use this photo', 'look at this').\n"
        "3.  **Format:** Return a JSON object where keys are attributes (e.g., 'hair_color', 'pet', 'hometown') and values are short strings.\n"
        "4.  **Empty:** If no facts are found, return {}.\n\n"
        f"## User Input:\n"
        f"Author: '{user_name}'\nMessage: \"{user_message}\""
    )
    
    parts = [types.Part(text=fact_extraction_prompt)]
    
    if image_bytes and mime_type:
        parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes)))

    # 2. Define Safety Settings
    safety_settings_list = [
        types.SafetySetting(
            category=cat, 
            threshold="OFF" 
        )
        for cat in [
            types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        ]
    ]

    try:
        # 3. Use Local Config with "OFF" settings
        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            safety_settings=safety_settings_list
        )

        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[types.Content(role='user', parts=parts)],
            config=config 
        )
        
        if not response or not response.text: 
            return None 
            
        # --- THE FIX: Bulletproof Regex Extractor (Greedy Fix) ---
        clean_text = re.search(r'```json\s*(\{.*\})\s*```', response.text, re.DOTALL) or re.search(r'(\{.*\})', response.text, re.DOTALL)
        json_string = clean_text.group(1) if clean_text else response.text
        
        return json.loads(json_string)
    
    except Exception:
        logging.error("Fact extraction failed.", exc_info=True)

    return None