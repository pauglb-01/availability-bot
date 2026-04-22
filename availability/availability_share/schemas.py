from enum import Enum
from typing import List

from pydantic import BaseModel


class Intent(str, Enum):
    availability = "availability"
    confirmation = "confirmation"
    reschedule = "reschedule"
    other = "other"


class ResolvedSlot(BaseModel):
    start_ts: str  # ISO 8601 datetime, e.g. "2026-03-24T09:00:00"
    end_ts: str  # ISO 8601 datetime, e.g. "2026-03-24T13:00:00"


class LLMResponse(BaseModel):
    intent: Intent
    resolved_slots: List[ResolvedSlot]
    reply_to_user: str
