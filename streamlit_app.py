"""
streamlit_app.py — Eco-Travel Advisor dashboard
=====================================================================
Design direction ("boarding pass" theme — see accompanying design notes
in README.md "Frontend design decisions"):
  Background  #1B2B22  deep pine charcoal      Accent   #C9A24B  compass gold
  Surface     #24352A  card/panel background   Text     #EDEBE2  warm paper
  Tier green  #5FA777  Tier amber  #D9A441      Tier red #C6553D
  Display type: Fraunces (headings) · Body/UI type: Inter

Everything the bot sends as `buttons` is rendered as a real, clickable
button whose PAYLOAD (not its title) is sent back to Rasa — Rasa's REST
channel understands the `/intent_name{"entity":"value"}` syntax natively
and skips NLU entirely for it (see "Making a Bot Behave" §3.2-3.3).
Anything the bot sends as a `custom` JSON payload (trip summaries, hotel
lists, booking confirmations, handover notices) is rendered as a styled
HTML card instead of plain chat text — that's what makes the "colour-
coded results card" and "human handover indicator" required by the
brief actually look like a dashboard rather than a chat transcript.
"""

import os
import uuid

import requests
import streamlit as st

# =============================================================================
# Config
# =============================================================================

RASA_URL = os.environ.get("RASA_URL", "http://localhost:5005")
REST_WEBHOOK = f"{RASA_URL}/webhooks/rest/webhook"

TIER_COLORS = {"green": "#5FA777", "amber": "#D9A441", "red": "#C6553D"}
TIER_LABELS = {"green": "Low impact", "amber": "Moderate impact", "red": "High impact"}

st.set_page_config(
    page_title="Eco-Travel Advisor",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
# Styling
# =============================================================================

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=Inter:wght@400;500;600;700&display=swap');

:root {
    --bg: #1B2B22;
    --surface: #24352A;
    --surface-raised: #2C3F32;
    --text: #EDEBE2;
    --text-muted: #A9B7AC;
    --accent: #C9A24B;
    --tier-green: #5FA777;
    --tier-amber: #D9A441;
    --tier-red: #C6553D;
    --hairline: #3A4D3E;
}

html, body, [class*="stApp"] {
    background-color: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
}

h1, h2, h3, .eco-heading {
    font-family: 'Fraunces', serif;
    font-weight: 600;
    letter-spacing: -0.01em;
}

section[data-testid="stSidebar"] {
    background-color: #16221A;
    border-right: 1px solid var(--hairline);
}
section[data-testid="stSidebar"] * { color: var(--text) !important; }

/* chat bubbles */
[data-testid="stChatMessage"] {
    background-color: var(--surface);
    border: 1px solid var(--hairline);
    border-radius: 10px;
    padding: 0.25rem 0.5rem;
}

/* buttons: bot quick-replies AND streamlit's native buttons */
.stButton > button {
    background-color: var(--surface-raised);
    color: var(--text);
    border: 1px solid var(--accent);
    border-radius: 999px;
    padding: 0.4rem 1rem;
    font-family: 'Inter', sans-serif;
    font-weight: 500;
    transition: background-color 0.15s ease;
}
.stButton > button:hover {
    background-color: var(--accent);
    color: #1B2B22;
    border-color: var(--accent);
}

/* ---- boarding-pass style result card ------------------------------- */
.pass-card {
    background: var(--surface);
    border: 1px solid var(--hairline);
    border-radius: 6px;
    margin: 0.6rem 0;
    display: flex;
    overflow: hidden;
}
.pass-main { flex: 1; padding: 0.9rem 1.1rem; }
.pass-stub {
    width: 88px;
    flex-shrink: 0;
    border-left: 2px dashed var(--hairline);
    display: flex;
    align-items: center;
    justify-content: center;
    writing-mode: vertical-rl;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.72rem;
    font-weight: 700;
    color: #16221A;
}
.pass-route {
    font-family: 'Fraunces', serif;
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 0.15rem;
}
.pass-meta { color: var(--text-muted); font-size: 0.86rem; }
.pass-price { color: var(--accent); font-weight: 600; font-size: 0.95rem; margin-top: 0.3rem; }

.handover-banner {
    background: var(--tier-red);
    color: #1B2B22;
    font-weight: 600;
    padding: 0.5rem 0.9rem;
    border-radius: 6px;
    margin-bottom: 0.8rem;
}

.section-label {
    color: var(--text-muted);
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin: 0.9rem 0 0.3rem 0;
}
</style>
""",
    unsafe_allow_html=True,
)

# =============================================================================
# Session state
# =============================================================================

if "sender_id" not in st.session_state:
    st.session_state.sender_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []  # list of {role, text, buttons, custom}
if "handover_active" not in st.session_state:
    st.session_state.handover_active = False
if "pending_payload" not in st.session_state:
    st.session_state.pending_payload = None


def send_to_rasa(message: str):
    """Sends raw text OR a Rasa button payload ('/intent{"entity":"value"}')
    to the REST webhook, appends the exchange to history, and stores every
    bot reply's buttons/custom payload for rendering."""
    st.session_state.messages.append({"role": "user", "text": message, "buttons": None, "custom": None})
    try:
        response = requests.post(
            REST_WEBHOOK,
            json={"sender": st.session_state.sender_id, "message": message},
            timeout=15,
        )
        response.raise_for_status()
        bot_replies = response.json()
    except requests.exceptions.RequestException:
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": "⚠️ I couldn't reach the bot server. Is `rasa run --enable-api` running?",
                "buttons": None,
                "custom": None,
            }
        )
        return

    if not bot_replies:
        st.session_state.messages.append(
            {"role": "assistant", "text": "…", "buttons": None, "custom": None}
        )
        return

    for reply in bot_replies:
        custom = reply.get("custom")
        if custom and custom.get("card_type") == "handover":
            st.session_state.handover_active = True
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": reply.get("text"),
                "buttons": reply.get("buttons"),
                "custom": custom,
            }
        )


