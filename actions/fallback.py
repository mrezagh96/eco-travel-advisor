"""
fallback.py — Eco-Travel Advisor
=====================================================================
Custom fallback + human-handover behaviour, replacing Rasa's built-in
action_two_stage_fallback (which only has two stages). Project spec:

  * 1st and 2nd time the bot doesn't understand / the user goes off
    topic -> nudge them back with relevant quick-reply buttons.
  * 3rd consecutive time in the SAME detour -> give up gracefully
    ("I don't have information about that"), then reset the streak so
    a fresh detour later starts counting from zero again.
  * The give-up message is capped at 5 uses across the WHOLE
    conversation. On the 6th time the bot would have to give up, it
    escalates to a human advisor instead of declining again.

Two counters, both on slots so they survive across turns:
  fallback_streak         — consecutive off-topic/low-confidence turns
                             in the CURRENT detour (resets on give-up,
                             and whenever another action in actions.py
                             calls reset_fallback_events()).
  fallback_total_giveups  — how many times we've reached "give up" in
                             THIS conversation (never reset).
"""

from __future__ import annotations

import json
import os
import random
import string
from datetime import datetime, timezone
from typing import Any, Dict, List

from rasa_sdk import Action, Tracker
from rasa_sdk.events import EventType, FollowupAction, SlotSet
from rasa_sdk.executor import CollectingDispatcher

GIVEUP_LIMIT_PER_CONVERSATION = 5

# Maps a handful of in-domain intents to a human-readable label, so the
# stage-1 "did you mean...?" buttons never show a raw intent name like
# `ask_trip_planning` — per Rasa's own advice on overriding the default
# two-stage fallback action (Worksheet "Making a Bot Behave" §4.4).
INTENT_LABELS: Dict[str, str] = {
    "ask_trip_planning": "Planning a trip",
    "ask_weather": "Checking the weather",
    "ask_currency_exchange": "A currency conversion",
    "ask_carbon_footprint": "Your trip's carbon footprint",
    "ask_carbon_offset_info": "Carbon offsetting",
    "confirm_booking": "Booking one of your options",
    "see_more_options": "Seeing more options",
    "request_human_handover": "Talking to a human advisor",
}


def _topic_buttons() -> List[Dict[str, str]]:
    """The bot's own menu of what it can actually help with. Built in
    Python and sent via dispatcher.utter_message(buttons=...) rather than
    a static domain.yml response — the brief specifically asks for buttons
    "generated dynamically from custom action responses"."""
    return [
        {"title": "🧳 Plan a trip", "payload": "/ask_trip_planning"},
        {"title": "☁️ Check the weather", "payload": "/ask_weather"},
        {"title": "💱 Currency exchange", "payload": "/ask_currency_exchange"},
        {"title": "🧑‍💼 Talk to a human", "payload": "/request_human_handover"},
    ]


def _guessed_intent_buttons(tracker: Tracker) -> List[Dict[str, str]]:
    """Reads DIET's intent_ranking for the message that triggered the
    fallback and turns the top matches into "did you mean...?" buttons."""
    ranking = tracker.latest_message.get("intent_ranking", []) or []
    candidates = [r for r in ranking if r.get("name") in INTENT_LABELS][:2]
    return [
        {"title": INTENT_LABELS[r["name"]], "payload": f"/{r['name']}"}
        for r in candidates
    ]


def reset_fallback_events() -> List[EventType]:
    """Any action that successfully handles a genuine, in-scope request
    should append `+ reset_fallback_events()` to its returned events —
    that is how the streak counter learns the user is back on topic."""
    return [SlotSet("fallback_streak", 0)]


