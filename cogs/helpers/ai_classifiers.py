import json
import re
import logging
from google.genai import types

# --- GLOBAL SAFETY SETTINGS ---
# Use "OFF" for Gemini 2.5 Flash compatibility
SAFETY_SETTINGS = [
    types.SafetySetting(
        category=cat, threshold="OFF"
    )
    for cat in [
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    ]
]

### Short-Term Summary

async def get_short_term_summary(bot_instance, message_history: list):
    """Summarizes the last few messages to find the current topic."""
    conversation_text = "\n".join(message_history)
    summary_prompt = (
        "You are a conversation analysis tool. Read the following chat log and provide a concise, "
        "one-sentence summary of the current topic or situation.\n\n"
        "## CHAT LOG:\n"
        f"{conversation_text}\n\n"
        "## ONE-SENTENCE SUMMARY:"
    )
    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[summary_prompt],
            config=types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS)
        )
        if response:
            return response.text.strip()
    except Exception:
        logging.error("Failed to generate short-term summary.", exc_info=True)
    return ""

### Sentiment Classification

async def get_message_sentiment(bot_instance, message_content: str):
    """Analyzes the sentiment of a user's message."""
    sentiment_prompt = (
        "You are a sentiment analysis expert. Analyze the following user message and classify its primary sentiment. "
        "Your output MUST be a single, valid JSON object with one key, 'sentiment', and one of the following values: "
        "'positive', 'negative', 'neutral', 'sarcastic', 'flirty', 'angry'.\n\n"
        "## Examples:\n"
        "- User Message: 'I love this new feature, you're the best!' -> {\"sentiment\": \"positive\"}\n"
        "- User Message: 'Ugh, I had such a bad day.' -> {\"sentiment\": \"negative\"}\n"
        "- User Message: 'wow, great job. really impressive.' -> {\"sentiment\": \"sarcastic\"}\n"
        "- User Message: 'hey there ;) what are you up to?' -> {\"sentiment\": \"flirty\"}\n\n"
        f"## User Message to Analyze:\n"
        f"\"{message_content}\""
    )
    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[sentiment_prompt],
            config=types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS)
        )
        if not response:
            logging.error("Failed to get message sentiment (API call aborted or failed).")
            return "neutral"
        
        json_match = re.search(r'```json\s*(\{.*?\})\s*```|(\{.*?\})', response.text, re.DOTALL)
        if json_match:
            json_string = json_match.group(1) or json_match.group(2)
            sentiment_data = json.loads(json_string)
            return sentiment_data.get("sentiment")
    except Exception:
        logging.error("Failed to get message sentiment.", exc_info=True)
    return "neutral"

## Intent Classification

