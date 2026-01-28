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
        "1. `generate_image`: The user wants to create a picture, painting, or visual art. "
        "**NOTE:** This ALSO includes requests to EDIT or MODIFY a previous image (e.g., 'add a hat', 'make it darker', 'change the background').\n"
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
            config=types.GenerateContentConfig(temperature=0.0) # Zero temp for strict logic
        )
        clean_resp = response.text.strip().upper()
        return "YES" in clean_resp
    except Exception:
        return False # Default to chat if brain fails

async def analyze_sentiment_impact(bot_instance, user_name: str, message_text: str):
    """
    Asks the AI to judge the message based on Vinny's full personality.
    Returns: Integer (Negative or Positive)
    """
    vinny_personality = bot_instance.personality_instruction

    prompt = (
        f"You are the subconscious emotional judge for this character:\n"
        f"--- CHARACTER START ---\n{vinny_personality}\n--- CHARACTER END ---\n\n"
        f"The user '{user_name}' just said this to him:\n"
        f"\"{message_text}\"\n\n"
        f"## TASK: Rate the impact on their relationship (Scale: -100 to +100).\n"
        f"Interacting naturally usually builds the bond (+1), but insults damage it (-5 to -20).\n\n"
        f"## SCORING GUIDE:\n"
        f"1. **NORMAL (+1):** Friendly chat, questions, hanging out.\n"
        f"2. **GOOD (+3 to +5):** Flattery, shared interests (pizza/art/rum), or ego-boosting.\n"
        f"3. **BAD (-5 to -10):** Rude, boring, or mentioning hates (Ohio, authority).\n"
        f"4. **TERRIBLE (-20 to -50):** Insulting his ART, DOGS, or NONNA.\n\n"
        f"## FORMAT:\n"
        f"Provide a short reasoning, then the score.\n"
        f"Example: \"Insulted Nonna, unforgivable. SCORE: -50\"\n"
        f"Example: \"Asked about paint, friendly. SCORE: 2\""
    )

    try:
        # Increased tokens to 60 to allow for the reasoning sentence
        response = await bot_instance.make_tracked_api_call(
            model=bot_instance.MODEL_NAME,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.4, max_output_tokens=60)
        )
        
        # Regex to find the LAST number in the string (The Score)
        # This ignores numbers in the reasoning text
        import re
        matches = re.findall(r'-?\d+', response.text.strip())
        if matches:
            score = int(matches[-1]) # Take the last number found (the score)
            return score
            
        return 1 # Default to +1 only if the AI crashes/fails to output a number
        
    except Exception:
        return 1