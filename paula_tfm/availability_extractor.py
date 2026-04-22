from datetime import datetime, timedelta

from connect.openai_client import responses_structured
from paula_tfm.schemas import LLMResponse

# TODO: checkear esto, si cambiar el nombre a schedule y moverlo a modulo constants
# Company working hours — edit here to change available time ranges shown to the LLM.
TIME_RANGES = (
    "- mañana: 08:00–13:00\n"
    "- mediodía: 13:00–15:00\n"
    "- tarde: 15:00–20:00\n"
    "- sin hora: 08:00–20:00"
)


SYSTEM_PROMPT = """
Eres un asistente que extrae disponibilidad horaria de clientes en español.

Estado: {state}
Hoy: {today_date}. Zona horaria: Madrid.
Ventana: {date_from} – {date_to} (14 días)

Franjas:
{time_ranges}
(Si no se indica hora, usar "sin hora")

Objetivo:
- clasificar intención
- extraer disponibilidad (ISO 8601)
- responder al cliente (breve, español)

Intents:
availability · confirmation · reschedule · other

Reglas:
- Día sin hora → "sin hora"
- Vago → no resolver y pedir concreción
- Parcial → resolver lo claro y preguntar lo faltante
- "los <día>" → todos dentro de la ventana
- "a partir de <día>" → desde ese día hasta fin de ventana
- Deduplicar → usar la más específica
- Solo fechas dentro de la ventana

Output (siempre JSON){{
  "intent": "<intent>",
  "resolved_slots": [
    {{"start_ts": "YYYY-MM-DDTHH:MM:SS", "end_ts": "YYYY-MM-DDTHH:MM:SS"}}
  ],
  "reply_to_user": "<mensaje breve, natural en español>"
}}
"""


# Few-shot examples
EXAMPLES = [
    {"role": "user", "content": "El martes de 9 a 12"},
    {
        "role": "assistant",
        "content": """{
  "intent": "availability",
  "resolved_slots": [
    { "start_ts": "2026-03-24T09:00:00", "end_ts": "2026-03-24T12:00:00" }
  ],
  "reply_to_user": "Perfecto, te apunto disponible el martes 24 de marzo de 09:00 a 12:00. ¿Es correcto?"
}""",
    },
    {"role": "user", "content": "La semana que viene por las mañanas"},
    {
        "role": "assistant",
        "content": """{
  "intent": "availability",
  "resolved_slots": [
    { "start_ts": "2026-03-23T09:00:00", "end_ts": "2026-03-23T13:00:00" },
    { "start_ts": "2026-03-24T09:00:00", "end_ts": "2026-03-24T13:00:00" },
    { "start_ts": "2026-03-25T09:00:00", "end_ts": "2026-03-25T13:00:00" },
    { "start_ts": "2026-03-26T09:00:00", "end_ts": "2026-03-26T13:00:00" },
    { "start_ts": "2026-03-27T09:00:00", "end_ts": "2026-03-27T13:00:00" }
  ],
  "reply_to_user": "Entendido, te apunto disponible de lunes a viernes de la semana que viene por las mañanas (09:00–13:00). ¿Es correcto?"
}""",
    },
    {
        "role": "user",
        "content": "El jueves por la tarde, y también el viernes pero no sé a qué hora",
    },
    {
        "role": "assistant",
        "content": """{
  "intent": "availability",
  "resolved_slots": [
    { "start_ts": "2026-03-26T13:00:00", "end_ts": "2026-03-26T18:00:00" }
  ],
  "reply_to_user": "Anotado el jueves 26 de marzo por la tarde (13:00–18:00). ¿A qué hora te vendría bien el viernes?"
}""",
    },
    {"role": "user", "content": "Sí, eso está bien"},
    {
        "role": "assistant",
        "content": """{
  "intent": "confirmation",
  "resolved_slots": [],
  "reply_to_user": "Perfecto, queda confirmado. En breve te contactaremos para cerrar la cita."
}""",
    },
    {
        "role": "user",
        "content": "Sí pero el miércoles mejor por la mañana, no por la tarde",
    },
    {
        "role": "assistant",
        "content": """{
  "intent": "reschedule",
  "resolved_slots": [
    { "start_ts": "2026-03-25T09:00:00", "end_ts": "2026-03-25T13:00:00" }
  ],
  "reply_to_user": "Entendido, cambio el miércoles 25 de marzo a mañana (09:00–13:00). ¿El resto queda igual?"
}""",
    },
]


def format_conversation_history(messages: list[dict]) -> list[dict]:
    """Convert saved DB messages to LLM conversation format."""
    return [
        {
            "role": "user" if m["direction"] == "inbound" else "assistant",
            "content": m["content_raw"],
        }
        for m in messages
    ]


def extract_availability(
    user_message: str, conversation_history: list[dict] | None = None, state: str = ""
) -> LLMResponse:
    """
    Call the LLM to classify intent and resolve availability slots.

    Args:
        user_message: the latest inbound message from the client.
        conversation_history: list of prior DB message dicts (direction + content_raw).
        state: current conversation state, included in the system prompt.

    Returns:
        LLMResponse with intent, resolved_slots, reply_to_user.
    """
    today = datetime.today()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=14)).strftime("%Y-%m-%d")
    today_date = today.strftime("%Y-%m-%d")
    system_message = {
        "role": "system",
        "content": SYSTEM_PROMPT.format(
            state=state,
            today_date=today_date,
            date_from=date_from,
            date_to=date_to,
            time_ranges=TIME_RANGES,
        ),
    }

    history_messages = format_conversation_history(conversation_history or [])

    messages = [
        system_message,
        *EXAMPLES,
        *history_messages,
        {"role": "user", "content": user_message},
    ]

    # TODO: delete when debugged
    print(f"[DEBUG] state={state!r}  message={user_message!r}")
    result = responses_structured(messages, pydantic_model=LLMResponse)
    print(f"[DEBUG] intent={result.intent.value}  slots={len(result.resolved_slots)}")
    return result
