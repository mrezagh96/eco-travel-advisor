"""
api_clients.py — Eco-Travel Advisor
=====================================================================
Thin, well-behaved wrappers around every external (or self-hosted) API
the bot uses. This module is deliberately framework-agnostic: nothing
in here imports Rasa. actions.py calls these functions and turns their
results (or their ApiError exceptions) into dispatcher messages.

Every function here is a direct descendant of one of the course's
sample scripts (01_geocode_place.py .. 08_carbon_estimate.py) — same
endpoints, same honesty about each API's limitations — just:
  * wrapped in a shared retry/timeout/caching policy (the reliability
    layer 02_find_hotels.py explicitly said was missing — this is our
    own 09_reliability_and_caching.py), and
  * raising a single ApiError instead of letting requests exceptions
    or empty results propagate raw into a custom action.

Flight search is the one exception: Amadeus (the API the assignment
brief names) was decommissioned on 17 July 2026, and no free, no-key,
no-payment-details API returns live flight prices as of this project's
build date. Rather than depend on Amadeus, Booking.com-affiliate style
partner programmes (which need account approval we can't guarantee
before the deadline), or the sketchy "AI-agent flight booking" tools
that ask for real payment cards, `get_flight_estimate()` calls a REST
API *we host ourselves* (flight_data_service/) that mirrors the exact
architecture the Rasa docs recommend for custom actions calling an
external API — it is just an API we control instead of a third party.
It is seeded with a curated table of real-world indicative fares/
durations for common European routes and falls back to a documented,
cited distance-based estimate for any route not in the table (see
flight_data_service/main.py for the methodology and citations).
"""

from __future__ import annotations

import functools
import time
import urllib.parse
from math import asin, cos, radians, sin, sqrt
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

# -----------------------------------------------------------------------
# Shared configuration
# -----------------------------------------------------------------------

HEADERS = {
    # Real contact details belong here before any production / public use —
    # Nominatim and Overpass's usage policies require an honest User-Agent.
    "User-Agent": "BSBI-EcoTravelAdvisor/1.0 (MSc coursework; contact via GitHub repo)"
}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"

import os

FLIGHT_SERVICE_URL = os.environ.get("FLIGHT_SERVICE_URL", "http://localhost:8000")
CLIMATIQ_API_KEY = os.environ.get("CLIMATIQ_API_KEY", "")  # optional, see estimate_carbon()


class ApiError(Exception):
    """Raised whenever a data source could not answer — always carries a
    short, user-safe explanation actions.py can hand straight to the
    dispatcher instead of leaking a stack trace into the conversation."""


# -----------------------------------------------------------------------
# Reliability layer: retry with backoff + a tiny in-memory TTL cache
# -----------------------------------------------------------------------
# Nominatim/Overpass explicitly warn that 504s and timeouts are common on
# their shared free infrastructure (see 02_find_hotels.py, 03_find_transport.py).
# Geocoding results never change for a given place name, so caching them is
# both a courtesy to the free service (Nominatim's usage policy asks for
# this) and a latency win for the bot's < 3s response target.

_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 60 * 60 * 6  # 6 hours — plenty for a single demo session


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


def with_retry(max_attempts: int = 3, backoff_seconds: float = 1.5):
    """Retries a flaky network call with exponential backoff. Only retries
    on network-level failures / timeouts / 5xx — a 404 or a genuinely empty
    result is NOT a reason to retry, it's an answer."""

    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                    last_exc = exc
                except requests.exceptions.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status and status < 500:
                        raise  # client error - retrying won't help
                    last_exc = exc
                if attempt < max_attempts:
                    time.sleep(backoff_seconds * attempt)
            raise ApiError(f"{func.__name__} failed after {max_attempts} attempts: {last_exc}")

        return wrapper

    return decorator


# -----------------------------------------------------------------------
# 1. Geocoding — Nominatim (see 01_geocode_place.py)
# -----------------------------------------------------------------------

@with_retry()
def geocode(place: str) -> Optional[Dict[str, Any]]:
    """Turn a place name into {lat, lon, display_name}. None if not found."""
    cache_key = f"geocode:{place.strip().lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    response = requests.get(
        NOMINATIM_URL,
        params={"q": place, "format": "json", "limit": 1},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    results = response.json()

    if not results:
        _cache_set(cache_key, None)
        return None

    best = results[0]
    result = {
        "lat": float(best["lat"]),
        "lon": float(best["lon"]),
        "display_name": best["display_name"],
    }
    _cache_set(cache_key, result)
    return result


# -----------------------------------------------------------------------
# 2. Hotels — Overpass, replacing the decommissioned Amadeus (02_find_hotels.py)
# -----------------------------------------------------------------------

