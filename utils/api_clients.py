import logging
import io
import base64
import json
from typing import Coroutine

import aiohttp
from google.oauth2 import service_account
from google.auth.transport.requests import Request

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
    api_url = f"https://{gcp_region}-aiplatform.googleapis.com/v1/projects/{gcp_project_id}/locations/{gcp_region}/publishers/google/models/imagegeneration@006:predict"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    data = {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}}

    try:
        async with http_session.post(api_url, headers=headers, json=data) as response:
            if response.status == 200:
                result = await response.json()
                if result.get("predictions") and "bytesBase64Encoded" in result["predictions"][0]:
                    return io.BytesIO(base64.b64decode(result["predictions"][0]["bytesBase64Encoded"]))
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

async def get_full_weather_forecast(http_session: aiohttp.ClientSession, api_key: str, lat: float, lon: float):
    """Gets a full forecast using the One Call API."""
    if not api_key: return None
    
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "imperial",
        "exclude": "minutely,hourly"
    }
    
    # We will use the 2.5 endpoint as it's the most common for free keys
    url = "https://api.openweathermap.org/data/2.5/onecall"
    
    try:
        async with http_session.get(url, params=params) as response:
            # --- NEW DEBUGGING LOGIC ---
            if response.status == 200:
                return await response.json()
            else:
                # Log the exact error from the API if the request fails
                error_text = await response.text()
                logging.error(f"OpenWeatherMap API returned non-200 status: {response.status} | Body: {error_text}")
                return None # Ensure we return None on failure
    except Exception:
        logging.error("One Call weather API call failed.", exc_info=True)
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