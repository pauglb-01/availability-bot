from __future__ import annotations

import logging
from dataclasses import dataclass

from availability.availability_share.constants import (
    INTENT_AVAILABILITY,
    INTENT_CONFIRMATION,
    INTENT_OTHER,
    INTENT_RESCHEDULE,
    STATE_AWAITING_REPLY,
    STATE_CLOSED,
    STATE_COLLECTING_AVAILABILITY,
    STATE_CONFIRMING_AVAILABILITY,
)
from availability.availability_share.extractor import extract_availability
from availability.availability_share.schemas import LLMResponse
from availability.availability_share.stores.availability_repo import AvailabilityRepo
from availability.availability_share.stores.contacts_repo import ContactsRepo

logger = logging.getLogger(__name__)


@dataclass
class TriggerResult:
    conversation_id: int
    contact_id: int
    phone: str
    greeting: str


class TriggerError(Exception):
    """Raised when trigger_conversation cannot proceed."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code  # "not_found", "no_contact", "no_phone", "conflict"


def _build_greeting(contact_name: str, project_name: str) -> str:
    parts = ["¡Hola"]
    if contact_name:
        parts[0] += f", {contact_name}"
    parts[0] += "! Soy el asistente de disponibilidad."

    if project_name:
        parts.append(f'Necesitamos programar una visita para la obra "{project_name}".')
    else:
        parts.append("Necesitamos programar una visita a tu obra.")

    parts.append(
        "¿Podrías indicarme qué días y horas te vienen bien en las próximas dos semanas?"
    )
    return " ".join(parts)


class AvailabilityEngine:
    """
    State machine for the availability chatbot.

    Transition table (from CLAUDE.md, with fixes):

        State          | Intent          | resolved_slots | Next state
        ───────────────┼─────────────────┼────────────────┼──────────────────
        AWAITING       | availability    | not empty      | CONFIRMING
        AWAITING       | other intents   | —              | COLLECTING
        COLLECTING     | availability    | not empty      | CONFIRMING
        COLLECTING     | availability    | empty          | COLLECTING
        COLLECTING     | other intents   | —              | COLLECTING
        CONFIRMING     | confirmation    | —              | CLOSED
        CONFIRMING     | availability    | not empty      | CONFIRMING
        CONFIRMING     | availability    | empty          | CONFIRMING
        CONFIRMING     | reschedule      | —              | COLLECTING
        CONFIRMING     | other           | —              | CONFIRMING
        CLOSED         | avail/reschedule| —              | CONFIRMING
        CLOSED         | confirm/other   | —              | CLOSED
    """

    def __init__(self) -> None:
        self.repo = AvailabilityRepo()
        self.contacts_repo = ContactsRepo()

    def trigger_conversation(self, project_id: int) -> TriggerResult:
        """
        Start or reopen an availability conversation for a project's contact.

        Raises TriggerError if the project/contact is invalid or a conversation
        is already active.

        Returns TriggerResult with conversation_id, contact info, and greeting.
        The caller is responsible for delivering the greeting (WhatsApp, terminal, etc.).
        """
        project = self.contacts_repo.find_project_by_id(project_id)
        if project is None:
            raise TriggerError(f"Project {project_id} not found", code="not_found")

        contact_id = project.get("contact_id")
        if not contact_id:
            raise TriggerError(
                f"Project {project_id} has no linked contact", code="no_contact"
            )

        contact = self.contacts_repo.find_by_id(contact_id)
        if contact is None:
            raise TriggerError(
                f"Contact {contact_id} linked to project {project_id} not found",
                code="not_found",
            )

        phone = contact.get("phone")
        if not phone:
            raise TriggerError(
                f"Contact {contact_id} has no phone number", code="no_phone"
            )

        existing = self.repo.get_conversation_for_contact(contact_id)

        if existing is not None and existing["state"] != STATE_CLOSED:
            raise TriggerError(
                f"Contact {contact_id} already has an open conversation "
                f"(id={existing['id']}, state={existing['state']})",
                code="conflict",
            )

        contact_name = contact.get("name") or ""
        project_name = project.get("name") or project.get("description") or ""
        greeting = _build_greeting(contact_name, project_name)

        with self.repo.transaction() as conn:
            if existing is not None:
                conversation = self.repo.reopen_conversation(existing["id"], conn=conn)
            else:
                conversation = self.repo.create_conversation(
                    contact_id=contact_id,
                    conn=conn,
                )

            conversation_id = int(conversation["id"])

            self.repo.insert_message(
                conversation_id=conversation_id,
                contact_id=None,
                direction="outbound",
                content_raw=greeting,
                conn=conn,
            )

        logger.info(
            "Availability conversation triggered: conversation_id=%s project_id=%s contact_id=%s",
            conversation_id,
            project_id,
            contact_id,
        )

        return TriggerResult(
            conversation_id=conversation_id,
            contact_id=contact_id,
            phone=phone,
            greeting=greeting,
        )

    def handle_message(self, phone: str, text: str) -> str | None:
        """
        Process an inbound text message from a WhatsApp user.

        Returns:
            The reply text to send back, or None if the message should be ignored.
        """
        contact = self.contacts_repo.find_by_phone(phone)
        if contact is None:
            logger.warning("Inbound availability message from unknown phone: %s", phone)
            return None

        contact_id = int(contact["id"])
        conversation = self.repo.get_conversation_for_contact(contact_id)

        if conversation is None:
            logger.warning(
                "No conversation exists for contact_id=%s phone=%s — ignoring.",
                contact_id,
                phone,
            )
            return None

        conversation_id = int(conversation["id"])
        state = conversation["state"]

        # Retrieve conversation history for LLM context
        history = self.repo.get_conversation_messages(conversation_id)

        # Call LLM
        llm_response = extract_availability(
            user_message=text,
            conversation_history=history,
            state=state,
        )

        if state == STATE_CLOSED:
            return self._handle_closed_message(
                contact_id=contact_id,
                conversation_id=conversation_id,
                text=text,
                llm_response=llm_response,
            )

        with self.repo.transaction() as conn:
            # Save inbound message
            inbound_msg = self.repo.insert_message(
                conversation_id=conversation_id,
                contact_id=contact_id,
                direction="inbound",
                content_raw=text,
                intent=llm_response.intent.value,
                llm_raw_response=llm_response.model_dump(),
                conn=conn,
            )

            # Compute next state + side effects
            next_state, should_close = self._compute_transition(
                state=state,
                intent=llm_response.intent.value,
                has_slots=len(llm_response.resolved_slots) > 0,
            )

            # On confirmation → CLOSED: persist the confirmed slots
            if should_close:
                confirmed_slots = self._collect_confirmed_slots(
                    conversation_id, conn=conn
                )
                if confirmed_slots:
                    self.repo.insert_slots(
                        contact_id=contact_id,
                        conversation_id=conversation_id,
                        source_message_id=int(inbound_msg["id"]),
                        slots=confirmed_slots,
                        conn=conn,
                    )

            # Check clarification auto-close
            increment_clarification = (
                llm_response.intent.value in {INTENT_OTHER, INTENT_AVAILABILITY}
                and len(llm_response.resolved_slots) == 0
                and next_state != STATE_CLOSED
            )

            conv = self.repo.update_conversation_state(
                conversation_id,
                state=next_state,
                conn=conn,
                increment_clarification=increment_clarification,
                close=should_close,
            )

            # Auto-close if clarification limit reached
            if not should_close and int(conv.get("clarification_count", 0)) >= int(
                conv.get("max_clarifications", 3)
            ):
                next_state = STATE_CLOSED
                conv = self.repo.update_conversation_state(
                    conversation_id,
                    state=STATE_CLOSED,
                    conn=conn,
                    close=True,
                )
                reply_text = (
                    "No hemos podido concretar tu disponibilidad tras varios intentos. "
                    "Tu gestor se pondrá en contacto contigo directamente."
                )
                self.repo.insert_message(
                    conversation_id=conversation_id,
                    contact_id=None,
                    direction="outbound",
                    content_raw=reply_text,
                    conn=conn,
                )
                logger.info(
                    "Auto-closed conversation_id=%s (max clarifications reached)",
                    conversation_id,
                )
                return reply_text

            # Save outbound message
            self.repo.insert_message(
                conversation_id=conversation_id,
                contact_id=None,
                direction="outbound",
                content_raw=llm_response.reply_to_user,
                conn=conn,
            )

        logger.info(
            "Availability transition: conversation_id=%s state=%s→%s intent=%s slots=%d",
            conversation_id,
            state,
            next_state,
            llm_response.intent.value,
            len(llm_response.resolved_slots),
        )

        return llm_response.reply_to_user

    def _handle_closed_message(
        self,
        *,
        contact_id: int,
        conversation_id: int,
        text: str,
        llm_response: LLMResponse,
    ) -> str | None:
        """Handle an inbound message on a CLOSED conversation."""
        intent = llm_response.intent.value

        with self.repo.transaction() as conn:
            self.repo.insert_message(
                conversation_id=conversation_id,
                contact_id=contact_id,
                direction="inbound",
                content_raw=text,
                intent=intent,
                llm_raw_response=llm_response.model_dump(),
                conn=conn,
            )

            if intent == INTENT_CONFIRMATION:
                logger.info(
                    "CLOSED conversation_id=%s: confirmation ignored.", conversation_id
                )
                return None

            if intent == INTENT_OTHER:
                fallback = (
                    "Lo siento, el asistente de disponibilidad no está disponible en este momento. "
                    "Tu gestor se pondrá en contacto contigo."
                )
                self.repo.insert_message(
                    conversation_id=conversation_id,
                    contact_id=None,
                    direction="outbound",
                    content_raw=fallback,
                    conn=conn,
                )
                return fallback

            # availability or reschedule → keep slots, reopen to COLLECTING
            existing_slots = self.repo.get_active_slots(conversation_id, conn=conn)
            reply = self._build_reengagement_message(existing_slots)
            self.repo.update_conversation_state(
                conversation_id,
                state=STATE_CONFIRMING_AVAILABILITY,
                conn=conn,
            )
            self.repo.insert_message(
                conversation_id=conversation_id,
                contact_id=None,
                direction="outbound",
                content_raw=reply,
                conn=conn,
            )
            logger.info(
                "CLOSED conversation_id=%s reopened to CONFIRMING (intent=%s)",
                conversation_id,
                intent,
            )
            return reply

    def _build_reengagement_message(self, slots: list[dict]) -> str:
        """Build a re-engagement message listing existing active slots."""
        from datetime import datetime

        DAYS_ES = [
            "lunes",
            "martes",
            "miércoles",
            "jueves",
            "viernes",
            "sábado",
            "domingo",
        ]
        MONTHS_ES = [
            "",
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        ]

        if not slots:
            return (
                "¿Quieres indicarme tu disponibilidad? "
                "Dime qué días y horas te vienen bien en las próximas dos semanas."
            )

        lines = []
        for slot in slots:
            start = datetime.fromisoformat(str(slot["start_ts"]))
            end = datetime.fromisoformat(str(slot["end_ts"]))
            day_name = DAYS_ES[start.weekday()]
            month_name = MONTHS_ES[start.month]
            time_range = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
            lines.append(f"- {day_name} {start.day} de {month_name}, {time_range}")

        slots_text = "\n".join(lines)
        return (
            f"Tenías registrada la siguiente disponibilidad:\n{slots_text}\n\n"
            "¿Quieres modificarla? Indícame qué días y horas te vienen bien."
        )

    def _compute_transition(
        self,
        *,
        state: str,
        intent: str,
        has_slots: bool,
    ) -> tuple[str, bool]:
        """
        Return (next_state, should_close) based on the transition table.
        """
        # AWAITING_REPLY → availability with slots → CONFIRMING
        if state == STATE_AWAITING_REPLY:
            if intent == INTENT_AVAILABILITY and has_slots:
                return STATE_CONFIRMING_AVAILABILITY, False
            return STATE_COLLECTING_AVAILABILITY, False

        # COLLECTING
        if state == STATE_COLLECTING_AVAILABILITY:
            if intent == INTENT_AVAILABILITY and has_slots:
                return STATE_CONFIRMING_AVAILABILITY, False
            # availability without slots, or reschedule/confirmation/other → stay
            return STATE_COLLECTING_AVAILABILITY, False

        # CONFIRMING
        if state == STATE_CONFIRMING_AVAILABILITY:
            if intent == INTENT_CONFIRMATION:
                return STATE_CLOSED, True
            if intent == INTENT_AVAILABILITY and has_slots:
                return STATE_CONFIRMING_AVAILABILITY, False
            if intent == INTENT_RESCHEDULE:
                return STATE_COLLECTING_AVAILABILITY, False
            # availability without slots, or other → stay
            return STATE_CONFIRMING_AVAILABILITY, False

        # CLOSED
        if state == STATE_CLOSED:
            if intent in {INTENT_AVAILABILITY, INTENT_RESCHEDULE}:
                return STATE_CONFIRMING_AVAILABILITY, False
            return STATE_CLOSED, False

        # Fallback: stay in current state
        logger.warning("Unknown state %r, staying in place.", state)
        return state, False

    def _collect_confirmed_slots(
        self,
        conversation_id: int,
        *,
        conn,
    ) -> list[dict]:
        """
        Return the complete set of confirmed slots.

        The LLM always returns ALL currently agreed slots in resolved_slots,
        so we just take the slots from the last inbound message that has them.
        """
        import json

        messages = self.repo.get_conversation_messages(conversation_id, conn=conn)

        for msg in reversed(messages):
            if msg.get("direction") != "inbound":
                continue
            raw = msg.get("llm_raw_response")
            if not raw:
                continue
            if isinstance(raw, str):
                raw = json.loads(raw)

            slots = raw.get("resolved_slots", [])
            if slots:
                return [
                    {"start_ts": s["start_ts"], "end_ts": s["end_ts"]} for s in slots
                ]

        return []
