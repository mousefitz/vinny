import discord
from google.genai import types

# --- API Limits ---

TEXT_GENERATION_LIMIT = 1490
SEARCH_GROUNDING_LIMIT = 490

# --- Bot Persona ---

MOODS = ["cranky", "depressed", "horny", "belligerent", "artistic", "cheerful", "drunkenly profound", "suspicious", "flirty", "nostalgic", "mischievous"]

# --- Relationship Thresholds (Centralized) ---

RELATIONSHIP_THRESHOLDS = [
    (500, "obsessed", discord.Color.from_rgb(255, 105, 180)),
    (200, "soulmate", discord.Color.from_rgb(255, 215, 0)),
    (100, "family", discord.Color.purple()),
    (60, "bestie", discord.Color.dark_purple()),
    (25, "friend", discord.Color.green()),
    (10, "chill", discord.Color.teal()),
    (-10, "neutral", discord.Color.dark_magenta()), # -10 to 9
    (-25, "annoyance", discord.Color.orange()),     # -11 to -25
    (-60, "sketchy", discord.Color.dark_orange()),  # -26 to -59
    (-100, "enemy", discord.Color.red()),           # -60 to -99
    (-200, "nemesis", discord.Color.dark_red()),    # -100 to -199
    (-500, "arch-nemesis", discord.Color.from_rgb(50, 0, 0)), # -200 to -499
    (float('-inf'), "dead to me", discord.Color.default())    # -500 and below
]

def get_relationship_status(score):
    """Returns (status_name, color) for a given score."""
    for threshold, name, color in RELATIONSHIP_THRESHOLDS:
        if score >= threshold:
            return name, color
    # Fallback (should normally be caught by -inf)
    return "dead to me", discord.Color.default()

# --- Gemini Configuration ---

# UPDATED: Use "OFF" for Gemini 2.5 Flash compatibility
GEMINI_SAFETY_SETTINGS_TEXT_ONLY = [
    types.SafetySetting(
        category=cat, threshold="OFF"
    )
    for cat in [
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT
    ]
]

# --- Firestore Path Generators ---

def get_user_profile_collection_path(app_id: str, guild_id: str | None) -> str:
    if guild_id:
        return f"artifacts/{app_id}/servers/{guild_id}/user_profiles"
    return f"artifacts/{app_id}/global_user_profiles"

def get_summaries_collection_path(app_id: str, guild_id: str) -> str:
    return f"artifacts/{app_id}/servers/{guild_id}/summaries"

def get_proposals_collection_path(app_id: str) -> str:
    return f"artifacts/{app_id}/global_proposals"
    
def get_bot_state_collection_path(app_id: str) -> str:
    return f"artifacts/{app_id}/bot_state"

def get_global_user_profiles_path(app_id: str) -> str:
    return f"artifacts/{app_id}/global_user_profiles"
    
def get_user_details_path(app_id: str, user_id: str) -> str:
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
    if "clear" in weather_main:
        return "☀️"
    elif "clouds" in weather_main:
        return "☁️"
    elif "rain" in weather_main or "drizzle" in weather_main:
        return "🌧️"
    elif "thunderstorm" in weather_main:
        return "⛈️"
    elif "snow" in weather_main:
        return "❄️"
    elif "mist" in weather_main or "fog" in weather_main or "haze" in weather_main:
        return "🌫️"
    else:
        return "🌎"
