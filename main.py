import os
import sys
import discord
from discord.ext import commands
import aiohttp
import logging
import datetime
import random
import re
import json
from cachetools import TTLCache
from dotenv import load_dotenv
load_dotenv()
from google import genai
from google.genai import types
from utils.firestore_service import FirestoreService
from utils import constants

# --- Setup Project-Wide Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Standalone Fact Extraction Function ---
async def extract_facts_from_message(bot_instance, user_message: str):
    """
    Analyzes a user message to extract personal facts using the bot's Gemini client.
    This is a standalone function to prevent 'self' scope issues.
    """
    fact_extraction_prompt = (
        "You are a highly accurate and semantic fact-extraction system. Your task is to analyze a user message "
        "and identify any personal facts, preferences, or descriptions. Your output must be a valid JSON object.\n\n"
        "## Rules:\n"
        "1.  **Identify the Subject:** The subject can be a name or a pronoun. The subject is NEVER part of the key.\n"
        "2.  **Determine the Key:** The key for the JSON object should be a descriptive noun or attribute (e.g., 'pet', 'favorite color', 'hometown', 'relationship'). AVOID using generic verbs like 'has', 'is', or 'are' as the key when a better noun is available.\n"
        "3.  **Handle Pronouns Neutrally:** When the subject is 'I' or 'my', convert the fact to a gender-neutral, third-person perspective.\n"
        "4.  **Return JSON:** If no facts are found, return an empty JSON object: {}.\n\n"
        "## Examples:\n"
        "-   **Input:** '⋆˚☆⋆｡𖦹°‧★｡⋆ smells like seaweed'\n"
        "-   **Output:** {\"smells like\": \"seaweed\"}\n\n"
        "-   **Input:** 'I have a cat named chumba'\n"
        "-   **Analysis:** Subject is 'I'. A better key is 'pet'.\n"
        "-   **Output:** {\"pet\": \"a cat named chumba\"}\n\n"
        "-   **Input:** 'enraged is my boyfriend'\n"
        "-   **Analysis:** Subject is 'enraged'. A better key is 'relationship'. The value should note who they are in a relationship with.\n"
        "-   **Output:** {\"relationship\": \"is their boyfriend\"}\n\n"
        "-   **Input:** 'I miss my little brother'\n"
        "-   **Output:** {\"misses\": \"their little brother\"}\n\n"
        "-   **Input:** 'my favorite color is blue'\n"
        "-   **Output:** {\"favorite color\": \"blue\"}\n\n"
        "## User Message to Analyze:\n"
        f"\"{user_message}\""
    )
    try:
        response = await bot_instance.gemini_client.aio.models.generate_content(
            model=bot_instance.MODEL_NAME,
            contents=[types.Content(role='user', parts=[types.Part(text=fact_extraction_prompt)])],
            config=bot_instance.GEMINI_TEXT_CONFIG
        )
        raw_text = response.text.strip()
        json_match = re.search(r'```json\s*(\{.*?\})\s*```|(\{.*?\})', raw_text, re.DOTALL)
        if json_match:
            json_string = json_match.group(1) or json_match.group(2)
            return json.loads(json_string)
    except Exception:
        logging.error(f"Fact extraction from message failed.", exc_info=True)
    return None


class VinnyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # --- Load Configuration ---
        self.DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
        self.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
        self.GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
        self.OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
        self.FIREBASE_B64 = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_BASE64')
        self.APP_ID = os.getenv('__app_id', 'default-app-id')

        # --- Validate Configuration ---
        if not self.DISCORD_BOT_TOKEN or not self.GEMINI_API_KEY:
            logging.critical("Essential environment variables (DISCORD_BOT_TOKEN, GEMINI_API_KEY) are not set.")
            sys.exit("Error: Essential environment variables are not set.")

        # --- Initialize API Clients ---
        self.gemini_client = None
        self.http_session = None
        self.firestore_service = None

        # --- Load Personality ---
        try:
            with open('personality.txt', 'r', encoding='utf-8') as f:
                self.personality_instruction = f.read()
            logging.info("Personality prompt loaded.")
        except FileNotFoundError:
            logging.critical("personality.txt not found. Please create it.")
            sys.exit("Error: personality.txt not found.")

        # --- Bot State & Globals ---
        self.MODEL_NAME = "gemini-2.5-flash"
        self.processed_message_ids = TTLCache(maxsize=1024, ttl=60)
        self.channel_locks = {}
        self.MAX_CHAT_HISTORY_LENGTH = 10
        
        # --- Persona & Autonomous Mode ---
        self.MOODS = constants.MOODS
        self.current_mood = random.choice(self.MOODS)
        self.last_mood_change_time = datetime.datetime.now()
        self.MOOD_CHANGE_INTERVAL = datetime.timedelta(hours=3)
        self.PASSIVE_LEARNING_ENABLED = True
        self.autonomous_mode_enabled = True
        self.autonomous_reply_chance = 0.05
        self.reaction_chance = 0.15
        
        # --- Centralized Gemini Configuration ---
        self.GEMINI_SAFETY_SETTINGS_TEXT_ONLY = constants.GEMINI_SAFETY_SETTINGS_TEXT_ONLY
        self.GEMINI_TEXT_CONFIG = types.GenerateContentConfig(safety_settings=self.GEMINI_SAFETY_SETTINGS_TEXT_ONLY)
        
        # --- Rate Limiting ---
        self.TEXT_GENERATION_LIMIT = constants.TEXT_GENERATION_LIMIT
        self.SEARCH_GROUNDING_LIMIT = constants.SEARCH_GROUNDING_LIMIT
        self.API_CALL_COUNTS = {
            "date": str(datetime.date.today()),
            "text_generation": 0,
            "search_grounding": 0,
        }

    # --- Core Bot Setup ---
    async def setup_hook(self):
        """This is called once when the bot logs in."""
        logging.info("Running setup_hook...")
        self.http_session = aiohttp.ClientSession()
        self.gemini_client = genai.Client(api_key=self.GEMINI_API_KEY)
        
        # Initialize our new service
        self.firestore_service = FirestoreService(
            loop=self.loop,
            firebase_b64_creds=self.FIREBASE_B64,
            app_id=self.APP_ID
        )
        
        if self.firestore_service.db:
            await self.initialize_rate_limiter()

        logging.info("Loading cogs...")
        await self.load_extension("cogs.vinny_logic")
        logging.info("Cogs loaded successfully.")

    async def on_ready(self):
        logging.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logging.info('------')

    async def process_commands(self, message):
        """This function processes commands."""
        ctx = await self.get_context(message, cls=commands.Context)
        if ctx.command:
            await self.invoke(ctx)
            
    async def on_error(self, event, *args, **kwargs):
        """Catch unhandled errors."""
        logging.error(f"Unhandled error in {event}", exc_info=True)

    async def close(self):
        """Called when the bot is shutting down."""
        logging.info("Shutting down...")
        await super().close()
        if self.http_session:
            await self.http_session.close()
            
    # --- Helper & Utility Functions ---
    def split_message(self, content, char_limit=1900):
        if len(content) <= char_limit: return [content]
        chunks = []
        for chunk in content.split('\n'):
            if len(chunk) > char_limit:
                words = chunk.split(' ')
                new_chunk = ""
                for word in words:
                    if len(new_chunk) + len(word) + 1 > char_limit:
                        chunks.append(new_chunk)
                        new_chunk = word
                    else: new_chunk += f" {word}" if new_chunk else word
                if new_chunk: chunks.append(new_chunk)
            else: chunks.append(chunk)
        return chunks

    # --- Rate Limiting Logic ---
    async def initialize_rate_limiter(self):
        if not self.firestore_service or not self.firestore_service.db: return
        
        today_str = str(datetime.date.today())
        try:
            doc_data = await self.firestore_service.get_rate_limit_doc()
            if doc_data and doc_data.get('date') == today_str:
                self.API_CALL_COUNTS.update(doc_data)
                logging.info(f"Loaded today's API counts: {self.API_CALL_COUNTS}")
            else:
                await self.firestore_service.set_rate_limit_doc(self.API_CALL_COUNTS)
                logging.info(f"Reset API counts for new day: {self.API_CALL_COUNTS}")
        except Exception:
            logging.error("Failed to initialize rate limiter from Firestore.", exc_info=True)

    async def update_api_count_in_firestore(self):
        if not self.firestore_service or not self.firestore_service.db: return
        try:
            await self.firestore_service.update_rate_limit_doc(self.API_CALL_COUNTS)
        except Exception:
            logging.error("Failed to update API count in Firestore.", exc_info=True)


if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = VinnyBot(command_prefix='!', intents=intents, help_command=None)
    
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if TOKEN:
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            logging.critical("Invalid Discord Bot Token. Please check your .env file.")
        except Exception as e:
            logging.critical(f"A critical error occurred during bot startup.", exc_info=True)
    else:
        logging.critical("FATAL: DISCORD_BOT_TOKEN not found in environment.")