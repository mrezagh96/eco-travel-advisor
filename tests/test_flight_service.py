"""
tests/test_flight_service.py — smoke tests for flight_data_service/main.py
Run with:  pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from flight_data_service.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_curated_route_returns_table_values():
    response = client.get(
        "/flights/estimate",
        params={
            "origin_name": "Berlin", "origin_lat": 52.52, "origin_lon": 13.405,
            "destination_name": "Lisbon", "destination_lat": 38.7223, "destination_lon": -9.1393,
        },
    )
    data = response.json()
    assert response.status_code == 200
    assert data["source"] == "curated_table"
    assert data["price"] == 120


def test_curated_route_is_direction_independent():
    forward = client.get(
        "/flights/estimate",
        params={
            "origin_name": "Berlin", "origin_lat": 52.52, "origin_lon": 13.405,
            "destination_name": "Lisbon", "destination_lat": 38.7223, "destination_lon": -9.1393,
        },
    ).json()
    backward = client.get(
        "/flights/estimate",
        params={
            "origin_name": "Lisbon", "origin_lat": 38.7223, "origin_lon": -9.1393,
            "destination_name": "Berlin", "destination_lat": 52.52, "destination_lon": 13.405,
        },
    ).json()
    assert forward["price"] == backward["price"]
    assert forward["duration_hours"] == backward["duration_hours"]


def test_unlisted_route_falls_back_to_distance_estimate():
    response = client.get(
        "/flights/estimate",
        params={
            "origin_name": "Hannover", "origin_lat": 52.3759, "origin_lon": 9.7320,
            "destination_name": "Tallinn", "destination_lat": 59.4370, "destination_lon": 24.7536,
        },
    )
    data = response.json()
    assert data["source"] == "distance_estimate"
    assert data["price"] > 0
    assert data["duration_hours"] > 0
