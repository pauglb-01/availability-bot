from enum import Enum
from typing import List

from pydantic import BaseModel


class Intent(str, Enum):
    availability = "availability"  # client giving availability (full or partial)
    confirmation = "confirmation"  # client accepts reflected slots
    reschedule = "reschedule"  # client wants to reschedule
    other = "other"  # anything unclassifiable


class ResolvedSlot(BaseModel):
    start_ts: str  # ISO 8601 datetime, e.g. "2026-03-24T09:00:00"
    end_ts: str  # ISO 8601 datetime, e.g. "2026-03-24T13:00:00"


class LLMResponse(BaseModel):
    intent: Intent
    resolved_slots: List[ResolvedSlot]
    # exact message to send to the client, written by the LLM, sent verbatim
    reply_to_user: str
