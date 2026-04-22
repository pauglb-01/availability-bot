"""Tests for AvailabilityEngine state machine transitions."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from availability.availability_share.constants import (
    STATE_AWAITING_REPLY,
    STATE_CLOSED,
    STATE_COLLECTING_AVAILABILITY,
    STATE_CONFIRMING_AVAILABILITY,
)
from availability.availability_share.engine import AvailabilityEngine
from availability.availability_share.schemas import Intent, LLMResponse, ResolvedSlot

# ── Fakes ────────────────────────────────────────────────────────


class FakeContactsRepo:
    def __init__(self, contacts: dict[str, dict] | None = None):
        self._by_phone = contacts or {}

    def find_by_phone(self, phone, *, conn=None):
        return self._by_phone.get(phone)


class FakeAvailabilityRepo:
    """In-memory repo that tracks conversations, messages, and slots."""

    def __init__(self):
        self._conversations: dict[int, dict] = {}
        self._messages: list[dict] = []
        self._slots: list[dict] = []
        self._next_conv_id = 1
        self._next_msg_id = 1
        self._next_slot_id = 1

    @contextmanager
    def transaction(self):
        yield None  # no real transaction

    def get_open_conversation(self, contact_id, *, conn=None):
        for c in self._conversations.values():
            if c["contact_id"] == contact_id and c["state"] != STATE_CLOSED:
                return dict(c)
        return None

    def get_conversation_messages(self, conversation_id, *, conn=None):
        return [m for m in self._messages if m["conversation_id"] == conversation_id]

    def create_conversation(self, *, contact_id, max_clarifications=3, conn=None):
        cid = self._next_conv_id
        self._next_conv_id += 1
        conv = {
            "id": cid,
            "contact_id": contact_id,
            "state": STATE_AWAITING_REPLY,
            "clarification_count": 0,
            "max_clarifications": max_clarifications,
        }
        self._conversations[cid] = conv
        return dict(conv)

    def insert_message(
        self,
        *,
        conversation_id,
        contact_id,
        direction,
        content_raw,
        intent=None,
        llm_raw_response=None,
        conn=None,
    ):
        mid = self._next_msg_id
        self._next_msg_id += 1
        msg = {
            "id": mid,
            "conversation_id": conversation_id,
            "contact_id": contact_id,
            "direction": direction,
            "content_raw": content_raw,
            "intent": intent,
        }
        self._messages.append(msg)
        return msg

    def insert_slots(
        self,
        *,
        contact_id,
        conversation_id,
        source_message_id,
        slots,
        project_id=None,
        conn=None,
    ):
        inserted = []
        for s in slots:
            sid = self._next_slot_id
            self._next_slot_id += 1
            row = {"id": sid, "contact_id": contact_id, "status": "active", **s}
            self._slots.append(row)
            inserted.append(row)
        return inserted

    def cancel_active_slots(self, conversation_id, *, conn=None):
        count = 0
        for s in self._slots:
            if s.get("status") == "active":
                s["status"] = "cancelled"
                count += 1
        return count

    def update_conversation_state(
        self,
        conversation_id,
        *,
        state,
        conn=None,
        increment_clarification=False,
        close=False,
    ):
        conv = self._conversations[conversation_id]
        conv["state"] = state
        if increment_clarification:
            conv["clarification_count"] = conv.get("clarification_count", 0) + 1
        if close:
            conv["closed_at"] = "now"
        return dict(conv)

    def get_active_slots(self, conversation_id, *, conn=None):
        return [s for s in self._slots if s.get("status") == "active"]


# ── Helpers ──────────────────────────────────────────────────────


def _make_engine(
    *, state=STATE_AWAITING_REPLY, contact_phone="34600111111", contact_id=1
):
    """Build an AvailabilityEngine with fakes and an open conversation in the given state."""
    engine = AvailabilityEngine.__new__(AvailabilityEngine)
    engine.contacts_repo = FakeContactsRepo(
        {
            contact_phone: {"id": contact_id, "phone": contact_phone, "name": "Test"},
        }
    )
    engine.repo = FakeAvailabilityRepo()
    # Pre-create a conversation
    conv = engine.repo.create_conversation(contact_id=contact_id)
    conv["state"] = state
    engine.repo._conversations[conv["id"]] = conv
    return engine, conv["id"]


def _mock_llm(intent: str, slots: list[dict] | None = None, reply: str = "ok"):
    """Return a patch context manager that mocks extract_availability."""
    resolved = [
        ResolvedSlot(start_ts=s["start_ts"], end_ts=s["end_ts"]) for s in (slots or [])
    ]
    response = LLMResponse(
        intent=Intent(intent), resolved_slots=resolved, reply_to_user=reply
    )
    return patch(
        "services.availability.availability_share.engine.extract_availability",
        return_value=response,
    )


# ── Tests ────────────────────────────────────────────────────────


class TestTransitionsFromAwaiting:
    """AWAITING_REPLY → any intent → COLLECTING_AVAILABILITY"""

    def test_availability_intent(self):
        engine, conv_id = _make_engine(state=STATE_AWAITING_REPLY)
        with _mock_llm(
            "availability",
            [{"start_ts": "2026-04-06T09:00:00", "end_ts": "2026-04-06T13:00:00"}],
        ):
            reply = engine.handle_message("34600111111", "El lunes de 9 a 13")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY
        assert reply is not None

    def test_other_intent(self):
        engine, conv_id = _make_engine(state=STATE_AWAITING_REPLY)
        with _mock_llm("other", reply="No entiendo"):
            engine.handle_message("34600111111", "hola qué tal")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY


class TestTransitionsFromCollecting:
    """COLLECTING with various intents."""

    def test_availability_with_slots_goes_to_confirming(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        with _mock_llm(
            "availability",
            [{"start_ts": "2026-04-06T09:00:00", "end_ts": "2026-04-06T13:00:00"}],
        ):
            engine.handle_message("34600111111", "El lunes de 9 a 13")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CONFIRMING_AVAILABILITY

    def test_availability_without_slots_stays(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        with _mock_llm("availability", [], reply="¿Qué días te vienen bien?"):
            engine.handle_message("34600111111", "pues no sé")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY

    def test_other_stays(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        with _mock_llm("other", reply="No entiendo"):
            engine.handle_message("34600111111", "qué tiempo hace")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY

    def test_reschedule_stays(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        with _mock_llm("reschedule", reply="Entendido"):
            engine.handle_message("34600111111", "mejor otro día")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY


class TestTransitionsFromConfirming:
    """CONFIRMING_AVAILABILITY transitions."""

    def test_confirmation_closes(self):
        engine, conv_id = _make_engine(state=STATE_CONFIRMING_AVAILABILITY)
        with _mock_llm("confirmation", reply="Perfecto, confirmado."):
            engine.handle_message("34600111111", "Sí, perfecto")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CLOSED
        assert "closed_at" in conv

    def test_availability_with_slots_stays_confirming(self):
        engine, conv_id = _make_engine(state=STATE_CONFIRMING_AVAILABILITY)
        with _mock_llm(
            "availability",
            [{"start_ts": "2026-04-07T09:00:00", "end_ts": "2026-04-07T13:00:00"}],
        ):
            engine.handle_message("34600111111", "También el martes")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CONFIRMING_AVAILABILITY

    def test_availability_without_slots_stays(self):
        engine, conv_id = _make_engine(state=STATE_CONFIRMING_AVAILABILITY)
        with _mock_llm("availability", [], reply="¿Cuándo exactamente?"):
            engine.handle_message("34600111111", "algún día más")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CONFIRMING_AVAILABILITY

    def test_reschedule_goes_to_collecting(self):
        engine, conv_id = _make_engine(state=STATE_CONFIRMING_AVAILABILITY)
        with _mock_llm("reschedule", reply="Entendido, replanificamos"):
            engine.handle_message("34600111111", "mejor cambiar todo")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY

    def test_other_stays(self):
        engine, conv_id = _make_engine(state=STATE_CONFIRMING_AVAILABILITY)
        with _mock_llm("other", reply="No entiendo"):
            engine.handle_message("34600111111", "qué hora es")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CONFIRMING_AVAILABILITY


class TestTransitionsFromClosed:
    """CLOSED transitions — can reopen on availability/reschedule."""

    def test_availability_reopens(self):
        engine, conv_id = _make_engine(state=STATE_CLOSED)
        with _mock_llm(
            "availability",
            [{"start_ts": "2026-04-08T09:00:00", "end_ts": "2026-04-08T13:00:00"}],
        ):
            engine.handle_message("34600111111", "El miércoles")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY

    def test_reschedule_reopens(self):
        engine, conv_id = _make_engine(state=STATE_CLOSED)
        with _mock_llm("reschedule", reply="Replanificamos"):
            engine.handle_message("34600111111", "quiero cambiar")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_COLLECTING_AVAILABILITY

    def test_confirmation_stays_closed(self):
        engine, conv_id = _make_engine(state=STATE_CLOSED)
        with _mock_llm("confirmation", reply="Ya estaba confirmado"):
            engine.handle_message("34600111111", "sí")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CLOSED

    def test_other_stays_closed(self):
        engine, conv_id = _make_engine(state=STATE_CLOSED)
        with _mock_llm("other", reply="Gracias"):
            engine.handle_message("34600111111", "gracias")
        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CLOSED


class TestUnknownPhone:
    """Messages from unknown phones return None."""

    def test_unknown_phone_returns_none(self):
        engine = AvailabilityEngine.__new__(AvailabilityEngine)
        engine.contacts_repo = FakeContactsRepo({})  # empty
        engine.repo = FakeAvailabilityRepo()
        result = engine.handle_message("34999999999", "hola")
        assert result is None


class TestNoOpenConversation:
    """Messages from known contacts without an open conversation return None."""

    def test_no_conversation_returns_none(self):
        engine = AvailabilityEngine.__new__(AvailabilityEngine)
        engine.contacts_repo = FakeContactsRepo(
            {
                "34600111111": {"id": 1, "phone": "34600111111"},
            }
        )
        engine.repo = FakeAvailabilityRepo()
        # No conversation created
        result = engine.handle_message("34600111111", "hola")
        assert result is None


class TestClarificationAutoClose:
    """Conversation auto-closes after max_clarifications vague replies."""

    def test_auto_close_on_max_clarifications(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        # Set clarification_count just below max
        engine.repo._conversations[conv_id]["clarification_count"] = 2
        engine.repo._conversations[conv_id]["max_clarifications"] = 3

        with _mock_llm("other", reply="No entiendo"):
            reply = engine.handle_message("34600111111", "blah blah")

        conv = engine.repo._conversations[conv_id]
        assert conv["state"] == STATE_CLOSED
        assert "gestor" in reply.lower()  # auto-close message mentions manager


class TestSlotPersistence:
    """Slots are persisted to the repo."""

    def test_slots_saved(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        slots = [
            {"start_ts": "2026-04-06T09:00:00", "end_ts": "2026-04-06T13:00:00"},
            {"start_ts": "2026-04-07T09:00:00", "end_ts": "2026-04-07T13:00:00"},
        ]
        with _mock_llm("availability", slots):
            engine.handle_message("34600111111", "Lunes y martes por la mañana")
        assert len(engine.repo._slots) == 2
        assert all(s["status"] == "active" for s in engine.repo._slots)


class TestRescheduleCancelsSlots:
    """Reschedule cancels existing active slots."""

    def test_reschedule_cancels_and_adds_new(self):
        engine, conv_id = _make_engine(state=STATE_CONFIRMING_AVAILABILITY)
        # Pre-insert some active slots
        engine.repo._slots.append(
            {"id": 99, "status": "active", "start_ts": "old", "end_ts": "old"}
        )

        new_slots = [
            {"start_ts": "2026-04-10T09:00:00", "end_ts": "2026-04-10T13:00:00"}
        ]
        with _mock_llm("reschedule", new_slots, reply="Cambio anotado"):
            engine.handle_message("34600111111", "mejor el viernes")

        # Old slot cancelled
        assert engine.repo._slots[0]["status"] == "cancelled"
        # New slot added
        active = [s for s in engine.repo._slots if s["status"] == "active"]
        assert len(active) == 1


class TestMessagesPersisted:
    """Both inbound and outbound messages are saved."""

    def test_messages_saved(self):
        engine, conv_id = _make_engine(state=STATE_COLLECTING_AVAILABILITY)
        with _mock_llm(
            "availability",
            [{"start_ts": "2026-04-06T09:00:00", "end_ts": "2026-04-06T13:00:00"}],
            reply="Anotado",
        ):
            engine.handle_message("34600111111", "El lunes")
        messages = engine.repo._messages
        assert len(messages) == 2  # inbound + outbound
        assert messages[0]["direction"] == "inbound"
        assert messages[1]["direction"] == "outbound"
        assert messages[0]["intent"] == "availability"