async def get_intent_from_prompt(bot_instance, message):
    """Asks the Gemini model to classify the user's intent via a text prompt."""
    intent_prompt = (
        "You are an intent routing system. Analyze the user's message and determine which function to call. "
        "Your output MUST be a single, valid JSON object and NOTHING ELSE.\n\n"
        "## CRITICAL RULE: ADDRESSING VS SUBJECT\n"
        "If the user starts with 'Vinny, draw...' or 'Vinny paint...', 'Vinny' is likely the addressee, NOT the subject.\n"
        "- 'Vinny draw a cat' -> User is talking TO Vinny. Subject is 'a cat'. Intent: `generate_image`.\n"
        "- 'Draw Vinny' -> User wants a picture OF Vinny. Subject is 'Vinny'. Intent: `generate_user_portrait`.\n"
        "- 'Draw yourself' -> Subject is 'Vinny'. Intent: `generate_user_portrait`.\n\n"
        "## Available Functions:\n"
        "1. `generate_image`: For generic art requests or characters that are NOT specific users/people in the chat (e.g. 'paint a girl', 'draw a goblin', 'paint an alcoholic woman').\n"
        "2. `generate_user_portrait`: ONLY for requests to paint A SPECIFIC REAL PERSON in the chat (e.g. 'paint me', 'paint @Alex', 'paint yourself').\n"
        "   - `target`: The name or reference of the person to paint (e.g. 'me', 'myself', 'alex', 'vinny').\n"
        "   - `details`: Any specific visual modifiers.\n"
        "3. `get_weather`: For requests about the weather. Requires a 'location' argument.\n"
        "4. `get_user_knowledge`: For requests about what you know about a person.\n"
        "5. `tag_user`: For requests to ping/tag someone.\n"
        "   - `user_to_tag`: The name or mention of the person to tag.\n"
        "   - `times_to_tag`: (Optional) The number of times to tag them (integer).\n"
        "6. `get_my_name`: For when the user asks 'what's my name'.\n"
        "7. `general_conversation`: Fallback for everything else.\n\n"
        "## Examples:\n"
        "- 'paint me' -> {\"intent\": \"generate_user_portrait\", \"args\": {\"target\": \"me\", \"details\": \"\"}}\n"
        "- 'Vinny draw a wizard' -> {\"intent\": \"generate_image\", \"args\": {\"prompt\": \"a wizard\"}}\n"
        "- 'draw an alcoholic woman playing fortnite' -> {\"intent\": \"generate_image\", \"args\": {\"prompt\": \"an alcoholic woman playing fortnite\"}}\n"
        "- 'paint @Vincenzo wearing a tuxedo' -> {\"intent\": \"generate_user_portrait\", \"args\": {\"target\": \"Vincenzo\", \"details\": \"wearing a tuxedo\"}}\n"
        "- 'tag Alex' -> {\"intent\": \"tag_user\", \"args\": {\"user_to_tag\": \"Alex\", \"times_to_tag\": 1}}\n"
        "- 'annoy Vinny 3 times' -> {\"intent\": \"tag_user\", \"args\": {\"user_to_tag\": \"Vinny\", \"times_to_tag\": 3}}\n"
        f"## User Message to Analyze:\n"
        f"\"{message.content}\""
    )
    
    try:
        json_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            safety_settings=SAFETY_SETTINGS
        )
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[intent_prompt],
            config=json_config 
        )
        
        if not response: 
            return "general_conversation", {}
            
        intent_data = json.loads(response.text)
        return intent_data.get("intent"), intent_data.get("args", {})

    except json.JSONDecodeError:
        logging.error(f"Failed to parse JSON in intent router. Raw response: '{response.text}'", exc_info=True)
    except Exception:
        logging.error("Failed to get intent from prompt due to an API or other error.", exc_info=True)

    return "general_conversation", {}

## Question Triage

async def triage_question(bot_instance, question_text: str) -> str:
    """Classifies a question to determine the best response strategy."""
    triage_prompt = (
        "You are a question-routing AI. Classify the user's question into one of three categories.\n"
        "Your output MUST be a single, valid JSON object with one key, 'question_type', and one of the following three values:\n"
        "1. 'real_time_search': For questions that require current, up-to-the-minute information (news, weather, sports) or specific, verifiable facts likely outside a general knowledge base.\n"
        "2. 'general_knowledge': For questions whose answers are stable, well-known facts (e.g., history, science, geography).\n"
        "3. 'personal_opinion': For subjective questions directed at the AI's persona, its feelings, or about other users in the chat.\n\n"
        "## Examples:\n"
        "- User Question: 'what were today's news headlines?' -> {\"question_type\": \"real_time_search\"}\n"
        "- User Question: 'who was the first us president?' -> {\"question_type\": \"general_knowledge\"}\n"
        "- User Question: 'vinny what do you think of me?' -> {\"question_type\": \"personal_opinion\"}\n\n"
        f"## User Question to Analyze:\n"
        f"\"{question_text}\""
    )
    try:
        json_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            safety_settings=SAFETY_SETTINGS
        )
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME, contents=[triage_prompt], config=json_config
        )
        if not response: 
            return "personal_opinion"
            
        data = json.loads(response.text)
        return data.get("question_type", "personal_opinion")
    except Exception:
        logging.error("Failed to triage question, defaulting to personal_opinion.", exc_info=True)
        return "personal_opinion"

## Correction Detection

async def is_a_correction(bot_instance, message, text_gen_config) -> bool:
    """Checks if a user's message is correcting a known fact."""
    correction_keywords = ["that's not true", "that isn't true", "you're wrong", "i am not", "i'm not", "i don't have"]
    if not any(keyword in message.content.lower() for keyword in correction_keywords):
        return False
    
    user_id = str(message.author.id)
    guild_id = str(message.guild.id) if message.guild else None
    user_profile = await bot_instance.firestore_service.get_user_profile(user_id, guild_id)
    
    if not user_profile:
        return False
        
    known_facts = ", ".join([f"{k.replace('_', ' ')} is {v}" for k, v in user_profile.items()])
    contradiction_check_prompt = (
        f"Analyze the user's message and the known facts about them. "
        f"Does the message directly contradict one of the known facts? "
        f"Answer with a single word: 'Yes' or 'No'.\n\n"
        f"Known Facts: \"{known_facts}\"\nUser Message: \"{message.content}\""
    )
    try:
        # Use a safe config with our custom settings
        safe_config = types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS)
        
        response = await bot_instance.gemini_client.aio.models.generate_content(
            model=bot_instance.MODEL_NAME, 
            contents=[contradiction_check_prompt], 
            config=safe_config
        )
        if "yes" in response.text.lower():
            logging.info(f"Correction detected for user {message.author.display_name}. Message: '{message.content}'")
            return True
    except Exception:
        logging.error("Failed to perform contradiction check.", exc_info=True)
    return False

