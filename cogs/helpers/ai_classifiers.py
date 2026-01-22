import json
import re
import logging
from google.genai import types

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
            contents=[summary_prompt]
        )
        if response:
            return response.text.strip()
    except Exception:
        logging.error("Failed to generate short-term summary.", exc_info=True)
    return ""

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
            contents=[sentiment_prompt]
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

async def get_intent_from_prompt(bot_instance, message):
    """Asks the Gemini model to classify the user's intent via a text prompt."""
    intent_prompt = (
        "You are an intent routing system. Analyze the user's message and determine which function to call. "
        "Your output MUST be a single, valid JSON object and NOTHING ELSE.\n\n"
        "## Available Functions:\n"
        "1. `generate_image`: For generic art requests (e.g., 'paint a dog', 'draw a landscape'). Requires a 'prompt' argument.\n"
        "2. `generate_user_portrait`: For requests where the user asks to be painted THEMSELVES (e.g., 'paint me', 'draw my portrait', 'do a picture of me'). Requires NO arguments.\n"
        "3. `get_weather`: For requests about the weather. Requires a 'location' argument.\n"
        "4. `get_user_knowledge`: For requests about what you know about a person. Requires a 'target_user' argument.\n"
        "5. `tag_user`: For requests to ping someone. Requires 'user_to_tag' and optional 'times_to_tag'.\n"
        "6. `get_my_name`: For when the user asks 'what's my name'.\n"
        "7. `general_conversation`: Fallback for everything else.\n\n"
        "## Examples:\n"
        "- 'paint a sad clown' -> {\"intent\": \"generate_image\", \"args\": {\"prompt\": \"a sad clown\"}}\n"
        "- 'paint me' -> {\"intent\": \"generate_user_portrait\", \"args\": {}}\n"
        "- 'draw a picture of me' -> {\"intent\": \"generate_user_portrait\", \"args\": {}}\n"
        f"## User Message to Analyze:\n"
        f"\"{message.content}\""
    )
    
    try:
        json_config = types.GenerateContentConfig(response_mime_type="application/json")
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
        json_config = types.GenerateContentConfig(response_mime_type="application/json")
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
        response = await bot_instance.gemini_client.aio.models.generate_content(
            model=bot_instance.MODEL_NAME, 
            contents=[contradiction_check_prompt], 
            config=text_gen_config
        )
        if "yes" in response.text.lower():
            logging.info(f"Correction detected for user {message.author.display_name}. Message: '{message.content}'")
            return True
    except Exception:
        logging.error("Failed to perform contradiction check.", exc_info=True)
    return False