import json
import logging
from dataclasses import dataclass

from availability.availability_share.engine import AvailabilityEngine
from availability.availability_share.stores.contacts_repo import ContactsRepo
from whatsapp.whatsapp_share.flow.constants import PROCESS_DAILY_FLOW
from whatsapp.whatsapp_share.flow.daily_flow_engine import DailyFlowEngine
from whatsapp.whatsapp_share.flow.models import InboundEvent
from whatsapp.whatsapp_share.flow.outbound_dispatcher import WhatsAppOutboundDispatcher
from whatsapp.whatsapp_share.stores.ai_job_store import AIJobStore
from whatsapp.whatsapp_share.stores.daily_flow_repo import DailyFlowRepository
from whatsapp.whatsapp_share.stores.voice_input_store import VoiceInputStore
from whatsapp.whatsapp_share.stores.whatsapp_action_store import WhatsAppActionStore
from whatsapp.whatsapp_share.stores.whatsapp_inbound_store import WhatsAppInboundStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessageMeta:
    phone: str | None
    wa_message_id: str | None
    msg_type: str | None
    wa_media_id: str | None = None
    media_mime: str | None = None


_ENGINE = DailyFlowEngine()
_AVAILABILITY_ENGINE = AvailabilityEngine()
_CONTACTS_REPO = ContactsRepo()
_REPO = DailyFlowRepository()
_ACTION_STORE = WhatsAppActionStore()
_INBOUND_STORE = WhatsAppInboundStore()
_VOICE_INPUT_STORE = VoiceInputStore()
_AI_JOB_STORE = AIJobStore()
_OUTBOUND_DISPATCHER = WhatsAppOutboundDispatcher(logger_name=__name__)


def handle_status_updates(value, _):
    return {"status": "message status updated"}, 200


def handle_new_message(value, raw_body):
    summary = _summarize_inbound(value, raw_body)
    logger.info("Inbound message payload: %s", summary)

    message_meta = _extract_inbound_message_meta(value)
    if message_meta is None:
        logger.info("Inbound message ignored (empty messages array).")
        return {"status": "ignored"}, 200

    event = _parse_inbound_event(value)
    message_type = _resolve_message_type(message_meta.msg_type, event) or "unknown"

    with _INBOUND_STORE.transaction() as conn:
        inbound_id = None
        attempts = None
        if message_meta.wa_message_id and message_meta.phone:
            inbound_id, attempts = _INBOUND_STORE.upsert_inbound(
                wa_message_id=message_meta.wa_message_id,
                phone=message_meta.phone,
                message_type=message_type,
                wa_media_id=message_meta.wa_media_id,
                conn=conn,
            )
        elif not message_meta.wa_message_id:
            logger.warning(
                "Inbound message without wa_message_id; idempotency row skipped."
            )
        else:
            logger.warning(
                "Inbound message without phone; idempotency row skipped: wa_message_id=%s",
                message_meta.wa_message_id,
            )

        log_id = _ACTION_STORE.log_action(
            process_name=PROCESS_DAILY_FLOW,
            action_type="INBOUND_EVENT",
            direction="inbound",
            phone=message_meta.phone,
            wa_message_id=message_meta.wa_message_id,
            message_type=message_type,
            button_id=event.button_id if event else None,
            message_text=event.text if event else None,
            payload={"summary": summary},
            conn=conn,
        )

        if inbound_id is not None and log_id is not None:
            _INBOUND_STORE.set_last_log_id(
                inbound_id=inbound_id, log_id=log_id, conn=conn
            )

        if message_meta.msg_type == "audio":
            _handle_audio_event(
                message_meta=message_meta,
                inbound_id=inbound_id,
                log_id=log_id,
                attempts=attempts,
                conn=conn,
            )
            return {"status": "accepted_audio"}, 200

    if event is None:
        logger.info(
            "Inbound message ignored (unsupported type=%s).", message_meta.msg_type
        )
        return {"status": "ignored"}, 200

    # ── Availability bot routing ─────────────────────────────────
    # If the phone belongs to a tfm_bot contact with an open conversation,
    # route to the AvailabilityEngine instead of the daily flow.
    if event.kind == "text" and event.text and event.phone:
        availability_reply = _try_availability_engine(event.phone, event.text)
        if availability_reply is not None:
            _OUTBOUND_DISPATCHER.send_text_message(
                phone=event.phone, text=availability_reply
            )
            return {"status": "ok_availability"}, 200

    logger.info(
        "Inbound event parsed: phone=%s kind=%s text_len=%s button_id=%s",
        event.phone,
        event.kind,
        len(event.text or "") if event.text is not None else 0,
        event.button_id,
    )

    responses = _ENGINE.handle_event(event)
    logger.info("DailyFlowEngine responses count=%s", len(responses))
    _OUTBOUND_DISPATCHER.dispatch_responses_and_log(
        action_store=_ACTION_STORE,
        process_name=PROCESS_DAILY_FLOW,
        phone=event.phone,
        responses=responses,
    )

    return {"status": "ok"}, 200