class ActionSmartFallback(Action):
    def name(self) -> str:
        return "action_smart_fallback"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[str, Any]):
        streak = int(tracker.get_slot("fallback_streak") or 0) + 1
        total_giveups = int(tracker.get_slot("fallback_total_giveups") or 0)

        if streak == 1:
            buttons = _guessed_intent_buttons(tracker) + _topic_buttons()
            dispatcher.utter_message(
                text=(
                    "Sorry, I didn't quite catch that. I'm your eco-travel "
                    "planning assistant, so I'm best with trip planning, "
                    "weather and currency questions — did you mean one of these?"
                ),
                buttons=buttons,
            )
            return [SlotSet("fallback_streak", streak)]

        if streak == 2:
            dispatcher.utter_message(
                text=(
                    "I'm still not following, sorry. Let's get back on track — "
                    "pick one below, or just tell me where you'd like to travel:"
                ),
                buttons=_topic_buttons(),
            )
            return [SlotSet("fallback_streak", streak)]

        # streak >= 3: give up gracefully on THIS particular detour, then
        # reset the streak so a later, separate detour starts counting
        # from zero — only the whole-conversation `total_giveups` persists.
        total_giveups += 1
        events: List[EventType] = [
            SlotSet("fallback_streak", 0),
            SlotSet("fallback_total_giveups", total_giveups),
        ]

        if total_giveups > GIVEUP_LIMIT_PER_CONVERSATION:
            dispatcher.utter_message(
                text=(
                    "We've hit this a few times now, so rather than keep "
                    "guessing, let me bring in a human advisor instead."
                )
            )
            events.append(FollowupAction("action_handover_to_human"))
            return events

        dispatcher.utter_message(
            text=(
                "I'm sorry — I don't have information to help with that, it's "
                "outside what I'm built for. Whenever you're ready, let's get "
                "back to planning your trip:"
            ),
            buttons=_topic_buttons(),
        )
        return events


# -------------------------------------------------------------------------
# Human handover — full-context packaging
# -------------------------------------------------------------------------

HANDOVER_LOG_PATH = os.environ.get("HANDOVER_LOG_PATH", "handover_queue.jsonl")

_HANDOVER_SLOT_KEYS = [
    "origin_city", "destination_city", "travel_date_from", "travel_date_to",
    "num_travelers", "budget_amount", "budget_currency", "sustainability_level",
    "priority_focus",
]


def _build_transcript(tracker: Tracker, max_turns: int = 20) -> List[str]:
    transcript = []
    for event in tracker.events:
        if event.get("event") == "user" and event.get("text"):
            transcript.append(f"USER: {event['text']}")
        elif event.get("event") == "bot" and event.get("text"):
            transcript.append(f"BOT : {event['text']}")
    return transcript[-max_turns:]


def _collected_slots(tracker: Tracker) -> Dict[str, Any]:
    return {k: tracker.get_slot(k) for k in _HANDOVER_SLOT_KEYS if tracker.get_slot(k) is not None}


def send_to_human_queue(package: Dict[str, Any]) -> None:
    """Coursework-honest stand-in for a real integration. A production
    system would POST this package to a Slack webhook or a ticketing
    system (Zendesk, Freshdesk, ...). That infrastructure doesn't exist
    for a demo, so it is persisted to a local append-only log and printed
    to the action server's console instead. Being explicit about that
    substitution in the report is legitimate; presenting the console log
    as a finished integration would not be — see "Making a Bot Behave" §5.3.
    """
    print("\n===== HUMAN HANDOVER =====")
    print(json.dumps(package, indent=2, ensure_ascii=False))
    print("===========================\n")
    try:
        with open(HANDOVER_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(package, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a logging failure must never take down the handover itself


class ActionHandoverToHuman(Action):
    def name(self) -> str:
        return "action_handover_to_human"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[str, Any]):
        last_intent = tracker.latest_message.get("intent", {}).get("name")
        last_confidence = tracker.latest_message.get("intent", {}).get("confidence")
        transcript = _build_transcript(tracker)
        reference = "HUM-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

        package = {
            "handover_reference": reference,
            "conversation_id": tracker.sender_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "collected_slots": _collected_slots(tracker),
            "trip_results_available": tracker.get_slot("trip_results") is not None,
            "last_intent": last_intent,
            "last_confidence": last_confidence,
            "turn_count": len(transcript),
            "transcript": transcript,
        }
        send_to_human_queue(package)

        dispatcher.utter_message(response="utter_handover_notice")
        dispatcher.utter_message(
            text=(
                f"Your reference for this handover is **{reference}** — a human "
                "advisor can pull up everything we've discussed using it."
            ),
            json_message={"custom": {"card_type": "handover", "reference": reference}},
        )
        return [SlotSet("handover_active", True)]
