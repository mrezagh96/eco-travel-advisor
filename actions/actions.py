"""
actions.py — Eco-Travel Advisor
=====================================================================
The Rasa-facing layer: everything here is an Action or a
FormValidationAction. All the actual API work lives in api_clients.py,
the ranking maths lives in scoring.py, and the fallback/handover system
lives in fallback.py — this file just wires user intent to those
building blocks and to the dispatcher.
"""

from __future__ import annotations

import random
import string
from typing import Any, Dict, List, Optional, Text

from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import EventType, SlotSet
from rasa_sdk.executor import CollectingDispatcher

from . import api_clients, scoring
from .fallback import reset_fallback_events

NIGHTS_PER_STAY = 3  # coursework simplification — see README "Known limitations"


# =========================================================================
# Form: trip_planning_form
# =========================================================================

class ValidateTripPlanningForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_trip_planning_form"

    async def required_slots(
        self,
        domain_slots: List[Text],
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Text]:
        # required_slots() pattern straight out of "Making a Bot Behave" §2.3,
        # applied to a real branch: travellers who said sustainability is
        # their TOP priority get one extra adaptive question that decides how
        # hard the scoring function (scoring.py) leans on carbon vs. cost.
        slots = list(domain_slots)  # copy — never mutate what we were given
        if tracker.get_slot("sustainability_level") == "high" and "priority_focus" not in slots:
            slots.append("priority_focus")
        return slots

    def validate_origin_city(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        try:
            found = api_clients.geocode(str(slot_value))
        except api_clients.ApiError:
            found = None
        if not found:
            dispatcher.utter_message(
                text=f"I couldn't find a place called '{slot_value}'. Could you try a nearby larger city or check the spelling?"
            )
            return {"origin_city": None}
        return {"origin_city": str(slot_value).strip().title()}

    def validate_destination_city(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        try:
            found = api_clients.geocode(str(slot_value))
        except api_clients.ApiError:
            found = None
        if not found:
            dispatcher.utter_message(
                text=f"I couldn't find a place called '{slot_value}'. Could you try a different spelling?"
            )
            return {"destination_city": None}

        origin = tracker.get_slot("origin_city")
        if origin and str(slot_value).strip().lower() == str(origin).strip().lower():
            dispatcher.utter_message(
                text="Your destination can't be the same as where you're leaving from — where would you actually like to go?"
            )
            return {"destination_city": None}
        return {"destination_city": str(slot_value).strip().title()}

    def validate_travel_date_from(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        text = str(slot_value).strip()
        if not text or len(text) > 100:
            dispatcher.utter_message(text="Sorry, could you give me a rough departure date, in a few words?")
            return {"travel_date_from": None}
        return {"travel_date_from": text}

    def validate_travel_date_to(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        text = str(slot_value).strip()
        if not text or len(text) > 100:
            dispatcher.utter_message(text="Sorry, could you give me a rough return date, in a few words?")
            return {"travel_date_to": None}
        return {"travel_date_to": text}

    def validate_num_travelers(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        try:
            n = int(float(slot_value))
            if not (1 <= n <= 20):
                raise ValueError
        except (TypeError, ValueError):
            dispatcher.utter_message(text="Please give me a number of travellers between 1 and 20.")
            return {"num_travelers": None}
        return {"num_travelers": n}

    def validate_budget_amount(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # Same "extract digits, reject if there are none" pattern taught for
        # validate_budget() in "Making a Bot Behave" §2.4.
        digits = "".join(ch for ch in str(slot_value) if ch.isdigit() or ch == ".")
        try:
            amount = float(digits)
            if amount <= 0:
                raise ValueError
        except ValueError:
            dispatcher.utter_message(text='I need a positive number for your budget — e.g. "800" or "800 EUR".')
            return {"budget_amount": None}
        return {"budget_amount": amount}

    def validate_sustainability_level(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        value = str(slot_value).strip().lower()
        if value not in ("low", "medium", "high"):
            dispatcher.utter_message(text="Please pick one of the options below.")
            return {"sustainability_level": None}
        return {"sustainability_level": value}

    def validate_priority_focus(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        value = str(slot_value).strip().lower()
        if value not in ("carbon", "cost", "balanced"):
            dispatcher.utter_message(text="Please pick one of the options below.")
            return {"priority_focus": None}
        return {"priority_focus": value}


# =========================================================================
# Form submission: build the full recommendation
# =========================================================================

class ActionGenerateTripRecommendations(Action):
    def name(self) -> Text:
        return "action_generate_trip_recommendations"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        origin_name = tracker.get_slot("origin_city")
        destination_name = tracker.get_slot("destination_city")
        sustainability_level = tracker.get_slot("sustainability_level") or "medium"
        priority_focus = tracker.get_slot("priority_focus") or ""
        budget_currency = tracker.get_slot("budget_currency") or "EUR"

        try:
            origin_geo = api_clients.geocode(origin_name)
            dest_geo = api_clients.geocode(destination_name)
        except api_clients.ApiError:
            origin_geo = dest_geo = None

        if not origin_geo or not dest_geo:
            dispatcher.utter_message(
                text="Sorry, I lost track of one of those two places — could we start the trip planning again?"
            )
            return reset_fallback_events()

        results: Dict[str, Any] = {"origin": origin_name, "destination": destination_name}

        # ---- flights (see api_clients.get_flight_estimate) -----------------
        try:
            flight = api_clients.get_flight_estimate(
                origin_name, origin_geo["lat"], origin_geo["lon"],
                destination_name, dest_geo["lat"], dest_geo["lon"],
            )
        except api_clients.ApiError:
            flight = None

        distance_km = (flight or {}).get("distance_km") or api_clients.haversine_km(
            origin_geo["lat"], origin_geo["lon"], dest_geo["lat"], dest_geo["lon"]
        )

        if flight and budget_currency != flight.get("currency", "EUR"):
            try:
                converted, _rate, _date = api_clients.convert(
                    flight["price"], flight.get("currency", "EUR"), budget_currency
                )
                flight["price_converted"] = round(converted, 2)
                flight["converted_currency"] = budget_currency
            except api_clients.ApiError:
                pass
        results["flight"] = flight

        # ---- hotels near the destination (Overpass) -------------------------
        try:
            hotels_raw = api_clients.find_hotels(dest_geo["lat"], dest_geo["lon"])
        except api_clients.ApiError:
            hotels_raw = []

        hotel_candidates = []
        for h in hotels_raw[:8]:
            stars = h.get("stars")
            try:
                stars_n = float(stars) if stars else 3.0
            except ValueError:
                stars_n = 3.0
            # OSM almost never carries real price data (documented in
            # 02_find_hotels.py) — nightly price is an indicative estimate
            # from star rating, always labelled as such, never presented as
            # a real quote.
            nightly_price = 35 + stars_n * 25
            hotel_candidates.append(
                {
                    "name": h["name"],
                    "eco_certified": bool(h.get("eco_tag")),
                    "stars": stars,
                    "carbon_kg": 15.0,  # flat indicative per-stay footprint, see README
                    "price": round(nightly_price * NIGHTS_PER_STAY, 2),
                    "price_is_estimated": True,
                }
            )
        ranked_hotels = scoring.rank_options(hotel_candidates, sustainability_level, priority_focus)
        results["hotels"] = ranked_hotels[:5]

        # ---- public transport availability at the destination ---------------
        try:
            transport_stops = api_clients.find_transport(dest_geo["lat"], dest_geo["lon"])
            transport_summary = api_clients.summarise_transport(transport_stops)
        except api_clients.ApiError:
            transport_stops, transport_summary = [], "unavailable right now"
        results["transport_summary"] = transport_summary
        results["transport_stop_count"] = len(transport_stops)

        # ---- attractions + a short Wikipedia blurb for the top few -----------
        try:
            attractions = api_clients.find_attractions(dest_geo["lat"], dest_geo["lon"])
        except api_clients.ApiError:
            attractions = []
        enriched_attractions = []
        for a in attractions[:3]:
            info = None
            try:
                info = api_clients.describe(a.get("wikipedia_tag") or a["name"])
            except api_clients.ApiError:
                info = None
            enriched_attractions.append(
                {
                    "name": a["name"],
                    "kind": a.get("kind"),
                    "summary": info["summary"] if info else None,
                    "source_url": info["url"] if info else None,
                }
            )
        results["attractions"] = enriched_attractions

        # ---- weather at the destination ---------------------------------------
        try:
            weather_raw = api_clients.get_weather(dest_geo["lat"], dest_geo["lon"])
            current = weather_raw["current"]
            advice = api_clients.travel_advice(current["temperature_2m"], current["precipitation"])
            results["weather"] = {
                "temperature_c": current["temperature_2m"],
                "precipitation_mm": current["precipitation"],
                "wind_kmh": current["wind_speed_10m"],
                "advice": advice,
            }
        except (api_clients.ApiError, KeyError):
            results["weather"] = None

        # ---- carbon comparison across modes for this distance ------------------
        try:
            mode_comparison = api_clients.compare_modes(distance_km)
        except api_clients.ApiError:
            mode_comparison = []
        results["carbon_by_mode"] = [{"mode": m, "carbon_kg": round(kg, 1)} for m, kg in mode_comparison]
        results["distance_km"] = round(distance_km, 0)

        # ---- headline "how to get there" ranking: flight vs train vs coach -----
        travel_options = []
        if flight:
            flight_mode = "flight_short" if distance_km < 1500 else "flight_long"
            travel_options.append(
                {
                    "mode": "flight",
                    "price": flight.get("price_converted", flight.get("price")),
                    "carbon_kg": round(api_clients.estimate_carbon_kg(flight_mode, distance_km), 1),
                    "duration_hours": flight.get("duration_hours"),
                }
            )
        for mode in ("train", "coach"):
            travel_options.append(
                {
                    "mode": mode,
                    # No free, no-key fare API exists for rail/coach across
                    # Europe (documented gap — see README "Data sources").
                    # Price is left as None rather than guessed, and the UI
                    # discloses that rather than silently mixing a real and
                    # an invented number.
                    "price": None,
                    "carbon_kg": round(api_clients.estimate_carbon_kg(mode, distance_km), 1),
                    "duration_hours": None,
                }
            )

        priceable = [o for o in travel_options if o["price"] is not None]
        if travel_options and len(priceable) == len(travel_options):
            ranked_travel = scoring.rank_options(travel_options, sustainability_level, priority_focus)
        else:
            ranked_travel = sorted(travel_options, key=lambda o: o["carbon_kg"])
            for idx, option in enumerate(ranked_travel):
                option["tier"] = "green" if idx == 0 else ("amber" if idx == 1 else "red")
        results["travel_options"] = ranked_travel

        events: List[EventType] = [SlotSet("trip_results", results)] + reset_fallback_events()

        dispatcher.utter_message(
            text=self._summary_text(results, budget_currency),
            buttons=[
                {"title": "✅ Book this trip", "payload": "/confirm_booking"},
                {"title": "🔎 See more options", "payload": "/see_more_options"},
                {"title": "🧑‍💼 Talk to a human", "payload": "/request_human_handover"},
            ],
            json_message={"custom": {"card_type": "trip_summary", "data": results}},
        )
        return events

    @staticmethod
    def _summary_text(results: Dict[str, Any], currency: str) -> str:
        lines = [f"Here's what I found for **{results['origin']} → {results['destination']}**:"]

        if results.get("travel_options"):
            best = results["travel_options"][0]
            emoji = scoring.tier_emoji(best.get("tier", ""))
            price_txt = (
                f", ~{best['price']:.0f} {currency}"
                if best.get("price") is not None
                else " (no fare data available for this mode — see report)"
            )
            lines.append(
                f"{emoji} Best way there: **{best['mode']}** — ~{best['carbon_kg']:.0f} kg CO2e{price_txt}"
            )

        if results.get("hotels"):
            top_hotel = results["hotels"][0]
            emoji = scoring.tier_emoji(top_hotel.get("tier", ""))
            eco_note = (
                " (OSM eco-certification tag present)"
                if top_hotel.get("eco_certified")
                else " (no certification data available for this hotel)"
            )
            lines.append(
                f"{emoji} Top hotel pick: **{top_hotel['name']}** — ~{top_hotel['price']:.0f} {currency} "
                f"for {NIGHTS_PER_STAY} nights{eco_note}"
            )

        lines.append(f"🚉 Public transport near {results['destination']}: {results['transport_summary']}")

        if results.get("weather"):
            w = results["weather"]
            lines.append(f"🌤️ Weather there right now: {w['temperature_c']}°C — {w['advice']}")

        if results.get("attractions"):
            names = ", ".join(a["name"] for a in results["attractions"])
            lines.append(f"🏛️ Worth visiting: {names}")

        return "\n".join(lines)


# =========================================================================
# Post-recommendation actions
# =========================================================================

class ActionConfirmBooking(Action):
    def name(self) -> Text:
        return "action_confirm_booking"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        results = tracker.get_slot("trip_results")
        if not results:
            dispatcher.utter_message(
                text="I don't have an active trip to book yet — tell me where you'd like to go first."
            )
            return reset_fallback_events()

        reference = "ECO-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        dispatcher.utter_message(
            text=(
                f"🎫 Booking confirmed — reference **{reference}** for "
                f"{results['origin']} → {results['destination']}.\n\n"
                "This is a demo confirmation only: no real payment or reservation has "
                "been made. This project deliberately doesn't integrate a live payments/"
                "booking API — see README \"Known limitations\" for why."
            ),
            json_message={
                "custom": {"card_type": "booking_confirmation", "reference": reference, "data": results}
            },
        )
        return [SlotSet("booking_reference", reference)] + reset_fallback_events()


class ActionSeeMoreOptions(Action):
    def name(self) -> Text:
        return "action_see_more_options"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        results = tracker.get_slot("trip_results")
        if not results or not results.get("hotels"):
            dispatcher.utter_message(text="I don't have more options stored right now — let's plan a trip first.")
            return reset_fallback_events()

        currency = tracker.get_slot("budget_currency") or "EUR"
        lines = ["Here are a few more hotel options:"]
        for h in results["hotels"]:
            emoji = scoring.tier_emoji(h.get("tier", ""))
            lines.append(f"{emoji} {h['name']} — ~{h['price']:.0f} {currency} for {NIGHTS_PER_STAY} nights")

        dispatcher.utter_message(
            text="\n".join(lines),
            json_message={"custom": {"card_type": "more_hotels", "data": results["hotels"]}},
        )
        return reset_fallback_events()


# =========================================================================
# Standalone data lookups (work with or without an active trip)
# =========================================================================

class ActionGetWeather(Action):
    def name(self) -> Text:
        return "action_get_weather"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        city = tracker.get_slot("city_name") or tracker.get_slot("destination_city")
        if not city:
            dispatcher.utter_message(text="Which city would you like the weather for?")
            return []
        try:
            geo = api_clients.geocode(city)
            if not geo:
                dispatcher.utter_message(text=f"Sorry, I couldn't find a place called {city}.")
                return reset_fallback_events()
            data = api_clients.get_weather(geo["lat"], geo["lon"])
            current = data["current"]
            advice = api_clients.travel_advice(current["temperature_2m"], current["precipitation"])
            dispatcher.utter_message(
                text=(
                    f"Weather in {city}: {current['temperature_2m']}°C, "
                    f"wind {current['wind_speed_10m']} km/h, {current['precipitation']} mm rain. "
                    f"{advice.capitalize()}."
                )
            )
        except api_clients.ApiError:
            dispatcher.utter_message(text="Sorry, the weather service isn't responding right now — please try again shortly.")
        return reset_fallback_events()


class ActionGetCurrencyExchange(Action):
    def name(self) -> Text:
        return "action_get_currency_exchange"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        from_c = tracker.get_slot("from_currency")
        to_c = tracker.get_slot("to_currency")
        amount = tracker.get_slot("exchange_amount") or 1
        if not from_c or not to_c:
            dispatcher.utter_message(text="Which two currencies would you like to convert between?")
            return []
        try:
            converted, rate, date = api_clients.convert(amount, from_c, to_c)
            dispatcher.utter_message(
                text=f"{amount:.0f} {from_c.upper()} = {converted:.2f} {to_c.upper()} (ECB reference rate, {date})."
            )
        except api_clients.ApiError:
            dispatcher.utter_message(text="Sorry, the currency service isn't responding right now.")
        return reset_fallback_events()


class ActionGetCarbonFootprint(Action):
    def name(self) -> Text:
        return "action_get_carbon_footprint"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        results = tracker.get_slot("trip_results")
        if not results or not results.get("carbon_by_mode"):
            dispatcher.utter_message(
                text=(
                    "I don't have a trip to compare yet — tell me where you're travelling "
                    "from and to, and I'll break the carbon footprint down by transport mode."
                )
            )
            return reset_fallback_events()

        lines = [
            f"Estimated CO2e for {results['origin']} → {results['destination']} "
            f"(~{results['distance_km']:.0f} km) by mode:"
        ]
        for m in results["carbon_by_mode"]:
            lines.append(f"  • {m['mode']}: ~{m['carbon_kg']:.0f} kg CO2e")
        dispatcher.utter_message(text="\n".join(lines))
        return reset_fallback_events()