# =============================================================================
# Card renderers (for `custom` JSON payloads from actions.py)
# =============================================================================

def render_tier_badge(tier: str) -> str:
    color = TIER_COLORS.get(tier, "#888")
    label = TIER_LABELS.get(tier, "")
    return f'<span style="background:{color};color:#16221A;font-weight:700;font-size:0.72rem;padding:0.15rem 0.55rem;border-radius:999px;">{label}</span>'


def render_trip_summary(data: dict):
    st.markdown(f'<div class="pass-route">{data["origin"]} → {data["destination"]}</div>', unsafe_allow_html=True)

    travel_options = data.get("travel_options") or []
    if travel_options:
        st.markdown('<div class="section-label">Getting there</div>', unsafe_allow_html=True)
        for opt in travel_options:
            price_txt = f"~{opt['price']:.0f} EUR" if opt.get("price") is not None else "fare data not available"
            stub_bg = TIER_COLORS.get(opt.get("tier"), "#888")
            st.markdown(
                f"""<div class="pass-card">
                    <div class="pass-main">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <span style="font-weight:600;text-transform:capitalize;">{opt['mode']}</span>
                            {render_tier_badge(opt.get('tier',''))}
                        </div>
                        <div class="pass-meta">~{opt['carbon_kg']:.0f} kg CO2e</div>
                        <div class="pass-price">{price_txt}</div>
                    </div>
                    <div class="pass-stub" style="background:{stub_bg}22;">{opt['mode'][:3]}</div>
                </div>""",
                unsafe_allow_html=True,
            )

    hotels = data.get("hotels") or []
    if hotels:
        st.markdown('<div class="section-label">Where to stay</div>', unsafe_allow_html=True)
        for h in hotels[:3]:
            eco_txt = "OSM eco-tag ✓" if h.get("eco_certified") else "no certification data"
            stub_bg = TIER_COLORS.get(h.get("tier"), "#888")
            st.markdown(
                f"""<div class="pass-card">
                    <div class="pass-main">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <span style="font-weight:600;">{h['name']}</span>
                            {render_tier_badge(h.get('tier',''))}
                        </div>
                        <div class="pass-meta">{eco_txt}</div>
                        <div class="pass-price">~{h['price']:.0f} EUR for the stay</div>
                    </div>
                    <div class="pass-stub" style="background:{stub_bg}22;">HTL</div>
                </div>""",
                unsafe_allow_html=True,
            )

    if data.get("weather"):
        w = data["weather"]
        st.markdown('<div class="section-label">Weather</div>', unsafe_allow_html=True)
        st.markdown(f"🌤️ {w['temperature_c']}°C — {w['advice']}")

    if data.get("attractions"):
        st.markdown('<div class="section-label">Worth visiting</div>', unsafe_allow_html=True)
        for a in data["attractions"]:
            summary = f" — {a['summary']}" if a.get("summary") else ""
            st.markdown(f"🏛️ **{a['name']}**{summary}")

    st.markdown(f"🚉 Public transport: {data.get('transport_summary', 'n/a')}")


