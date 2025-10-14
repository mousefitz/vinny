from google.genai import types

# --- API Limits ---

TEXT_GENERATION_LIMIT = 1490
SEARCH_GROUNDING_LIMIT = 490

# --- Bot Persona ---

MOODS = ["cranky", "depressed", "horny", "belligerent", "artistic", "cheerful", "drunkenly profound", "suspicious", "flirty", "nostalgic", "mischievous"]

# --- Gemini Configuration ---

GEMINI_SAFETY_SETTINGS_TEXT_ONLY = [
    types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for cat in [
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT
    ]
]

# --- Firestore Path Generators ---

def get_user_profile_collection_path(app_id: str, guild_id: str | None) -> str:
    """Returns the path for server-specific or global user profiles."""
    if guild_id:
        return f"artifacts/{app_id}/servers/{guild_id}/user_profiles"
    return f"artifacts/{app_id}/global_user_profiles"

def get_summaries_collection_path(app_id: str, guild_id: str) -> str:
    """Returns the path for conversation summaries for a specific server."""
    return f"artifacts/{app_id}/servers/{guild_id}/summaries"

def get_proposals_collection_path(app_id: str) -> str:
    """Returns the path for global marriage proposals."""
    return f"artifacts/{app_id}/global_proposals"
    
def get_bot_state_collection_path(app_id: str) -> str:
    """Returns the path for storing bot state like rate limits."""
    return f"artifacts/{app_id}/bot_state"

def get_global_user_profiles_path(app_id: str) -> str:
    """Returns the path for global user profiles (for marriages, etc)."""
    return f"artifacts/{app_id}/global_user_profiles"
    
def get_user_details_path(app_id: str, user_id: str) -> str:
    """Returns the path for a user's nickname details."""
    return f"artifacts/{app_id}/users/{user_id}/user_profile"

# --- Horoscope Emojis ---

SIGN_EMOJIS = {
    "aries": "♈", "taurus": "♉", "gemini": "♊", "cancer": "♋", 
    "leo": "♌", "virgo": "♍", "libra": "♎", "scorpio": "♏", 
    "sagittarius": "♐", "capricorn": "♑", "aquarius": "♒", "pisces": "♓"
}

# --- Weather Emojis ---

def get_weather_emoji(weather_main: str):
    weather_main = weather_main.lower()
    if "clear" in weather_main: return "☀️"
    elif "clouds" in weather_main: return "☁️"
    elif "rain" in weather_main or "drizzle" in weather_main: return "🌧️"
    elif "thunderstorm" in weather_main: return "⛈️"
    elif "snow" in weather_main: return "❄️"
    elif "mist" in weather_main or "fog" in weather_main or "haze" in weather_main: return "🌫️"
    else: return "🌎"