## Image Edit Request Detection

async def is_image_edit_request(bot_instance, text: str):
    """
    Determines if a reply to an image is actually a request to change it.
    Returns: True (Edit) or False (Just Chat)
    """
    prompt = (
        f"You are a logic filter. The user is replying to an AI-generated image.\n"
        f"User Message: \"{text}\"\n\n"
        f"Task: Does this message imply a request to MODIFY, CHANGE, or REGENERATE the image? (e.g. 'add a hat', 'make it darker', 'wrong color').\n"
        f"If they are just commenting (e.g. 'cool', 'lol', 'wow', 'thanks'), say NO.\n"
        f"Reply ONLY with 'YES' or 'NO'."
    )
    
    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[prompt],
            config=types.GenerateContentConfig(
                temperature=0.0
                # NO SAFETY SETTINGS HERE (As requested for image logic)
            ) 
        )
        clean_resp = response.text.strip().upper()
        return "YES" in clean_resp
    except Exception:
        return False

## Sentiment Impact Analysis

async def analyze_sentiment_impact(bot_instance, user_name: str, message_text: str):
    """
    Asks the AI to judge the message.
    STRICT MODE: Caps all score changes to max 5 points.
    """
    vinny_personality = bot_instance.personality_instruction

    prompt = (
        f"You are the hidden sentiment engine for a character. "
        f"Your ONLY job is to rate how the user's message impacts their relationship with him.\n"
        f"--- CHARACTER ---\n{vinny_personality}\n--- END CHARACTER ---\n\n"
        f"USER: {user_name}\n"
        f"MESSAGE: \"{message_text}\"\n\n"
        f"## SCORING RULES (STRICT CAP: 5 POINTS)\n"
        f"Scores must be small. Do not inflate numbers.\n"
        f"- **Amazing Compliment/Shared Interest:** +3 to +5 (Max +5)\n"
        f"- **Nice/Friendly:** +1 to +2\n"
        f"- **Normal/Boring:** 0 (No change)\n"
        f"- **Rude/Annoying:** -1 to -3\n"
        f"- **CRITICAL INSULT (Nonna, Art, Dogs):** -5 to -10 (Max Penalty -10)\n\n"
        f"## REQUIRED RESPONSE FORMAT\n"
        f"You must reply with valid JSON only. Do not add markdown.\n"
        f"{{\n"
        f"  \"reasoning\": \"Analysis text...\",\n"
        f"  \"category\": \"POSITIVE\" | \"NEUTRAL\" | \"NEGATIVE\" | \"CRITICAL_INSULT\",\n"
        f"  \"score\": (integer between -10 and 5)\n"
        f"}}"
    )

    try:
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.3, 
                safety_settings=SAFETY_SETTINGS
            )
        )
        
        # --- API Crash Check ---
        if response is None:
            logging.warning(f"⚠️ API Error (Crashed) for: '{message_text}'")
            return 0 

        # --- Safety/Block Check ---
        if not hasattr(response, 'text') or not response.text:
            reason = "Unknown"
            if hasattr(response, 'candidates') and response.candidates:
                reason = response.candidates[0].finish_reason
            
            logging.warning(f"⚠️ Blocked! Reason: {reason} | Input: '{message_text}'")
            return 0 
        
        # --- Success ---
        text_response = response.text.strip()
        text_response = re.sub(r"```json|```", "", text_response).strip()
        data = json.loads(text_response)
        score = int(data.get("score", 0))
        reason = data.get("reasoning", "No reason provided")
        
        logging.info(f"🧠 AI JUDGEMENT: {reason} | Score: {score}")
        return score

    except Exception as e:
        logging.error(f"Sentiment Analysis Failed: {e}")
        return 0