def _handle_audio_event(
    *,
    message_meta: InboundMessageMeta,
    inbound_id: str | None,
    log_id: int | None,
    attempts: int | None,
    conn,
) -> None:
    if not inbound_id:
        logger.warning(
            "Audio inbound without persisted inbound row: wa_message_id=%s phone=%s",
            message_meta.wa_message_id,
            message_meta.phone,
        )
        return
    if not log_id:
        logger.warning(
            "Audio inbound without inbound log_id: inbound_id=%s", inbound_id
        )
        return
    if not message_meta.wa_media_id:
        logger.warning(
            "Audio inbound without wa_media_id: wa_message_id=%s inbound_id=%s",
            message_meta.wa_message_id,
            inbound_id,
        )
        return

    user_id, team_id, session_id, task_id = _resolve_audio_context(
        message_meta.phone, conn=conn
    )

    voice_input_id = _VOICE_INPUT_STORE.create_voice_input_if_missing(
        inbound_id=inbound_id,
        log_id=log_id,
        user_id=user_id,
        team_id=team_id,
        session_id=session_id,
        task_id=task_id,
        phone=message_meta.phone,
        wa_media_id=message_meta.wa_media_id,
        media_mime=message_meta.media_mime,
        conn=conn,
    )
    _AI_JOB_STORE.enqueue_media_fetch_if_missing(
        voice_input_id=voice_input_id, conn=conn
    )

    logger.info(
        "Audio inbound accepted: inbound_id=%s voice_input_id=%s attempts=%s",
        inbound_id,
        voice_input_id,
        attempts,
    )


def _try_availability_engine(phone: str, text: str) -> str | None:
    """
    Check if the phone belongs to a tfm_bot contact with an open availability
    conversation. If so, route through the AvailabilityEngine and return the
    reply text. Returns None if this phone is not in the availability domain.
    """
    try:
        contact = _CONTACTS_REPO.find_by_phone(phone)
        if contact is None:
            return None
        return _AVAILABILITY_ENGINE.handle_message(phone, text)
    except Exception:
        logger.exception("AvailabilityEngine error for phone=%s", phone)
        return None


def _resolve_audio_context(
    phone: str | None, conn
) -> tuple[int | None, int | None, int | None, int | None]:
    if not phone:
        return None, None, None, None

    try:
        user, person = _REPO.resolve_user_person_by_phone(phone, conn=conn)
        if not user or not person:
            return None, None, None, None

        user_id = int(user["id"])
        team_id = _REPO.get_team_id_by_user_id(user_id, conn=conn)
        session = _REPO.get_session_today(user_id, conn=conn)
        if not session:
            return user_id, team_id, None, None

        session_id = int(session["id"]) if session.get("id") is not None else None
        task_id_raw = session.get("active_task_task_id")
        task_id = int(task_id_raw) if task_id_raw is not None else None
        return user_id, team_id, session_id, task_id
    except Exception:
        logger.exception("Failed to resolve audio context: phone=%s", phone)
        return None, None, None, None


def _extract_inbound_message_meta(value: dict) -> InboundMessageMeta | None:
    messages = value.get("messages") or []
    if not messages:
        return None

    message = messages[0] or {}
    msg_type = message.get("type")
    audio = message.get("audio") or {}
    return InboundMessageMeta(
        phone=message.get("from"),
        wa_message_id=message.get("id"),
        msg_type=msg_type,
        wa_media_id=audio.get("id") if msg_type == "audio" else None,
        media_mime=audio.get("mime_type") if msg_type == "audio" else None,
    )


def _resolve_message_type(
    raw_message_type: str | None, event: InboundEvent | None
) -> str | None:
    if event is not None:
        return event.kind
    return raw_message_type


def _parse_inbound_event(value) -> InboundEvent | None:
    messages = value.get("messages") or []
    if not messages:
        return None
    message = messages[0]
    phone = message.get("from")
    wa_message_id = message.get("id")
    if not phone:
        return None

    msg_type = message.get("type")
    if msg_type == "text":
        body = (message.get("text") or {}).get("body") or ""
        return InboundEvent(
            phone=phone, kind="text", text=body, message_id=wa_message_id
        )

    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        if interactive.get("type") == "button_reply":
            reply = interactive.get("button_reply") or {}
            button_id = reply.get("id")
            if button_id:
                return InboundEvent(
                    phone=phone,
                    kind="button",
                    button_id=button_id,
                    message_id=wa_message_id,
                )

    return None


def _summarize_inbound(value: dict, raw_body: dict | None) -> str:
    try:
        summary = {
            "messages": len(value.get("messages") or []),
            "statuses": len(value.get("statuses") or []),
            "metadata": value.get("metadata"),
        }
        if value.get("messages"):
            msg = value["messages"][0]
            msg_type = msg.get("type")
            summary["message_type"] = msg_type
            summary["from"] = msg.get("from")
            if msg_type == "text":
                summary["text_preview"] = _truncate(
                    (msg.get("text") or {}).get("body", ""), 120
                )
            if msg_type == "audio":
                summary["audio_id"] = (msg.get("audio") or {}).get("id")
        return json.dumps(summary, ensure_ascii=False)
    except Exception:
        return _truncate(str(raw_body or value), 500)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
