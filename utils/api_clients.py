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

# --- VINNY IMAGE & TEXT USAGE TRACKER ---
def track_daily_usage(model_name, usage_type="image", tokens=0):
    """
    Logs costs for Imagen 4 Fast & Gemini 2.5 Flash.
    """
    file_path = "vinny_usage_stats.json"
    today = datetime.now().strftime("%Y-%m-%d")
    
    # --- PRICING LOGIC (Jan 2026) ---
    cost = 0.0
    
    if usage_type == "image":
        if "fast" in model_name: cost = 0.01 
        elif "imagen-4" in model_name: cost = 0.04
        else: cost = 0.02
    elif usage_type == "text":
        # Gemini 2.5 Flash (~$0.10 per 1M tokens)
        cost = (tokens / 1_000_000) * 0.10

    # --- SAVE TO FILE ---
    data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f: data = json.load(f)
        except: data = {}

    if today not in data:
        data[today] = {"images": 0, "text_requests": 0, "tokens": 0, "estimated_cost": 0.0}
    
    if usage_type == "image": data[today]["images"] += 1
    elif usage_type == "text":
        data[today]["text_requests"] += 1
        data[today]["tokens"] += tokens
        
    data[today]["estimated_cost"] = round(data[today]["estimated_cost"] + cost, 5)
    
    with open(file_path, "w") as f: json.dump(data, f, indent=4)

# --- Google Cloud Imagen API ---

async def generate_image_with_imagen(
    http_session: aiohttp.ClientSession,
    loop: Coroutine,
    prompt: str,
    gcp_project_id: str,
    firebase_b64_creds: str
) -> io.BytesIO | None:
    if not gcp_project_id or not firebase_b64_creds:
        logging.warning("GCP Project ID or Firebase creds not set. Imagen disabled.")
        return None
    
    token = None
    try:
        service_account_info = json.loads(base64.b64decode(firebase_b64_creds).decode('utf-8'))
        creds = service_account.Credentials.from_service_account_info(service_account_info)
        scoped_creds = creds.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])
        await loop.run_in_executor(None, lambda: scoped_creds.refresh(Request()))
        token = scoped_creds.token
    except Exception:
        logging.error("Failed to refresh Google auth token for Imagen.", exc_info=True)
        return None

    gcp_region = "us-central1"
    
    # 1. UPDATED: Model ID changed to imagen-4.0-generate-001
    api_url = f"https://{gcp_region}-aiplatform.googleapis.com/v1/projects/{gcp_project_id}/locations/{gcp_region}/publishers/google/models/imagen-4.0-fast-generate-001:predict"
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    
    # 2. UPDATED: Parameters updated for the newer API (number_of_images, aspect_ratio)
    data = {
        "instances": [
            {
                "prompt": prompt
            }
        ],
        "parameters": {
            "sampleCount": 1,        
            "aspectRatio": "1:1",        
            "safetySetting": "block_only_high", 
            "personGeneration": "allow_adult"   
        }
    }

    try:
        async with http_session.post(api_url, headers=headers, json=data) as response:
            if response.status == 200:
                
                track_daily_usage("imagen-4.0-fast", usage_type="image")

                result = await response.json()
                
                if result.get("predictions") and "bytesBase64Encoded" in result["predictions"][0]:
                    return io.BytesIO(base64.b64decode(result["predictions"][0]["bytesBase64Encoded"]))
                else:
                    logging.error(f"Imagen API returned 200 OK but the response body was unexpected: {result}")
            else:
                logging.error(f"Imagen API returned non-200 status: {response.status} | Body: {await response.text()}")
    except Exception:
        logging.error("An exception occurred during the Imagen API call.", exc_info=True)
    return None

# --- OpenWeatherMap API ---

async def geocode_location(http_session: aiohttp.ClientSession, api_key: str, location: str):
    if not api_key: return None
    params = {"limit": 1, "appid": api_key}
    is_zip = location.isdigit() and len(location) == 5
    
    if is_zip:
        base_url = "http://api.openweathermap.org/geo/1.0/zip"
        params["zip"] = f"{location},US"
    else:
        base_url = "http://api.openweathermap.org/geo/1.0/direct"
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
        async with http_session.get("http://api.openweathermap.org/data/2.5/weather", params=params) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        logging.error("Weather data API call failed.", exc_info=True)
    return None

async def get_5_day_forecast(http_session: aiohttp.ClientSession, api_key: str, lat: float, lon: float):
    """Gets a 5-day forecast (in 3-hour intervals) from the standard free endpoint."""
    if not api_key: return None
    
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "imperial",
    }
    
    try:
        url = "https://api.openweathermap.org/data/2.5/forecast"
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
            else:
                error_text = await response.text()
                logging.error(f"OpenWeatherMap 5-Day Forecast API returned non-200 status: {response.status} | Body: {error_text}")
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