@with_retry()
def find_hotels(lat: float, lon: float, radius_m: int = 3000, limit: int = 15) -> List[Dict[str, Any]]:
    """Hotels near a point. Honest about eco-certification: OpenStreetMap
    almost never carries that data (see module docstring in the original
    script) — the `eco` field is usually None, and callers must not present
    an uncertified hotel as eco-certified."""
    query = f"""
    [out:json][timeout:45];
    (
      nwr["tourism"="hotel"](around:{radius_m},{lat},{lon});
    );
    out center {limit};
    """
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=60)
    response.raise_for_status()

    hotels = []
    for element in response.json().get("elements", []):
        tags = element.get("tags", {})
        if not tags.get("name"):
            continue
        lat_val = element.get("lat") or element.get("center", {}).get("lat")
        lon_val = element.get("lon") or element.get("center", {}).get("lon")
        hotels.append(
            {
                "name": tags["name"],
                "stars": tags.get("stars"),
                "eco_tag": tags.get("green_key") or tags.get("ecolabel"),
                "lat": lat_val,
                "lon": lon_val,
            }
        )
    return hotels


# -----------------------------------------------------------------------
# 3. Transport — Overpass (03_find_transport.py)
# -----------------------------------------------------------------------

@with_retry()
def find_transport(lat: float, lon: float, radius_m: int = 1500) -> List[Dict[str, Any]]:
    query = f"""
    [out:json][timeout:45];
    (
      node["railway"="station"](around:{radius_m},{lat},{lon});
      node["railway"="subway_entrance"](around:{radius_m},{lat},{lon});
      node["railway"="tram_stop"](around:{radius_m},{lat},{lon});
    );
    out body 25;
    """
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=60)
    response.raise_for_status()

    stops = []
    for element in response.json().get("elements", []):
        tags = element.get("tags", {})
        stops.append(
            {
                "name": tags.get("name", "(unnamed)"),
                "type": tags.get("railway", "unknown"),
                "lat": element.get("lat"),
                "lon": element.get("lon"),
            }
        )
    return stops


def summarise_transport(stops: List[Dict[str, Any]]) -> str:
    """Turn a list of stops into one bot-friendly sentence — the same
    threshold logic taught in 03_find_transport.py."""
    if len(stops) >= 10:
        return "excellent — you likely won't need a car"
    if len(stops) >= 3:
        return "reasonable, but check routes for your specific trip"
    return "limited — factor extra transport emissions into your planning"


# -----------------------------------------------------------------------
# 4. Attractions — Overpass (04_find_attractions.py)
# -----------------------------------------------------------------------

VISITABLE_HISTORIC = "castle|monument|ruins|fort|archaeological_site|city_gate"


@with_retry()
def find_attractions(lat: float, lon: float, radius_m: int = 1500, limit: int = 8) -> List[Dict[str, Any]]:
    query = f"""
    [out:json][timeout:45];
    (
      nwr["tourism"="museum"](around:{radius_m},{lat},{lon});
      nwr["historic"~"^({VISITABLE_HISTORIC})$"](around:{radius_m},{lat},{lon});
    );
    out center {limit};
    """
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=60)
    response.raise_for_status()

    sites = []
    for element in response.json().get("elements", []):
        tags = element.get("tags", {})
        if not tags.get("name"):
            continue
        sites.append(
            {
                "name": tags["name"],
                "kind": tags.get("tourism") or tags.get("historic"),
                "wikipedia_tag": tags.get("wikipedia"),
            }
        )
    return sites


# -----------------------------------------------------------------------
# 5. Place descriptions — Wikipedia REST (05_place_description.py)
# -----------------------------------------------------------------------

@with_retry()
def describe(title: str, sentences: int = 2) -> Optional[Dict[str, Any]]:
    cache_key = f"wiki:{title.strip().lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    safe_title = urllib.parse.quote(title.replace(" ", "_"))
    response = requests.get(
        WIKIPEDIA_SUMMARY_URL.format(title=safe_title), headers=HEADERS, timeout=20
    )
    if response.status_code == 404:
        _cache_set(cache_key, None)
        return None
    response.raise_for_status()

    data = response.json()
    text = data.get("extract", "")
    parts = text.split(". ")
    short = ". ".join(parts[:sentences])
    if short and not short.endswith("."):
        short += "."

    result = {
        "title": data.get("title"),
        "description": data.get("description"),
        "summary": short,
        "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
    }
    _cache_set(cache_key, result)
    return result


# -----------------------------------------------------------------------
# 6. Weather — Open-Meteo (06_weather_forecast.py)
# -----------------------------------------------------------------------

