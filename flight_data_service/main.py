"""
flight_data_service/main.py — Eco-Travel Advisor's self-hosted flight data API
=====================================================================
Why this exists: the assignment brief names the Amadeus for Developers
sandbox API for flight/hotel data. Amadeus's self-service platform was
decommissioned on 17 July 2026 (see actions/api_clients.py's module
docstring for the full reasoning). As of this project's build date, no
free, no-signup, no-payment-details API returns live flight prices —
the closest legitimate alternatives (Travelpayouts/Aviasales Data API,
Kiwi Tequila) require partner approval that can't be guaranteed before
a coursework deadline, and a cluster of newer "AI-agent flight booking"
tools ask you to attach a real payment card via Stripe, which is not
appropriate for a student project.

So: this IS a REST API, exactly the architecture the brief asks a
custom action to call — it's simply one we host ourselves rather than
a third party that can vanish or gate access behind a payment method.
It answers from two sources, in order:

  1. A curated table (flight_routes.json) of ~50 common European (plus
     a couple of long-haul) routes with indicative duration/price
     figures. These are NOT live prices scraped from anywhere — they
     are rounded, typical averages for the route based on general,
     publicly known fare and schedule patterns, included to make the
     demo look realistic. Cite this honestly in the report as curated
     indicative data, never as real-time pricing.

  2. For any origin/destination pair NOT in the table (which will be
     most of them, since a user can type ANY two cities in the world),
     a documented distance-based estimate: great-circle (haversine)
     distance, an assumed average block speed, a fixed turnaround
     overhead, and a simple base-fee-plus-per-km fare model. Every
     constant is declared and justified below, exactly like
     EMISSION_FACTORS in 08_carbon_estimate.py, so it can be cited and
     defended in the report the same way.

Run standalone:  uvicorn flight_data_service.main:app --port 8000
Docs once running: http://localhost:8000/docs
"""

from __future__ import annotations

import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import List

from fastapi import FastAPI, Query
from pydantic import BaseModel

app = FastAPI(
    title="Eco-Travel Advisor — Flight Data Service",
    description="Self-hosted replacement for the decommissioned Amadeus sandbox API.",
    version="1.0.0",
)

ROUTES_PATH = Path(__file__).parent / "flight_routes.json"

# ---- distance-based fallback model, used for any route not in the table ----
# All constants below are DELIBERATE, documented estimates, not measured —
# discuss/cite them in the report the same way as the carbon factor table.
AVERAGE_BLOCK_SPEED_KMH = 700.0    # blended cruise+climb+descent speed for a
                                    # typical short/medium-haul narrow-body jet
TURNAROUND_OVERHEAD_HOURS = 1.0    # boarding, taxi, take-off/landing on
                                    # average — excludes check-in/security
SHORT_HAUL_BASE_FEE_EUR = 40.0     # airport charges + minimum fare component
SHORT_HAUL_RATE_PER_KM = 0.10      # EUR/km, typical short/medium-haul
                                    # low-cost-carrier economy yield
LONG_HAUL_THRESHOLD_KM = 3500.0
LONG_HAUL_BASE_FEE_EUR = 150.0
LONG_HAUL_RATE_PER_KM = 0.06       # long-haul economy is cheaper per km


def _load_routes() -> List[dict]:
    with open(ROUTES_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


_ROUTES = _load_routes()
_ROUTES_INDEX = {}
for _r in _ROUTES:
    _ROUTES_INDEX[(_r["origin"].strip().lower(), _r["destination"].strip().lower())] = _r
    _ROUTES_INDEX[(_r["destination"].strip().lower(), _r["origin"].strip().lower())] = _r


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * r * asin(sqrt(a))


class FlightEstimate(BaseModel):
    origin: str
    destination: str
    distance_km: float
    duration_hours: float
    price: float
    currency: str = "EUR"
    source: str  # "curated_table" | "distance_estimate"


@app.get("/health")
def health():
    return {"status": "ok", "routes_loaded": len(_ROUTES)}


@app.get("/flights/estimate", response_model=FlightEstimate)
def estimate_flight(
    origin_name: str = Query(...),
    origin_lat: float = Query(...),
    origin_lon: float = Query(...),
    destination_name: str = Query(...),
    destination_lat: float = Query(...),
    destination_lon: float = Query(...),
):
    key = (origin_name.strip().lower(), destination_name.strip().lower())
    curated = _ROUTES_INDEX.get(key)
    distance_km = _haversine_km(origin_lat, origin_lon, destination_lat, destination_lon)

    if curated:
        return FlightEstimate(
            origin=origin_name,
            destination=destination_name,
            distance_km=round(distance_km, 1),
            duration_hours=curated["duration_hours"],
            price=curated["price_eur"],
            currency="EUR",
            source="curated_table",
        )

    if distance_km > LONG_HAUL_THRESHOLD_KM:
        base_fee, rate = LONG_HAUL_BASE_FEE_EUR, LONG_HAUL_RATE_PER_KM
    else:
        base_fee, rate = SHORT_HAUL_BASE_FEE_EUR, SHORT_HAUL_RATE_PER_KM

    duration_hours = TURNAROUND_OVERHEAD_HOURS + distance_km / AVERAGE_BLOCK_SPEED_KMH
    price = base_fee + rate * distance_km

    return FlightEstimate(
        origin=origin_name,
        destination=destination_name,
        distance_km=round(distance_km, 1),
        duration_hours=round(duration_hours, 2),
        price=round(price / 5) * 5,  # round to nearest 5 — reads like a real fare
        currency="EUR",
        source="distance_estimate",
    )
