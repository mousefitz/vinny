import logging
import io
import base64
import json
import os
from datetime import datetime
from typing import Coroutine
import fal_client
import asyncio
import aiohttp
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.genai import types

# --- GLOBAL LOCK ---
# This ensures only one image generation happens at a time across the whole bot
IMAGE_LOCK = asyncio.Lock()

# --- IMAGEN MODEL NAME CONSTANT ---
model_name = "fal-ai/flux-2/flash"

# --- PRICING CONSTANTS ---
IMAGEN_FAST_PRICE = 0.02
IMAGEN_STD_PRICE = 0.04
IMAGEN_ULTRA_PRICE = 0.06

# GEMINI 3.0 FLASH PREVIEW PRICING
GEMINI_INPUT_PRICE = 0.50       # $0.50 per 1M tokens
GEMINI_OUTPUT_PRICE = 3.00      # $3.00 per 1M tokens
GOOGLE_SEARCH_PRICE = 0.014     # $14.00 per 1,000 queries for Grounding

FAL_LLM_PRICE = 0.001           # $0.001 per request
FLUX_PRICE = 0.005              # $0.005 per Megapixel
SERPER_SEARCH_PRICE = 0.001     # $1.00 per 1,000 searches

def calculate_cost(model_name, usage_type="image", count=1, input_tokens=0, output_tokens=0):
    """Calculates the estimated cost based on usage."""
    total_cost = 0.0
    model_lower = model_name.lower()
    
    if usage_type == "image":
        if "flux" in model_lower:
            unit_cost = FLUX_PRICE 
        else:
            unit_cost = IMAGEN_STD_PRICE
            if "fast" in model_lower: unit_cost = IMAGEN_FAST_PRICE
            elif "ultra" in model_lower: unit_cost = IMAGEN_ULTRA_PRICE
        total_cost = unit_cost * count

    elif usage_type == "text":
        if "fal-ai" in model_lower or "enterprise" in model_lower:
            total_cost = FAL_LLM_PRICE * count
        else:
            cost_in = (input_tokens / 1_000_000) * GEMINI_INPUT_PRICE
            cost_out = (output_tokens / 1_000_000) * GEMINI_OUTPUT_PRICE
            total_cost = cost_in + cost_out
            
    # FIXED: Split Google Search (Grounding) and Serper (Images) so they track the right prices
    elif usage_type == "google_search":
        total_cost = GOOGLE_SEARCH_PRICE * count
    elif usage_type == "search":
        total_cost = SERPER_SEARCH_PRICE * count
        
    return round(total_cost, 6)

# --- Flux Image Generation ---

async def generate_image_with_genai(client, prompt, model=model_name):
    """
    Generates an image using Fal.ai (Flux) while maintaining the original 
    function signature. Includes a global lock and relaxed safety settings.
    """
    fal_key = os.getenv("FAL_KEY")
    if not fal_key:
        logging.error("FAL_KEY not found in environment variables.")
        return None, 0

    os.environ["FAL_KEY"] = fal_key

    # The Lock ensures Vinny only paints one masterpiece at a time
    async with IMAGE_LOCK:
        try:
            logging.info(f"Requesting Flux image for prompt: {prompt[:50]}...")

            # Using Fal.ai with the relaxed safety toggle
            handler = await fal_client.submit_async(
                model,
                arguments={
                    "prompt": prompt,
                    "image_size": "square_hd",
                    "enable_safety_checker": False # THE RELAXED TOGGLE
                }
            )
            
            result = await handler.get()
            image_url = result['images'][0]['url']
            
            # Download to BytesIO to remain compatible with your Discord upload logic
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status == 200:
                        image_data = await resp.read()
                        return io.BytesIO(image_data), 1
                    else:
                        logging.error(f"Failed to download image from Fal.ai: {resp.status}")
                        
        except Exception as e:
            logging.error(f"Flux Generation failed: {e}")
            
    return None, 0
  
# --- Google GenAI Text Generation ---

async def generate_text_with_genai(client, prompt, model="gemini-3.0-flash-preview"):
    """
    Generates text using the tracked wrapper so costs hit the ledger.
    """
    try:
        from google.genai import types # Ensure types is imported
        response = await client.make_tracked_api_call(
            model=model,
            contents=[prompt],
            config=types.GenerateContentConfig() # Default config
        )
        if response and response.text:
            return response.text
    except Exception as e:
        logging.error(f"GenAI Text Generation failed: {e}")
    return None

# --- OpenWeatherMap API ---

async def geocode_location(http_session: aiohttp.ClientSession, api_key: str, location: str):
    if not api_key: return None
    params = {"limit": 1, "appid": api_key}
    is_zip = location.isdigit() and len(location) == 5
    
    if is_zip:
        base_url = "https://api.openweathermap.org/geo/1.0/zip"
        params["zip"] = f"{location},US"
    else:
        base_url = "https://api.openweathermap.org/geo/1.0/direct"
        params["q"] = location

    try:
        async with http_session.get(base_url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                res = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
                if res and "lat" in res and "lon" in res and "name" in res:
                    return res
    except Exception:
        logging.error("Geocoding API call failed.", exc_info=True)
    return None

async def get_weather_data(http_session: aiohttp.ClientSession, api_key: str, lat: float, lon: float):
    if not api_key: return None
    params = {"lat": lat, "lon": lon, "appid": api_key, "units": "imperial"}
    try:
        async with http_session.get("https://api.openweathermap.org/data/2.5/weather", params=params) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        logging.error("Weather data API call failed.", exc_info=True)
    return None

async def get_5_day_forecast(http_session: aiohttp.ClientSession, api_key: str, lat: float, lon: float):
    if not api_key: return None
    params = {"lat": lat, "lon": lon, "appid": api_key, "units": "imperial"}
    try:
        url = "https://api.openweathermap.org/data/2.5/forecast"
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        logging.error("5-Day Forecast API call failed.", exc_info=True)
    return None

# --- Google Custom Search API for Image Search ---

async def search_google_images(http_session, api_key, query):
    """Queries Serper.dev for images and returns a list of URLs."""
    url = "https://google.serper.dev/images"
    headers = {
        'X-API-KEY': api_key,
        'Content-Type': 'application/json'
    }
    
    payload = json.dumps({
    "q": query,
    "safe": "off" 
})

    try:
        async with http_session.post(url, headers=headers, data=payload) as response:
            if response.status == 200:
                data = await response.json()
                
                return [img["imageUrl"] for img in data.get("images", [])[:10]]
            else:
                error_body = await response.text()
                logging.error(f"Serper API Error: {response.status} - {error_body}")
    except Exception as e:
        logging.error(f"Exception during Serper image search: {e}")
    return []