"""
Constants for the availability bot.

States control the conversation lifecycle.
Intents classify inbound client messages.
"""

# --- Conversation states ---
STATE_AWAITING_REPLY = "AWAITING_REPLY"
STATE_COLLECTING_AVAILABILITY = "COLLECTING_AVAILABILITY"
STATE_CONFIRMING_AVAILABILITY = "CONFIRMING_AVAILABILITY"
STATE_CLOSED = "CLOSED"

ALL_STATES = {
    STATE_AWAITING_REPLY,
    STATE_COLLECTING_AVAILABILITY,
    STATE_CONFIRMING_AVAILABILITY,
    STATE_CLOSED,
}

# --- Intents (returned by the LLM) ---
INTENT_AVAILABILITY = "availability"
INTENT_CONFIRMATION = "confirmation"
INTENT_RESCHEDULE = "reschedule"
INTENT_OTHER = "other"

ALL_INTENTS = {
    INTENT_AVAILABILITY,
    INTENT_CONFIRMATION,
    INTENT_RESCHEDULE,
    INTENT_OTHER,
}

# --- Process name (for action logging) ---
PROCESS_AVAILABILITY = "availability_bot"

# --- Limits ---
DEFAULT_MAX_CLARIFICATIONS = 3
