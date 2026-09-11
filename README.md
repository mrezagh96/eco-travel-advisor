---
title: Eco Travel Advisor
emoji: 🧭
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# 🧭 Eco-Travel Advisor

A Rasa-based conversational agent that plans lower-carbon trips: tell it
where you're going, and it geocodes both cities, estimates flights, finds
hotels and public transport near your destination, checks the weather,
converts currencies, and ranks everything with a weighted carbon/price
scoring function tuned to how much sustainability matters to you.

Built for *Advanced Conversational UI Design & Chatbot Development*
(MSc Artificial Intelligence, BSBI Berlin / UCA).

## Contents

- [Architecture](#architecture)
- [Quick start — local development](#quick-start--local-development)
- [Quick start — Docker Compose](#quick-start--docker-compose)
- [Deploying to HuggingFace Spaces](#deploying-to-huggingface-spaces)
- [Data sources](#data-sources)
- [The fallback & human-handover system](#the-fallback--human-handover-system)
- [Frontend design decisions](#frontend-design-decisions)
- [Known limitations](#known-limitations)
- [Project structure](#project-structure)
- [Environment variables](#environment-variables)

## Architecture

```
                     ┌─────────────────────┐
   user ──────────── │  Streamlit dashboard │  (only public port on HF Spaces)
                     └──────────┬───────────┘
                                │ REST (/webhooks/rest/webhook)
                     ┌──────────▼───────────┐
                     │      Rasa server      │  NLU (DIET) + Core (rules/forms)
                     └──────────┬───────────┘
                                │ action_endpoint
                     ┌──────────▼───────────┐
                     │  Rasa action server   │  actions/*.py
                     └──────────┬───────────┘
                                │ REST
              ┌─────────────────┼─────────────────────────────┐
              │                 │                              │
     ┌────────▼───────┐ ┌───────▼────────┐   Nominatim · Overpass ·
     │ flight_data_    │ │ (everything     │   Open-Meteo · Frankfurter ·
     │ service (ours)  │ │  else — see     │   Wikipedia REST (all free,
     │ FastAPI, :8000  │ │  api_clients.py)│   no key — see Data sources)
     └─────────────────┘ └─────────────────┘
```

All four processes run inside **one container** in the HuggingFace Spaces
deployment (`Dockerfile` + `start.sh`), since Spaces only routes external
traffic to a single port. `docker-compose.yml` runs the same four pieces as
properly separated containers for local development.

## Quick start — local development

Same two-terminal pattern as Worksheet 1/2, plus a third terminal for the
flight data service:

```bash
python3.10 -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -r requirements.txt

rasa data validate                 # catches domain/data mistakes in seconds
rasa train

# terminal 1
uvicorn flight_data_service.main:app --port 8000

# terminal 2
rasa run actions --port 5055

# terminal 3
rasa run --enable-api --cors "*" --port 5005

# terminal 4
streamlit run streamlit_app.py
```

Python **3.10** specifically — `rasa==3.6.21` requires `>=3.8,<3.11` (see
`requirements.txt` for the full reasoning behind every pinned version).

## Quick start — Docker Compose

```bash
docker compose up --build
```

Then open `http://localhost:8501`. Rasa's REST API is on `:5005`, the
action server on `:5055`, the flight data service on `:8000`.

## Deploying to HuggingFace Spaces

1. Create a new Space → **Docker** SDK.
2. Push this whole repository to the Space (the YAML block at the top of
   this file is the Space's required metadata — `sdk: docker` and
   `app_port: 7860` are what make it "just work" with no extra
   configuration in the Spaces UI).
3. Wait for the build. **The first build takes several minutes** — it
   installs Rasa's full dependency stack and then runs `rasa train`
   *during the image build* (see `Dockerfile`) so the container itself
   starts in seconds on every subsequent boot/wake. This is normal, not
   a hang.
4. If the build fails at the `rasa data validate` step, that means a
   genuine domain/data inconsistency slipped in — the log will say
   exactly which intent/action/slot is the problem.

No secrets are required for the default setup. `CLIMATIQ_API_KEY` is
optional (see Environment variables) — without it, carbon figures come
entirely from the local emission-factor table, which is a fully
documented, citable fallback, not a degraded mode.

## Data sources

| Data | Source | Key needed | Notes |
|---|---|---|---|
| Geocoding | Nominatim (OpenStreetMap) | No | Cached; max 1 req/s per usage policy |
| Hotels | Overpass (OpenStreetMap) | No | Amadeus replacement — see `actions/api_clients.py`. No reliable eco-certification data exists in OSM; the UI always discloses when a hotel has no certification tag rather than implying one |
| Public transport | Overpass (OpenStreetMap) | No | Station/stop *locations* only, not live schedules |
| Attractions | Overpass + Wikipedia REST | No | CC BY-SA content — always linked back to the source article |
| Weather | Open-Meteo | No | Current conditions + 3-day outlook |
| Currency | Frankfurter (ECB reference rates) | No | Once-daily reference rate, not a live trading rate |
| Carbon (transport modes) | Local factor table, cited from UK DESNZ/DEFRA & EEA conversion factors | No | Optional Climatiq API for real-time figures if `CLIMATIQ_API_KEY` is set |
| Flights | **Self-hosted** `flight_data_service/` | No | See below |

**Why flights are self-hosted:** the brief names the Amadeus for
Developers sandbox API. Amadeus's self-service platform was decommissioned
on 17 July 2026 — the portal and existing keys stopped working. As of this
project's build date, no free, no-signup, no-payment-details API returns
live flight prices; the closest legitimate alternatives
(Travelpayouts/Aviasales Data API, Kiwi Tequila) require partner approval
that can't be guaranteed before a coursework deadline, and a cluster of
newer "AI-agent flight booking" tools ask for a real payment card via
Stripe, which is inappropriate for a student project. `flight_data_service/`
is a small FastAPI app we control instead: a curated table of ~50 common
routes with indicative price/duration, and a documented haversine-distance
fallback formula (constants declared and justified in
`flight_data_service/main.py`) for any route not in the table. This keeps
the *architecture* identical to what the brief asks a custom action to do
— call a REST API — without depending on a third party that can vanish.

## The fallback & human-handover system

Custom-built rather than Rasa's default two-stage fallback (which only has
two stages) to match this project's spec exactly — see
`actions/fallback.py`:

1. **1st & 2nd** time the bot doesn't understand, or the user asks
   something off-topic → a nudge back to the bot's actual job, with
   quick-reply buttons generated in Python (not hardcoded in the UI).
2. **3rd** consecutive time in the same detour → a graceful decline
   ("I don't have information about that"), then the streak resets so a
   *later, separate* detour starts counting from zero again.
3. That decline is capped at **5 uses across the whole conversation**
   (`fallback_total_giveups`). On the 6th time the bot would have to
   decline again, it escalates to `action_handover_to_human` instead.

Human handover packages the full conversation (collected slots, last
20 turns of transcript, last intent/confidence) and — since this project
has no real ticketing system to send it to — logs it to
`handover_queue.jsonl` and prints it to the action server's console. That
substitution is documented here and in the code on purpose: describing an
integration you didn't build is fine for a coursework report; presenting a
console log as a finished integration would not be.

## Frontend design decisions

Palette and type were chosen to avoid the generic "AI dashboard" defaults
(warm cream + terracotta; SaaS card grids with identical shadows) in favour
of something grounded in the subject matter — a physical travel document:

| Token | Value | Role |
|---|---|---|
| Background | `#1B2B22` | deep pine charcoal |
| Surface | `#24352A` | cards / panels |
| Text | `#EDEBE2` | warm paper |
| Accent | `#C9A24B` | compass gold |
| Tier green/amber/red | `#5FA777` / `#D9A441` / `#C6553D` | the one required colour-coding signal |

Headings use **Fraunces** (a characterful serif, evoking old travel
posters); body/UI text uses **Inter** for density and legibility in chat.
Result cards are styled as **boarding passes / luggage tags** — an
asymmetric card with a dashed perforation and a rotated stub label —
rather than identical rounded SaaS cards, because the content genuinely is
a travel document. The tier badge is the *one* bold, saturated element;
everything else stays quiet charcoal-on-paper so the colour-coding the
brief asks for actually stands out instead of competing with decoration.

## Known limitations

Documented here so they can be cited honestly in the report rather than
discovered by a marker:

- **Hotel nightly price is estimated from star rating**, not a real quote
  (OSM carries almost no price data — see `02_find_hotels.py`'s own
  note). Always labelled `price_is_estimated: true` in the data sent to
  the frontend.
- **Stay length is fixed at 3 nights** (`NIGHTS_PER_STAY` in
  `actions/actions.py`) rather than computed from the two dates the user
  gives, to keep the scope of this iteration manageable.
- **No free fare API exists for rail/coach** across Europe, so those
  travel options carry a carbon figure but no price — the UI discloses
  this rather than inventing a number.
- **Booking is a mock confirmation only.** No payment or real reservation
  API is integrated — this is disclosed to the user in the confirmation
  message itself, not just in this file.
- **Tracker store is the default in-memory store** (see `endpoints.yml`).
  Fine for a demo; a persistent store (SQL/Redis) would be the production
  upgrade path.
- **City-name entity recognition** relies on DIET generalising from a
  training set of major cities — an unusual or very small town's name may
  not be extracted correctly. `validate_origin_city` /
  `validate_destination_city` catch this by geocoding whatever *was*
  extracted and asking again if Nominatim can't find it, but if DIET
  extracts nothing at all, the form will just re-ask.

## Project structure

```
eco-travel-advisor/
├── config.yml                  # NLU pipeline + dialogue policies
├── domain.yml                  # intents, entities, slots, responses, forms
├── endpoints.yml
├── data/
│   ├── nlu.yml
│   ├── rules.yml
│   └── stories.yml
├── actions/
│   ├── actions.py              # form validation + trip recommendation orchestrator
│   ├── api_clients.py          # every external API call, ported from 01–08 sample scripts
│   ├── scoring.py               # weighted carbon/price ranking function
│   └── fallback.py              # smart fallback (3-nudge/5-giveup) + human handover
├── flight_data_service/
│   ├── main.py                  # self-hosted flight data REST API
│   └── flight_routes.json       # curated route table
├── streamlit_app.py             # dashboard frontend
├── Dockerfile                   # single-container build (HuggingFace Spaces)
├── docker-compose.yml           # four-container local dev topology
├── start.sh                     # boots all four processes in the combined container
└── requirements.txt
```

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `RASA_URL` | `http://localhost:5005` | `streamlit_app.py` |
| `ACTIONS_HOST` | `localhost` | `endpoints.yml` |
| `FLIGHT_SERVICE_URL` | `http://localhost:8000` | `actions/api_clients.py` |
| `CLIMATIQ_API_KEY` | *(unset)* | `actions/api_clients.py` — optional, falls back to the local carbon table |
| `HANDOVER_LOG_PATH` | `handover_queue.jsonl` | `actions/fallback.py` |
| `PORT` | `7860` | `start.sh` — the single port HuggingFace Spaces routes to |
