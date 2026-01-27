import os
import sys
import discord
from discord.ext import commands
import aiohttp
import logging
import datetime
from zoneinfo import ZoneInfo
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

# 1. Create a custom formatter class
class ESTFormatter(logging.Formatter):
    """A custom logging formatter to display timestamps in EST."""
    def formatTime(self, record, datefmt=None):
        # Convert the log's timestamp to a timezone-aware datetime object
        dt = datetime.datetime.fromtimestamp(record.created, tz=ZoneInfo("America/New_York"))
        
        # Format the datetime object into a string
        if datefmt:
            return dt.strftime(datefmt)
        else:
            return dt.isoformat(timespec='milliseconds')

# 2. Configure the root logger to use our new formatter
def setup_logging():
    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Remove any existing handlers to prevent duplicate logs
    if logger.hasHandlers():
        logger.handlers.clear()
        
    # Create a new handler to print to the console
    handler = logging.StreamHandler()
    
    # Set the formatter for the handler to our custom ESTFormatter
    formatter = ESTFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S,%f')
    handler.setFormatter(formatter)
    
    # Add the configured handler to the root logger
    logger.addHandler(handler)

# 3. Run the setup function
setup_logging()

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
            logging.critical("Essential environment variables are not set.")
            sys.exit("Error: Essential environment variables are not set.")

        # --- API Clients (will be initialized in setup_hook) ---
        self.gemini_client = None
        self.http_session = None
        self.firestore_service = None

        # --- Load Personality ---
        try:
            with open('personality.txt', 'r', encoding='utf-8') as f:
                self.personality_instruction = f.read()
            logging.info("Personality prompt loaded.")
        except FileNotFoundError:
            logging.critical("personality.txt not found.")
            sys.exit("Error: personality.txt not found.")

        # --- Bot State & Globals ---
        self.MODEL_NAME = "gemini-2.5-flash-preview-09-2025"
        self.processed_message_ids = TTLCache(maxsize=1024, ttl=60)
        self.channel_locks = {}
        self.MAX_CHAT_HISTORY_LENGTH = 50
        
        # --- Harm Categories ---
        safety_settings_list = [
            types.SafetySetting(
                category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE
            )
            for cat in [
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            ]
        ]
        
        self.GEMINI_TEXT_CONFIG = types.GenerateContentConfig(
            safety_settings=safety_settings_list,
            #max_output_tokens=5000
            temperature=0.8
        )
    
        # --- Persona & Autonomous Mode ---
        self.MOODS = constants.MOODS
        self.current_mood = random.choice(self.MOODS)
        self.last_mood_change_time = datetime.datetime.now()
        self.MOOD_CHANGE_INTERVAL = datetime.timedelta(hours=3)
        self.PASSIVE_LEARNING_ENABLED = True
        self.autonomous_mode_enabled = True
        self.autonomous_reply_chance = 0.01
        self.reaction_chance = 0.05
        

    async def make_tracked_api_call(self, **kwargs):
        """A centralized method to make Gemini API calls and track them (Unlimited Version)."""
        
        # 1. Start the call
        try:
            logging.info("⏳ Sending request to Gemini...")
            response = await self.gemini_client.aio.models.generate_content(**kwargs)
            logging.info("✅ Gemini responded!")
            
            # 2. Track the Cost (Cloud Ledger)
            try:
                if response and response.usage_metadata:
                    from utils import api_clients  
                    
                    meta = response.usage_metadata
                    in_tok = getattr(meta, 'prompt_token_count', 0) or 0
                    out_tok = getattr(meta, 'candidates_token_count', 0) or 0
                    
                    # Calculate & Log
                    cost = api_clients.calculate_cost(
                        self.MODEL_NAME, "text", input_tokens=in_tok, output_tokens=out_tok
                    )
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    await self.firestore_service.update_usage_stats(today, {
                        "text_requests": 1,
                        "tokens": in_tok + out_tok,
                        "cost": cost
                    })
                    
                    logging.info(f"📊 Tracked: {in_tok} in / {out_tok} out | Cost: ${cost:.5f}")

            except Exception as e:
                logging.error(f"📉 Ledger Error (Non-Fatal): {e}")

            return response

        except Exception as e:
            logging.error(f"❌ CRITICAL API ERROR: {e}", exc_info=True)
            return None
        
    async def setup_hook(self):
        """This is called once when the bot logs in."""
        logging.info("Running setup_hook...")
        self.http_session = aiohttp.ClientSession()
        
        
        self.gemini_client = genai.Client(api_key=self.GEMINI_API_KEY)
        
        self.firestore_service = FirestoreService(
            loop=self.loop,
            firebase_b64_creds=self.FIREBASE_B64,
            app_id=self.APP_ID
        )
            
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