def render_booking_confirmation(payload: dict):
    ref = payload.get("reference", "")
    data = payload.get("data", {})
    st.markdown(
        f"""<div class="pass-card">
            <div class="pass-main">
                <div class="pass-route">🎫 {data.get('origin','')} → {data.get('destination','')}</div>
                <div class="pass-meta">Demo booking reference</div>
                <div class="pass-price" style="font-size:1.2rem;">{ref}</div>
            </div>
            <div class="pass-stub" style="background:#C9A24B22;">OK</div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_more_hotels(hotels: list):
    for h in hotels:
        stub_bg = TIER_COLORS.get(h.get("tier"), "#888")
        st.markdown(
            f"""<div class="pass-card">
                <div class="pass-main">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <span style="font-weight:600;">{h['name']}</span>
                        {render_tier_badge(h.get('tier',''))}
                    </div>
                    <div class="pass-price">~{h['price']:.0f} EUR for the stay</div>
                </div>
                <div class="pass-stub" style="background:{stub_bg}22;">HTL</div>
            </div>""",
            unsafe_allow_html=True,
        )


def render_custom_card(custom: dict):
    card_type = custom.get("card_type")
    if card_type == "trip_summary":
        render_trip_summary(custom.get("data", {}))
    elif card_type == "booking_confirmation":
        render_booking_confirmation(custom)
    elif card_type == "more_hotels":
        render_more_hotels(custom.get("data", []))
    # "handover" card_type has no extra visual beyond the banner + text reply


# =============================================================================
# Sidebar
# =============================================================================

with st.sidebar:
    st.markdown("## 🧭 Eco-Travel Advisor")
    st.caption("Advanced Conversational UI Design & Chatbot Development — BSBI")
    page = st.radio("Navigate", ["Chat", "About"], label_visibility="collapsed")

    if st.session_state.handover_active:
        st.markdown('<div class="handover-banner">🧑‍💼 Connected to a human advisor</div>', unsafe_allow_html=True)

    st.divider()
    st.markdown("**Quick start**")
    for label, city in [("🇵🇹 Berlin → Lisbon", "Lisbon"), ("🇮🇹 Berlin → Rome", "Rome"), ("🇪🇸 Berlin → Barcelona", "Barcelona")]:
        if st.button(label, key=f"quickstart-{city}", use_container_width=True):
            st.session_state.pending_payload = f"I want to go from Berlin to {city}"

    st.divider()
    if st.button("🔄 Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.sender_id = str(uuid.uuid4())
        st.session_state.handover_active = False
        st.rerun()

# =============================================================================
# Main area
# =============================================================================

if page == "About":
    st.markdown("# About this project")
    st.write(
        "Eco-Travel Advisor is a Rasa-based conversational agent that plans "
        "lower-carbon trips: it geocodes your origin and destination, estimates "
        "flights, finds hotels and public transport near your destination, checks "
        "the weather, converts currencies, and ranks everything with a weighted "
        "carbon/price scoring function tuned to how much sustainability matters "
        "to you."
    )
    st.markdown("**Built with:** Rasa Open Source · a self-hosted FastAPI flight-data service · "
                 "Nominatim · Overpass (OpenStreetMap) · Open-Meteo · Frankfurter · Wikipedia REST API")
else:
    st.markdown("# Plan your next trip")
    st.caption("Tell me where you're going, or use the quick-start buttons in the sidebar.")

    if not st.session_state.messages:
        st.session_state.pending_payload = st.session_state.pending_payload or None

    # ---- render history ----
    last_assistant_idx = max(
        (i for i, m in enumerate(st.session_state.messages) if m["role"] == "assistant"),
        default=-1,
    )
    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message("user" if msg["role"] == "user" else "assistant", avatar="🧑" if msg["role"] == "user" else "🧭"):
            if msg.get("text"):
                st.markdown(msg["text"])
            if msg.get("custom"):
                render_custom_card(msg["custom"])
            # only the LATEST bot message's buttons stay clickable
            if msg.get("buttons") and i == last_assistant_idx:
                cols = st.columns(len(msg["buttons"]))
                for col, btn in zip(cols, msg["buttons"]):
                    with col:
                        if st.button(btn["title"], key=f"btn-{i}-{btn['payload']}", use_container_width=True):
                            st.session_state.pending_payload = btn["payload"]

    # ---- input ----
    user_text = st.chat_input("Type your message here…")
    if user_text:
        st.session_state.pending_payload = user_text

    if st.session_state.pending_payload:
        payload = st.session_state.pending_payload
        st.session_state.pending_payload = None
        send_to_rasa(payload)
        st.rerun()
