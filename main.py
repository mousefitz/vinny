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
        self.PROACTIVE_MEMORY_CHANCE = 0.15
        self.autonomous_mode_enabled = False
        self.autonomous_reply_chance = 0.05
        self.reaction_chance = 0.15
        
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

    # --- ALL HELPER FUNCTIONS ARE NOW METHODS OF THE BOT ---

    async def initialize_firebase(self):
        if not self.FIREBASE_B64:
            print("Warning: GOOGLE_APPLICATION_CREDENTIALS_BASE64 not set. Firebase disabled.")
            self.db = None
            self.current_user_id = "simulated_user_id"
            return False
        if firebase_admin._apps:
            print("Firebase app already initialized.")
            self.db = firestore.client()
            self.current_user_id = "vinny_bot_id"
            return True
        try:
            service_account_info = json.loads(base64.b64decode(self.FIREBASE_B64).decode('utf-8'))
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            self.current_user_id = "vinny_bot_id"
            print("Firebase initialized successfully.")
            return True
        except Exception as e:
            print(f"Error initializing Firebase: {e}")
            self.db = None
            self.current_user_id = "simulated_user_id"
            return False

    async def generate_image_with_imagen(self, prompt: str) -> io.BytesIO | None:
        if not self.GCP_PROJECT_ID or not self.FIREBASE_B64:
            sys.stderr.write("ERROR: GCP_PROJECT_ID or GOOGLE_APPLICATION_CREDENTIALS_BASE64 is not set. Cannot generate images.\n")
            return None
        
        token = None
        try:
            service_account_info = json.loads(base64.b64decode(self.FIREBASE_B64).decode('utf-8'))
            creds = service_account.Credentials.from_service_account_info(service_account_info)
            scoped_creds = creds.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])
            await self.loop.run_in_executor(None, lambda: scoped_creds.refresh(Request()))
            token = scoped_creds.token
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to get auth token from service account: {e}\n")
            return None

        gcp_region = "us-central1"
        api_url = f"https://{gcp_region}-aiplatform.googleapis.com/v1/projects/{self.GCP_PROJECT_ID}/locations/{gcp_region}/publishers/google/models/imagegeneration@006:predict"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        data = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}}

        sys.stderr.write(f"DEBUG: Calling Vertex AI Imagen API with prompt: '{prompt}'\n")
        try:
            async with self.http_session.post(api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    result = await response.json()
                    predictions = result.get("predictions")
                    if predictions and "bytesBase64Encoded" in predictions[0]:
                        return io.BytesIO(base64.b64decode(predictions[0]["bytesBase64Encoded"]))
                    else:
                        sys.stderr.write(f"ERROR: No image data in Vertex AI response: {result}\n")
                else:
                    error_text = await response.text()
                    sys.stderr.write(f"ERROR: Vertex AI Imagen API failed with status {response.status}: {error_text}\n")
        except Exception as e:
            sys.stderr.write(f"ERROR: An unexpected error occurred during image generation: {e}\n")
        return None
    
    async def add_doc_to_firestore(self, collection_ref, data):
        if not self.db or not collection_ref: return None
        try:
            _update_time, doc_ref = await self.loop.run_in_executor(None, lambda: collection_ref.add(data))
            return {"id": doc_ref.id}
        except Exception as e:
            sys.stderr.write(f"ERROR: Error adding document to Firestore: {e}\n")
            return None

    async def get_docs_from_firestore(self, collection_ref):
        if not self.db or not collection_ref: return []
        try:
            docs_iterator = await self.loop.run_in_executor(None, collection_ref.stream)
            return [doc.to_dict() for doc in docs_iterator]
        except Exception as e:
            sys.stderr.write(f"ERROR: Error getting documents from Firestore: {e}\n")
            return []

    async def delete_docs_from_firestore(self, collection_ref):
        if not self.db or not collection_ref: return False
        try:
            docs_iterator = await self.loop.run_in_executor(None, collection_ref.stream)
            for doc_ref in [doc.reference for doc in docs_iterator]:
                await self.loop.run_in_executor(None, doc_ref.delete)
            return True
        except Exception as e:
            sys.stderr.write(f"ERROR: Error deleting documents from Firestore: {e}\n")
            return False

    async def save_user_nickname(self, user_id: str, nickname: str):
        if not self.db: return False
        try:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/user_profile").document('details')
            await self.loop.run_in_executor(None, lambda: profile_ref.set({'nickname': nickname}, merge=True))
            return True
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to save nickname for user {user_id}: {e}\n")
            return False

    async def get_user_nickname(self, user_id: str) -> str | None:
        if not self.db: return None
        try:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/user_profile").document('details')
            doc = await self.loop.run_in_executor(None, profile_ref.get)
            if doc.exists:
                return doc.to_dict().get('nickname')
            return None
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to retrieve nickname for user {user_id}: {e}\n")
            return None

    async def find_user_by_vinny_name(self, guild, target_name: str):
        if not self.db: return None
        lower_target_name = target_name.lower()
        for member in guild.members:
            user_id = str(member.id)
            stored_nickname = await self.get_user_nickname(user_id)
            if stored_nickname and stored_nickname.lower() == lower_target_name:
                return member
        return None
    
    async def geocode_location(self, location: str):
        if not self.OPENWEATHER_API_KEY:
            sys.stderr.write("ERROR: OPENWEATHER_API_KEY is not set.\n")
            return None

        params = {"limit": 1, "appid": self.OPENWEATHER_API_KEY}
        if location.isdigit() and len(location) == 5:
            base_url = "http://api.openweathermap.org/geo/1.0/zip"
            params["zip"] = f"{location},US"
            sys.stderr.write(f"DEBUG: Using zip code endpoint for location: {location}\n")
        else:
            base_url = "http://api.openweathermap.org/geo/1.0/direct"
            params["q"] = location
            sys.stderr.write(f"DEBUG: Using direct endpoint for location: {location}\n")
        
        try:
            async with self.http_session.get(base_url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and (isinstance(data, list) and len(data) > 0 or isinstance(data, dict)):
                        res = data[0] if isinstance(data, list) else data
                        if "lat" in res and "lon" in res and "name" in res:
                            return {"lat": res["lat"], "lon": res["lon"], "name": res["name"]}
                        else:
                            sys.stderr.write(f"ERROR: Geocoding response for '{location}' missing key data.\n")
                else:
                    sys.stderr.write(f"ERROR: Geocoding API failed with status {response.status} for location '{location}'.\n")
        except Exception as e:
            sys.stderr.write(f"ERROR: An unexpected error occurred during geocoding: {e}\n")
        return None

    async def get_weather_data(self, lat: float, lon: float):
        if not self.OPENWEATHER_API_KEY: return None
        base_url = "http://api.openweathermap.org/data/2.5/weather"
        params = {"lat": lat, "lon": lon, "appid": self.OPENWEATHER_API_KEY, "units": "imperial"}

        try:
            async with self.http_session.get(base_url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    sys.stderr.write(f"ERROR: OpenWeatherMap API failed with status {response.status}\n")
        except Exception as e:
            sys.stderr.write(f"ERROR: An unexpected error while fetching weather data: {e}\n")
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

    async def save_user_profile_fact(self, user_id: str, guild_id: str | None, key: str, value: str):
        if not self.db: return False
        key = key.lower().replace(' ', '_')
        if guild_id:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_profiles").document(user_id)
        else:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/dm_profile").document('details')
        try:
            await self.loop.run_in_executor(None, lambda: profile_ref.set({key: value}, merge=True))
            return True
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to save profile fact for user {user_id}: {e}\n")
            return False

    async def get_user_profile(self, user_id: str, guild_id: str | None):
        if not self.db: return None
        if guild_id:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_profiles").document(user_id)
        else:
            profile_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/dm_profile").document('details')
        try:
            doc = await self.loop.run_in_executor(None, profile_ref.get)
            return doc.to_dict() if doc.exists else None
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to retrieve profile for user {user_id}: {e}\n")
            return None

    async def extract_facts_from_message(self, user_message: str):
        fact_extraction_prompt = (
            "You are a highly accurate fact-extraction system... (prompt as before)"
        )
        # ** THE FIX IS HERE **
        # Define safety settings for TEXT ONLY
        text_safety_settings = [
            types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for cat in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]
        ]
        config = types.GenerateContentConfig(safety_settings=text_safety_settings)
        try:
            response = await self.gemini_client.aio.models.generate_content(
                model=self.MODEL_NAME, contents=[types.Content(role='user', parts=[types.Part(text=fact_extraction_prompt)])], config=config
            )
            raw_text = response.text.strip()
            json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if json_match:
                json_string = json_match.group(0)
                return json.loads(json_string)
        except Exception as e:
            sys.stderr.write(f"ERROR: Fact extraction from message failed: {e}\n")
        return None

    def split_message(self, content, char_limit=1900):
        if len(content) <= char_limit: return [content]
        chunks, current_chunk = [], ""
        sentences = content.replace('\n', ' SENTENCE_BREAK ').replace('. ', '. SENTENCE_BREAK ').replace('! ', '! SENTENCE_BREAK ').replace('? ', '? SENTENCE_BREAK ').split('SENTENCE_BREAK')
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence: continue
            if len(current_chunk) + len(sentence) + 1 > char_limit:
                if current_chunk: chunks.append(current_chunk)
                current_chunk = sentence
            else:
                current_chunk += (" " + sentence) if current_chunk else sentence
        if current_chunk: chunks.append(current_chunk)
        final_chunks = []
        for chunk in chunks:
            if len(chunk) > char_limit:
                words = chunk.split(' ')
                new_chunk = ""
                for word in words:
                    if len(new_chunk) + len(word) + 1 > char_limit:
                        final_chunks.append(new_chunk)
                        new_chunk = word
                    else:
                        new_chunk += (" " + word) if new_chunk else word
                if new_chunk: final_chunks.append(new_chunk)
            else:
                final_chunks.append(chunk)
        return final_chunks

    async def initialize_rate_limiter(self):
        if not self.db: return
        rate_limit_ref = self.db.collection(f"artifacts/{self.APP_ID}/bot_state").document('rate_limit')
        today_str = str(datetime.date.today())
        try:
            doc = await self.loop.run_in_executor(None, rate_limit_ref.get)
            if doc.exists and doc.to_dict().get('date') == today_str:
                data = doc.to_dict()
                self.API_CALL_COUNTS['text_generation'] = data.get('text_generation', 0)
                self.API_CALL_COUNTS['search_grounding'] = data.get('search_grounding', 0)
                print(f"Rate limiter initialized. Count for today: {self.API_CALL_COUNTS['text_generation']}")
            else:
                await self.loop.run_in_executor(None, lambda: rate_limit_ref.set(self.API_CALL_COUNTS))
                print("New day detected. Rate limit counter reset in Firestore.")
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to initialize persistent rate limiter: {e}\n")

    async def update_api_count_in_firestore(self):
        if not self.db: return
        rate_limit_ref = self.db.collection(f"artifacts/{self.APP_ID}/bot_state").document('rate_limit')
        try:
            await self.loop.run_in_executor(None, lambda: rate_limit_ref.update({
                "text_generation": self.API_CALL_COUNTS["text_generation"],
                "search_grounding": self.API_CALL_COUNTS["search_grounding"]
            }))
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to update rate limit count in Firestore: {e}\n")

    async def generate_memory_summary(self, messages):
        if not messages or not self.db: return None
        conversation_text = "\n".join([f"{msg['author']}: {msg['content']}" for msg in messages])
        summary_instruction = ("you are a conversation summarization assistant...")
        summary_prompt = (f"{summary_instruction}\n\n...conversation:\n{conversation_text}")
        try:
            contents = [types.Content(role='user', parts=[types.Part(text=summary_instruction)]),
                        types.Content(role='model', parts=[types.Part(text="aight, i get it. i'll summarize the slop.")]),
                        types.Content(role='user', parts=[types.Part(text=summary_prompt)])]
            
            # ** THE FIX IS HERE **
            # Define safety settings for TEXT ONLY
            text_safety_settings = [
                types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
                for cat in [types.HarmCategory.HARM_CATEGORY_HARASSMENT, types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT]
            ]
            config = types.GenerateContentConfig(safety_settings=text_safety_settings)
            
            response = await self.gemini_client.aio.models.generate_content(model=self.MODEL_NAME, contents=contents, config=config)
            
            if response.candidates and response.candidates[0].content.parts:
                raw_summary = response.candidates[0].content.parts[0].text
                summary_part = ""
                keywords_part = []
                if "summary:" in raw_summary:
                    parts = raw_summary.split("summary:", 1)[1].split("keywords:", 1)
                    summary_part = parts[0].strip()
                    if len(parts) > 1:
                        keywords_part = [k.strip() for k in parts[1].strip().replace('[', '').replace(']', '').split(',') if k.strip()]
                return {"summary": summary_part, "keywords": keywords_part, "raw_conversation": conversation_text}
            return None
        except Exception as e:
            sys.stderr.write(f"ERROR: error generating memory summary: {e}\n")
            return None

    async def save_memory(self, guild_id: str | None, summary_data: dict, context_id: str):
        if not self.db: return
        if guild_id:
            path = f"artifacts/{self.APP_ID}/servers/{guild_id}/summaries"
        else:
            path = f"artifacts/{self.APP_ID}/users/{context_id}/summaries"
        memories_collection_ref = self.db.collection(path)
        doc_data = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "context_id": context_id,
            "summary": summary_data.get("summary", ""),
            "keywords": summary_data.get("keywords", []),
            "raw_text": summary_data.get("raw_conversation", "")
        }
        await self.add_doc_to_firestore(memories_collection_ref, doc_data)

    async def save_explicit_memory(self, user_id: str, guild_id: str | None, channel_id: str, memory_text: str):
        if not self.db: return False
        if guild_id:
            mem_ref = self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_memories").document(user_id).collection("items")
        else:
            mem_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/dm_memories")
        doc_data = {"timestamp": datetime.datetime.now(datetime.UTC).isoformat(), "channel_id": channel_id, "memory_text": memory_text}
        result = await self.add_doc_to_firestore(mem_ref, doc_data)
        return result is not None

    async def retrieve_explicit_memories(self, user_id: str, guild_id: str | None, query_topic: str):
        if not self.db: return "vinny's brain ain't workin'..."
        if guild_id:
            mem_ref = self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_memories").document(user_id).collection("items")
        else:
            mem_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/dm_memories")

        try:
            all_docs = await self.get_docs_from_firestore(mem_ref)
            retrieved_docs = [doc for doc in all_docs if query_topic.lower() in doc.get("memory_text", "").lower()]
            if not retrieved_docs: return f"nah, {query_topic}? doesn't ring a bell..."
            response_parts = [f"oh, {query_topic}, eh? vinny remembers..."]
            response_parts.extend([f"- {doc.get('memory_text', '...')}" for doc in retrieved_docs])
            return "\n".join(response_parts)
        except Exception as e:
            sys.stderr.write(f"ERROR: Error retrieving explicit memories for user {user_id}: {e}\n")
            return "ugh, my head hurts..."

    async def retrieve_general_memories(self, guild_id: str | None, user_id: str | None, query_keywords: list, limit: int = 2):
        if not self.db: return []
        if guild_id:
            path = f"artifacts/{self.APP_ID}/servers/{guild_id}/summaries"
        elif user_id:
            path = f"artifacts/{self.APP_ID}/users/{user_id}/summaries"
        else:
            return []
        
        mem_ref = self.db.collection(path)
        try:
            all_docs = await self.get_docs_from_firestore(mem_ref)
            relevant_memories = []
            for doc in all_docs:
                doc_keywords = doc.get("keywords", [])
                summary = doc.get("summary", "")
                if any(qk.lower() in [dk.lower() for dk in doc_keywords] or qk.lower() in summary.lower() for qk in query_keywords):
                    relevant_memories.append(doc)
            relevant_memories.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return relevant_memories[:limit]
        except Exception as e:
            sys.stderr.write(f"ERROR: Error retrieving general memories: {e}\n")
            return []

    async def get_random_explicit_memory(self, user_id: str, guild_id: str | None):
        if not self.db: return None
        if guild_id:
            mem_ref = self.db.collection(f"artifacts/{self.APP_ID}/servers/{guild_id}/user_memories").document(user_id).collection("items")
        else:
            mem_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user_id}/dm_memories")
        try:
            all_docs = await self.get_docs_from_firestore(mem_ref)
            if all_docs:
                return random.choice(all_docs).get("memory_text")
            return None
        except Exception as e:
            sys.stderr.write(f"ERROR: Error retrieving a random memory for user {user_id}: {e}\n")
            return None

    # --- NEW: Marriage Helper Functions ---

    async def save_proposal(self, proposer_id: str, recipient_id: str):
        """Saves a marriage proposal to a global location in Firestore."""
        if not self.db: return False
        try:
            # This path is now global, not tied to a server
            proposal_ref = self.db.collection(f"artifacts/{self.APP_ID}/global_proposals").document(f"{proposer_id}_to_{recipient_id}")
            proposal_data = {
                "proposer_id": proposer_id,
                "recipient_id": recipient_id,
                "timestamp": datetime.datetime.now(datetime.UTC)
            }
            await self.loop.run_in_executor(None, lambda: proposal_ref.set(proposal_data))
            return True
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to save proposal: {e}\n")
            return False

    async def check_proposal(self, proposer_id: str, recipient_id: str):
        """Checks for a valid, recent global proposal."""
        if not self.db: return None
        try:
            # This path is now global
            proposal_ref = self.db.collection(f"artifacts/{self.APP_ID}/global_proposals").document(f"{proposer_id}_to_{recipient_id}")
            doc = await self.loop.run_in_executor(None, proposal_ref.get)
            if doc.exists:
                proposal_data = doc.to_dict()
                proposal_time = proposal_data.get("timestamp")
                if datetime.datetime.now(datetime.UTC) - proposal_time < datetime.timedelta(minutes=5):
                    return proposal_data
            return None
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to check proposal: {e}\n")
            return None

    async def finalize_marriage(self, user1_id: str, user2_id: str):
        """Updates both user global profiles to set them as married."""
        if not self.db: return False
        try:
            utc_now = datetime.datetime.now(datetime.UTC)
            local_now = utc_now.astimezone(ZoneInfo("America/New_York"))
            marriage_date = local_now.strftime("%B %d, %Y")
            # We now save to the global user profile (guild_id is None)
            await self.save_user_profile_fact(user1_id, None, "married_to", user2_id)
            await self.save_user_profile_fact(user1_id, None, "marriage_date", marriage_date)
            await self.save_user_profile_fact(user2_id, None, "married_to", user1_id)
            await self.save_user_profile_fact(user2_id, None, "marriage_date", marriage_date)
            
            # Clean up the global proposal
            proposal_ref = self.db.collection(f"artifacts/{self.APP_ID}/global_proposals").document(f"{user1_id}_to_{user2_id}")
            await self.loop.run_in_executor(None, proposal_ref.delete)
            return True
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to finalize marriage: {e}\n")
            return False

    async def process_divorce(self, user1_id: str, user2_id: str):
        """Removes marriage info from both user global profiles."""
        if not self.db: return False
        try:
            from google.cloud.firestore_v1.field_path import FieldPath
            
            # Delete fields from user1's global profile (guild_id is None)
            profile1_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user1_id}/dm_profile").document('details')
            await self.loop.run_in_executor(None, lambda: profile1_ref.update({"married_to": firestore.DELETE_FIELD, "marriage_date": firestore.DELETE_FIELD}))
            
            # Delete fields from user2's global profile (guild_id is None)
            profile2_ref = self.db.collection(f"artifacts/{self.APP_ID}/users/{user2_id}/dm_profile").document('details')
            await self.loop.run_in_executor(None, lambda: profile2_ref.update({"married_to": firestore.DELETE_FIELD, "marriage_date": firestore.DELETE_FIELD}))
            
            return True
        except Exception as e:
            sys.stderr.write(f"ERROR: Failed to process divorce: {e}\n")
            return False

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