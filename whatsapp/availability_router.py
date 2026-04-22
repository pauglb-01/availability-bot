from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from availability.availability_share.engine import AvailabilityEngine, TriggerError
from whatsapp.whatsapp_share.flow.outbound_dispatcher import WhatsAppOutboundDispatcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/availability", tags=["availability"])

_ENGINE = AvailabilityEngine()
_OUTBOUND = WhatsAppOutboundDispatcher(logger_name=__name__)


class TriggerRequest(BaseModel):
    project_id: int


class TriggerResponse(BaseModel):
    conversation_id: int
    project_id: int
    contact_id: int
    state: str


_ERROR_CODE_TO_HTTP = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "no_contact": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "no_phone": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "conflict": status.HTTP_409_CONFLICT,
}


@router.post("/trigger", response_model=TriggerResponse, status_code=201)
async def trigger_availability(
    payload: TriggerRequest,
) -> TriggerResponse:
    """
    Manager triggers an availability conversation for a project's contact.
    Looks up the contact from the project, creates/reopens the conversation,
    and sends the first WhatsApp message.
    """
    try:
        result = _ENGINE.trigger_conversation(payload.project_id)
    except TriggerError as exc:
        raise HTTPException(
            status_code=_ERROR_CODE_TO_HTTP.get(exc.code, 400),
            detail=str(exc),
        )

    # Send greeting via WhatsApp (outside DB transaction)
    _OUTBOUND.send_text_message(phone=result.phone, text=result.greeting)

    return TriggerResponse(
        conversation_id=result.conversation_id,
        project_id=payload.project_id,
        contact_id=result.contact_id,
        state="AWAITING_REPLY",
    )
    return " ".join(parts)
