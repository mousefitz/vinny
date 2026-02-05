import logging
import io
import base64
import json
import os
from datetime import datetime
from typing import Coroutine

import aiohttp
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.genai import types

# --- IMAGEN MODEL NAME CONSTANT ---
model_name = "imagen-4.0-fast-generate-001:predict"

# --- PRICING CONSTANTS ---
IMAGEN_FAST_PRICE = 0.02     # Imagen 4 Fast
IMAGEN_STD_PRICE = 0.04      # Standard
IMAGEN_ULTRA_PRICE = 0.06    # Ultra
GEMINI_INPUT_PRICE = 0.30    # $0.30 per 1M Input Tokens
GEMINI_OUTPUT_PRICE = 2.50   # $2.50 per 1M Output Tokens

# --- COST CALCULATOR (Pure Math) ---

def calculate_cost(model_name, usage_type="image", count=1, input_tokens=0, output_tokens=0):
    """Calculates the estimated cost based on usage."""
    total_cost = 0.0
    
    if usage_type == "image":
        unit_cost = IMAGEN_STD_PRICE
        if "fast" in model_name: unit_cost = IMAGEN_FAST_PRICE
        elif "ultra" in model_name: unit_cost = IMAGEN_ULTRA_PRICE
        elif "imagen-4" in model_name: unit_cost = IMAGEN_STD_PRICE
        total_cost = unit_cost * count

    elif usage_type == "text":
        cost_in = (input_tokens / 1_000_000) * GEMINI_INPUT_PRICE
        cost_out = (output_tokens / 1_000_000) * GEMINI_OUTPUT_PRICE
        total_cost = cost_in + cost_out
        
    return round(total_cost, 6)

# --- Google Cloud Imagen API ---

# --- Google Cloud Imagen API ---

async def generate_image_with_genai(client, prompt, model="imagen-4.0-fast-generate-001"):
    """
    Generates an image using the google-genai SDK (API Key).
    Returns: (image_bytes_io, count)
    """
    try:
        # Call the API
        response = await client.aio.models.generate_images(
            model=model,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="1:1",
                person_generation="allow_adult"
            )
        )
        
        # Check for results
        if response.generated_images:
            image_bytes = response.generated_images[0].image.image_bytes
            return io.BytesIO(image_bytes), 1
        else:
            # --- FIX: Log the reason why no images were returned ---
            logging.warning(f"GenAI returned 0 images. Full Response: {response}")
            
    except Exception as e:
        logging.error(f"GenAI Image Generation failed: {e}")
        
    return None, 0

# --- OpenWeatherMap API ---

async def geocode_location(http_session: aiohttp.ClientSession, api_key: str, location: str):
    if not api_key: return None
    params = {"limit": 1, "appid": api_key}
    is_zip = location.isdigit() and len(location) == 5
    
    if is_zip:
        # UPDATED: Use HTTPS
        base_url = "https://api.openweathermap.org/geo/1.0/zip"
        params["zip"] = f"{location},US"
    else:
        # UPDATED: Use HTTPS
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
        # UPDATED: Use HTTPS
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
        # UPDATED: Use HTTPS
        url = "https://api.openweathermap.org/data/2.5/forecast"
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        logging.error("5-Day Forecast API call failed.", exc_info=True)
    return None

# --- Horoscope API ---

async def get_horoscope(http_session: aiohttp.ClientSession, sign: str):
    if not http_session: return None
    url = "https://horoscope-app-api.vercel.app/api/v1/get-horoscope/daily"
    params = {"sign": sign.lower(), "day": "today"}
    try:
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data and data.get("status") and "data" in data:
                    return data["data"]
    except Exception:
        logging.error("Horoscope API call failed.", exc_info=True)
    return None
