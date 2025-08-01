import os
import discord
from discord.ext import commands
import json
import sys
import datetime
import re
import asyncio
import aiohttp
import base64
import io
import random
from zoneinfo import ZoneInfo

# Use python-dotenv for local development to load from a .env file
from dotenv import load_dotenv
load_dotenv()

# --- SDK and Auth Imports ---
from google import genai
from google.genai import types
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from cachetools import TTLCache
import firebase_admin
from firebase_admin import credentials, firestore

# --- Standalone Helper Function to avoid 'self' confusion ---
# vinny/main.py

async def extract_facts_from_message(bot_instance, user_message: str):
    """
    Analyzes a user message to extract personal facts using the bot's Gemini client.
    This is now a standalone function to prevent 'self' scope issues.
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
    except Exception as e:
        sys.stderr.write(f"ERROR: Fact extraction from message failed: {e}\n")
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
            sys.exit("Error: Essential environment variables (DISCORD_BOT_TOKEN, GEMINI_API_KEY) are not set.")

        # --- Initialize API Clients ---
        print("Initializing API clients...")
        self.gemini_client = genai.Client(api_key=self.GEMINI_API_KEY)
        self.http_session = None
        self.db = None
        self.current_user_id = None 

        # --- Load Personality ---
        try:
            with open('personality.txt', 'r', encoding='utf-8') as f:
                self.personality_instruction = f.read()
            print("Personality prompt loaded.")
        except FileNotFoundError:
            sys.exit("Error: personality.txt not found. Please create it.")

        # --- Bot State & Globals ---
        self.MODEL_NAME = "gemini-2.5-flash"
        self.processed_message_ids = TTLCache(maxsize=1024, ttl=60)
        self.channel_locks = {}
        self.MAX_CHAT_HISTORY_LENGTH = 10
        
        # --- Persona & Autonomous Mode ---
        self.MOODS = ["cranky", "depressed", "artistic", "cheerful", "drunkenly profound", "suspicious", "flirty"]
        self.current_mood = random.choice(self.MOODS)
        self.last_mood_change_time = datetime.datetime.now()
        self.MOOD_CHANGE_INTERVAL = datetime.timedelta(hours=3)
        self.PASSIVE_LEARNING_ENABLED = True
        self.autonomous_mode_enabled = False
        self.autonomous_reply_chance = 0.05
        self.reaction_chance = 0.15
        
        # --- Centralized Safety Settings for Text Generation ---
        self.GEMINI_SAFETY_SETTINGS_TEXT_ONLY = [
            types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for cat in [
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT
            ]
        ]
        self.GEMINI_TEXT_CONFIG = types.GenerateContentConfig(safety_settings=self.GEMINI_SAFETY_SETTINGS_TEXT_ONLY)
        
        # --- Rate Limiting ---
        self.TEXT_GENERATION_LIMIT = 1490
        self.SEARCH_GROUNDING_LIMIT = 490
        self.API_CALL_COUNTS = {
            "date": str(datetime.date.today()),
            "text_generation": 0,
            "search_grounding": 0,
        }

    async def setup_hook(self):
        """This is called once when the bot logs in."""
        self.http_session = aiohttp.ClientSession()
        
        await self.initialize_firebase()
        if self.db:
            await self.initialize_rate_limiter()

        print("Loading cogs...")
        await self.load_extension("cogs.vinny_logic")
        print("Cogs loaded successfully.")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    async def process_commands(self, message):
        """This function processes commands."""
        ctx = await self.get_context(message, cls=commands.Context)
        if ctx.command:
            await self.invoke(ctx)
            
    async def on_error(self, event, *args, **kwargs):
        """Catch unhandled errors."""
        _, exc, _ = sys.exc_info()
        sys.stderr.write(f"Unhandled error in {event}: {exc}\n")
        import traceback
        traceback.print_exc()

    async def close(self):
        """Called when the bot is shutting down."""
        await super().close()
        if self.http_session:
            await self.http_session.close()

    async def initialize_firebase(self):
        if not self.FIREBASE_B64:
            print("Warning: GOOGLE_APPLICATION_CREDENTIALS_BASE64 not set. Firebase disabled.")
            self.db = None
            return False
        if firebase_admin._apps:
            self.db = firestore.client()
            return True
        try:
            service_account_info = json.loads(base64.b64decode(self.FIREBASE_B64).decode('utf-8'))
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            print("Firebase initialized successfully.")
            return True
        except Exception as e:
            print(f"Error initializing Firebase: {e}")
            self.db = None
            return False

    async def generate_image_with_imagen(self, prompt: str) -> io.BytesIO | None:
        if not self.GCP_PROJECT_ID or not self.FIREBASE_B64: return None
        token = None
        try:
            service_account_info = json.loads(base64.b64decode(self.FIREBASE_B64).decode('utf-8'))
            creds = service_account.Credentials.from_service_account_info(service_account_info)
            scoped_creds = creds.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])
            await self.loop.run_in_executor(None, lambda: scoped_creds.refresh(Request()))
            token = scoped_creds.token
        except Exception as e: return None

        gcp_region = "us-central1"
        api_url = f"https://{gcp_region}-aiplatform.googleapis.com/v1/projects/{self.GCP_PROJECT_ID}/locations/{gcp_region}/publishers/google/models/imagegeneration@006:predict"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        data = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}}

        try:
            async with self.http_session.post(api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("predictions") and "bytesBase64Encoded" in result["predictions"][0]:
                        return io.BytesIO(base64.b64decode(result["predictions"][0]["bytesBase64Encoded"]))
        except Exception as e: pass
        return None
    
    async def add_doc_to_firestore(self, collection_ref, data):
        if not self.db: return None
        try:
            _, doc_ref = await self.loop.run_in_executor(None, lambda: collection_ref.add(data))
            return {"id": doc_ref.id}
        except Exception as e: return None

    async def get_docs_from_firestore(self, collection_ref):
        if not self.db: return []
        try:
            return [doc.to_dict() for doc in await self.loop.run_in_executor(None, collection_ref.stream)]
        except Exception as e: return []

    async def delete_docs_from_firestore(self, collection_ref):
        if not self.db: return False
        try:
            for doc_ref in [doc.reference for doc in await self.loop.run_in_executor(None, collection_ref.stream)]:
                await self.loop.run_in_executor(None, doc_ref.delete)
            return True
        except Exception as e: return False

    async def save_user_nickname(self, user_id: str, nickname: str):
        if not self.db: return False
        try:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/user_profile").document('details')
            await self.loop.run_in_executor(None, lambda: profile_ref.set({'nickname': nickname}, merge=True))
            return True
        except Exception as e: return False

    async def get_user_nickname(self, user_id: str) -> str | None:
        if not self.db: return None
        try:
            doc = await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/user_profile").document('details').get)
            return doc.to_dict().get('nickname') if doc.exists else None
        except Exception as e: return None

    async def find_user_by_vinny_name(self, guild, target_name: str):
        if not self.db: return None
        for member in guild.members:
            if (await self.get_user_nickname(str(member.id)) or "").lower() == target_name.lower():
                return member
        return None
    
    async def geocode_location(self, location: str):
        if not self.OPENWEATHER_API_KEY: return None
        params = {"limit": 1, "appid": self.OPENWEATHER_API_KEY}
        base_url, params["zip" if location.isdigit() and len(location) == 5 else "q"] = ("http://api.openweathermap.org/geo/1.0/zip", f"{location},US") if location.isdigit() and len(location) == 5 else ("http://api.openweathermap.org/geo/1.0/direct", location)
        try:
            async with self.http_session.get(base_url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    res = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
                    if res and "lat" in res and "lon" in res and "name" in res: return res
        except Exception as e: pass
        return None

    async def get_weather_data(self, lat: float, lon: float):
        if not self.OPENWEATHER_API_KEY: return None
        try:
            async with self.http_session.get("http://api.openweathermap.org/data/2.5/weather", params={"lat": lat, "lon": lon, "appid": self.OPENWEATHER_API_KEY, "units": "imperial"}) as response:
                if response.status == 200: return await response.json()
        except Exception as e: pass
        return None

    def get_weather_emoji(self, weather_main: str):
        weather_main = weather_main.lower()
        if "clear" in weather_main: return "☀️"
        elif "clouds" in weather_main: return "☁️"
        elif "rain" in weather_main or "drizzle" in weather_main: return "🌧️"
        elif "thunderstorm" in weather_main: return "⛈️"
        elif "snow" in weather_main: return "❄️"
        elif "mist" in weather_main or "fog" in weather_main or "haze" in weather_main: return "🌫️"
        else: return "🌎"

#fix for merge bug
    async def save_user_profile_fact(self, user_id: str, guild_id: str | None, key: str, value: str):
        if not self.db: 
            sys.stderr.write("ERROR: Firestore database not initialized. Cannot save fact.\n")
            return False
            
        key = key.lower().replace(' ', '_')
        path = f"artifacts/{self.APP_ID}/servers/{guild_id}/user_profiles" if guild_id else f"artifacts/{self.APP_ID}/global_user_profiles"
        data_to_save = {key: value}

        sys.stderr.write(f"DEBUG: Attempting to save to Firestore. Path: '{path}', UserID: '{user_id}', Data: {data_to_save}\n")

        try:
            profile_ref = self.db.collection(path).document(user_id)
            # FIX: Wrap the database call in a lambda to correctly pass the 'merge' argument
            await self.loop.run_in_executor(None, lambda: profile_ref.set(data_to_save, merge=True))
            sys.stderr.write("DEBUG: Firestore save successful.\n")
            return True
        except Exception as e:
            # This will print the exact, detailed error to your Render logs
            sys.stderr.write(f"CRITICAL SAVE FAILURE: An exception occurred while saving to Firestore.\n")
            sys.stderr.write(f"--> Path: {path}\n")
            sys.stderr.write(f"--> UserID: {user_id}\n")
            sys.stderr.write(f"--> Data: {data_to_save}\n")
            sys.stderr.write(f"--> Exception Type: {type(e).__name__}\n")
            sys.stderr.write(f"--> Exception Details: {e}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)
            return False

    async def get_user_profile(self, user_id: str, guild_id: str | None):
        if not self.db: return {}
        global_profile, server_profile = {}, {}
        try:
            doc = await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/global_user_profiles").document(user_id).get)
            if doc.exists: global_profile = doc.to_dict()
        except Exception as e: pass
        if guild_id:
            try:
                doc = await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_profiles").document(user_id).get)
                if doc.exists: server_profile = doc.to_dict()
            except Exception as e: pass
        return global_profile | server_profile

    async def delete_user_profile(self, user_id: str, guild_id: str):
        if not self.db: return False
        try:
            await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_profiles").document(user_id).delete)
            return True
        except Exception as e: return False

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

    async def initialize_rate_limiter(self):
        if not self.db: return
        rate_limit_ref = self.db.collection(f"artifacts/{self.APP_ID}/bot_state").document('rate_limit')
        today_str = str(datetime.date.today())
        try:
            doc = await self.loop.run_in_executor(None, rate_limit_ref.get)
            if doc.exists and doc.to_dict().get('date') == today_str:
                self.API_CALL_COUNTS.update(doc.to_dict())
            else: await self.loop.run_in_executor(None, lambda: rate_limit_ref.set(self.API_CALL_COUNTS))
        except Exception as e: pass

    async def update_api_count_in_firestore(self):
        if not self.db: return
        try:
            await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/bot_state").document('rate_limit').update, self.API_CALL_COUNTS)
        except Exception as e: pass

    async def generate_memory_summary(self, messages):
        if not messages or not self.db: return None
        summary_instruction = ("you are a conversation summarization assistant...")
        summary_prompt = f"{summary_instruction}\n\n...conversation:\n" + "\n".join([f"{msg['author']}: {msg['content']}" for msg in messages])
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=self.MODEL_NAME, contents=[types.Content(parts=[types.Part(text=summary_prompt)])], config=self.GEMINI_TEXT_CONFIG
            )
            if response.text:
                parts = response.text.split("summary:", 1)[1].split("keywords:", 1)
                return {"summary": parts[0].strip(), "keywords": [k.strip() for k in parts[1].strip('[]').split(',') if k.strip()]}
        except Exception as e: pass
        return None

    async def save_memory(self, guild_id: str, summary_data: dict):
        if not self.db: return
        path = f"artifacts/{self.APP_ID}/servers/{guild_id}/summaries"
        doc_data = {"timestamp": datetime.datetime.now(datetime.UTC), "summary": summary_data.get("summary", ""), "keywords": summary_data.get("keywords", [])}
        await self.add_doc_to_firestore(self.db.collection(path), doc_data)

    async def retrieve_general_memories(self, guild_id: str, query_keywords: list, limit: int = 2):
        if not self.db: return []
        docs = await self.get_docs_from_firestore(self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/summaries"))
        relevant = [doc for doc in docs if any(qk.lower() in (dk.lower() for dk in doc.get("keywords", [])) or qk.lower() in doc.get("summary", "").lower() for qk in query_keywords)]
        return sorted(relevant, key=lambda x: x.get('timestamp', ''), reverse=True)[:limit]

    async def save_proposal(self, proposer_id: str, recipient_id: str):
        if not self.db: return False
        try:
            await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/global_proposals").document(f"{proposer_id}_to_{recipient_id}").set, {"proposer_id": proposer_id, "recipient_id": recipient_id, "timestamp": datetime.datetime.now(datetime.UTC)})
            return True
        except Exception: return False

    async def check_proposal(self, proposer_id: str, recipient_id: str):
        if not self.db: return None
        try:
            doc = await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/global_proposals").document(f"{proposer_id}_to_{recipient_id}").get)
            if doc.exists and (datetime.datetime.now(datetime.UTC) - doc.to_dict().get("timestamp")) < datetime.timedelta(minutes=5): return doc.to_dict()
        except Exception: pass
        return None

    async def finalize_marriage(self, user1_id: str, user2_id: str):
        if not self.db: return False
        try:
            date = datetime.datetime.now(datetime.UTC).astimezone(ZoneInfo("America/New_York")).strftime("%B %d, %Y")
            await self.save_user_profile_fact(user1_id, None, "married_to", user2_id)
            await self.save_user_profile_fact(user1_id, None, "marriage_date", date)
            await self.save_user_profile_fact(user2_id, None, "married_to", user1_id)
            await self.save_user_profile_fact(user2_id, None, "marriage_date", date)
            await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/global_proposals").document(f"{user1_id}_to_{user2_id}").delete)
            return True
        except Exception: return False

    async def process_divorce(self, user1_id: str, user2_id: str):
        if not self.db: return False
        try:
            await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/global_user_profiles").document(user1_id).update, {"married_to": firestore.DELETE_FIELD, "marriage_date": firestore.DELETE_FIELD})
            await self.loop.run_in_executor(None, self.db.collection(f"artifacts/{self.APP_ID}/global_user_profiles").document(user2_id).update, {"married_to": firestore.DELETE_FIELD, "marriage_date": firestore.DELETE_FIELD})
            return True
        except Exception: return False


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
            sys.stderr.write("ERROR: invalid discord bot token.\n")
        except Exception as e:
            sys.stderr.write(f"CRITICAL ERROR DURING BOT STARTUP: {type(e).__name__}: {e}\n")
    else:
        print("FATAL: DISCORD_BOT_TOKEN not found in environment.")