@with_retry()
def get_weather(lat: float, lon: float) -> Dict[str, Any]:
    response = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,precipitation,wind_speed_10m",
            "daily": "temperature_2m_max,precipitation_sum",
            "forecast_days": 3,
            "timezone": "auto",
        },
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def travel_advice(temp_c: float, rain_mm: float) -> str:
    if rain_mm > 5:
        return "wet — plan indoor activities, and public transport over cycling"
    if temp_c < 5:
        return "cold — walking tours will be hard going"
    if temp_c > 30:
        return "hot — cycling and long walks are unwise in the middle of the day"
    return "good conditions for walking and cycling"


# -----------------------------------------------------------------------
# 7. Currency — Frankfurter / ECB reference rates (07_currency_exchange.py)
# -----------------------------------------------------------------------

@with_retry()
def get_rate(from_currency: str, to_currency: str) -> Tuple[float, str]:
    if from_currency.upper() == to_currency.upper():
        return 1.0, "n/a"
    response = requests.get(
        f"{FRANKFURTER_URL}/latest",
        params={"base": from_currency.upper(), "symbols": to_currency.upper()},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if to_currency.upper() not in data.get("rates", {}):
        raise ApiError(f"No rate available for {from_currency} -> {to_currency}")
    return data["rates"][to_currency.upper()], data["date"]


def convert(amount: float, from_currency: str, to_currency: str) -> Tuple[float, float, str]:
    rate, date = get_rate(from_currency, to_currency)
    return amount * rate, rate, date


# -----------------------------------------------------------------------
# 8. Carbon footprint — local factor table + optional Climatiq (08_carbon_estimate.py)
# -----------------------------------------------------------------------

# Indicative gCO2e per passenger-km. Rounded for teaching, cited sources:
# UK DESNZ/DEFRA GHG conversion factors and the EEA transport emissions
# dataset (see README.md "Data sources" for the full citation to use in
# the report's reference list).
EMISSION_FACTORS = {
    "walk": 0,
    "cycle": 0,
    "train": 35,
    "coach": 27,
    "electric_car": 50,
    "car_petrol": 170,
    "ferry": 115,
    "flight_short": 250,
    "flight_long": 150,
}


def estimate_carbon_kg(mode: str, distance_km: float) -> float:
    if mode not in EMISSION_FACTORS:
        raise ApiError(f"Unknown transport mode: {mode}")
    return EMISSION_FACTORS[mode] * distance_km / 1000.0


def compare_modes(distance_km: float, modes: Optional[List[str]] = None) -> List[Tuple[str, float]]:
    modes = modes or ["train", "coach", "car_petrol", "flight_short"]
    results = [(m, estimate_carbon_kg(m, distance_km)) for m in modes]
    return sorted(results, key=lambda pair: pair[1])


def estimate_carbon_climatiq(distance_km: float, activity_id: Optional[str] = None) -> Optional[Tuple[float, str]]:
    """Optional real-data path. Returns None (never raises) if no key is
    configured, so callers can always fall back to the local table without
    extra branching."""
    if not CLIMATIQ_API_KEY:
        return None

    activity_id = activity_id or (
        "passenger_vehicle-vehicle_type_car-fuel_source_na"
        "-engine_size_na-vehicle_age_na-vehicle_weight_na"
    )
    try:
        response = requests.post(
            "https://api.climatiq.io/data/v1/estimate",
            headers={**HEADERS, "Authorization": f"Bearer {CLIMATIQ_API_KEY}"},
            json={
                "emission_factor": {"activity_id": activity_id, "data_version": "^6"},
                "parameters": {"distance": distance_km, "distance_unit": "km"},
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data["co2e"], data["co2e_unit"]
    except requests.exceptions.RequestException:
        return None  # graceful — the local table is always the safety net


# -----------------------------------------------------------------------
# 9. Flights — our own REST service (see flight_data_service/)
# -----------------------------------------------------------------------

@with_retry(max_attempts=2, backoff_seconds=0.5)
def get_flight_estimate(
    origin_name: str, origin_lat: float, origin_lon: float,
    destination_name: str, destination_lat: float, destination_lon: float,
) -> Dict[str, Any]:
    """Calls flight_data_service — a small FastAPI app we host ourselves
    (see actions/api_clients.py module docstring for why). It returns an
    indicative price band, duration, and distance for the route, sourced
    either from its curated route table or a documented haversine-distance
    fallback formula. This keeps the *architecture* identical to what the
    brief asks for (a REST call from a custom action to an external
    service) without depending on Amadeus or a payment-gated third party.
    """
    response = requests.get(
        f"{FLIGHT_SERVICE_URL}/flights/estimate",
        params={
            "origin_name": origin_name,
            "origin_lat": origin_lat,
            "origin_lon": origin_lon,
            "destination_name": destination_name,
            "destination_lat": destination_lat,
            "destination_lon": destination_lon,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Used both here (as a pure-Python
    fallback if flight_data_service itself is unreachable) and inside
    flight_data_service/main.py for any route not in its curated table."""
    r = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * r * asin(sqrt(a))
