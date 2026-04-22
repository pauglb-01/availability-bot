"""
Gradio demo for the availability chatbot.

Simulates a WhatsApp conversation in the browser using the REAL engine,
real DB, and real LLM — no WhatsApp required.

Usage:
    python scripts/availability_gradio.py

Then open http://localhost:7860 in your browser.

Requires:
  - pip install gradio
  - .env with POSTGRES_* and OPENAI_API_KEY
  - At least one project with a linked contact in tfm_bot.projects / tfm_bot.contacts
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

# ── Project root on sys.path ─────────────────────────────────────
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gradio as gr

from availability.availability_share.constants import (
    STATE_AWAITING_REPLY,
    STATE_CLOSED,
    STATE_COLLECTING_AVAILABILITY,
    STATE_CONFIRMING_AVAILABILITY,
)
from availability.availability_share.engine import AvailabilityEngine, TriggerError
from connect.postgres_client import get_postgres_pool

# ── Formatting helpers ────────────────────────────────────────────

_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = [
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

_STATE_LABELS = {
    STATE_AWAITING_REPLY: "⏳ Esperando respuesta",
    STATE_COLLECTING_AVAILABILITY: "📝 Recogiendo disponibilidad",
    STATE_CONFIRMING_AVAILABILITY: "✅ Confirmando slots",
    STATE_CLOSED: "🔒 Cerrada",
}

_STATE_COLOURS = {
    STATE_AWAITING_REPLY: "#e67e22",
    STATE_COLLECTING_AVAILABILITY: "#2980b9",
    STATE_CONFIRMING_AVAILABILITY: "#27ae60",
    STATE_CLOSED: "#7f8c8d",
}


def _state_badge(state: str) -> str:
    label = _STATE_LABELS.get(state, state)
    colour = _STATE_COLOURS.get(state, "#7f8c8d")
    return (
        f'<span style="background:{colour};color:white;padding:4px 12px;'
        f'border-radius:12px;font-size:0.85em;font-weight:600">{label}</span>'
    )


def _fmt_slot(slot: dict) -> str:
    start = datetime.fromisoformat(str(slot["start_ts"]))
    end = datetime.fromisoformat(str(slot["end_ts"]))
    day = _DAYS_ES[start.weekday()]
    month = _MONTHS_ES[start.month]
    return f"{day} {start.day} de {month},  {start.strftime('%H:%M')} – {end.strftime('%H:%M')}"


def _slots_html(slots: list[dict]) -> str:
    if not slots:
        return "<em style='color:#999'>Sin slots activos</em>"
    items = "".join(f"<li style='margin:2px 0'>{_fmt_slot(s)}</li>" for s in slots)
    return f"<ul style='margin:4px 0;padding-left:1.2em'>{items}</ul>"


def _load_projects() -> list[tuple[str, int]]:
    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description FROM tfm_bot.projects ORDER BY id"
            )
            rows = cur.fetchall()
    result = []
    for row in rows:
        pid = row["id"]
        label = row.get("name") or row.get("description") or f"Proyecto {pid}"
        result.append((f"{label}  (id={pid})", pid))
    return result


def _load_existing_conversations() -> list[tuple[str, dict]]:
    """Return all conversations with basic contact info."""
    pool = get_postgres_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.id            AS conv_id,
                    c.state,
                    c.contact_id,
                    ct.name         AS contact_name,
                    ct.phone
                FROM tfm_bot.conversations c
                JOIN tfm_bot.contacts ct ON ct.id = c.contact_id
                ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC
                """
            )
            rows = cur.fetchall()
    result = []
    for row in rows:
        contact_label = (
            row.get("contact_name")
            or row.get("phone")
            or f"Contacto {row['contact_id']}"
        )
        state_icon = {
            STATE_AWAITING_REPLY: "⏳",
            STATE_COLLECTING_AVAILABILITY: "📝",
            STATE_CONFIRMING_AVAILABILITY: "✅",
            STATE_CLOSED: "🔒",
        }.get(row["state"], "❓")
        label = f"{state_icon} {contact_label}"
        value = {
            "conv_id": row["conv_id"],
            "contact_id": row["contact_id"],
            "phone": row["phone"],
            "state": row["state"],
        }
        result.append((label, value))
    return result


# ── Gradio handlers ───────────────────────────────────────────────


def _make_session() -> dict:
    return {"phone": None, "contact_id": None, "project_id": None, "started": False}


def _fresh_conv_update():
    return gr.update(choices=_load_existing_conversations())


def on_trigger(project_choice, session: dict):
    if project_choice is None:
        return (
            session,
            [],
            "<em>Selecciona un proyecto primero.</em>",
            "<em>Sin slots activos</em>",
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(),
        )

    engine = AvailabilityEngine()
    project_id = project_choice

    project = engine.contacts_repo.find_project_by_id(project_id)
    contact_id = project["contact_id"] if project else None
    contact = engine.contacts_repo.find_by_id(contact_id) if contact_id else None
    phone = contact["phone"] if contact else None

    session = _make_session()
    session.update(project_id=project_id, phone=phone, contact_id=contact_id)

    history = []

    existing = (
        engine.repo.get_conversation_for_contact(contact_id) if contact_id else None
    )
    if existing and existing["state"] != STATE_CLOSED:
        msgs = engine.repo.get_conversation_messages(int(existing["id"]))
        for m in msgs:
            role = "assistant" if m["direction"] == "outbound" else "user"
            history.append({"role": role, "content": m["content_raw"]})
        session["started"] = True
        state_html = _state_badge(existing["state"])
        slots = engine.repo.get_active_slots(int(existing["id"]))
        slots_html = _slots_html(slots)
    else:
        try:
            result = engine.trigger_conversation(project_id)
        except TriggerError as exc:
            return (
                session,
                [],
                f"<span style='color:red'>Error: {exc}</span>",
                "<em>Sin slots activos</em>",
                gr.update(interactive=False),
                gr.update(interactive=False),
                gr.update(),
            )
        history.append({"role": "assistant", "content": result.greeting})
        session["started"] = True
        state_html = _state_badge(STATE_AWAITING_REPLY)
        slots_html = _slots_html([])

    return (
        session,
        history,
        state_html,
        slots_html,
        gr.update(interactive=True),
        gr.update(interactive=True),
        _fresh_conv_update(),
    )


def on_load_conversation(conv_choice, session: dict):
    if conv_choice is None:
        return (
            session,
            [],
            "<em>Selecciona una conversación.</em>",
            "<em>Sin slots activos</em>",
            gr.update(interactive=False),
            gr.update(interactive=False),
        )

    engine = AvailabilityEngine()
    contact_id = conv_choice["contact_id"]
    conv_id = int(conv_choice["conv_id"])

    session = _make_session()
    session.update(
        phone=conv_choice["phone"],
        contact_id=contact_id,
        started=True,
    )

    msgs = engine.repo.get_conversation_messages(conv_id)
    history = []
    for m in msgs:
        role = "assistant" if m["direction"] == "outbound" else "user"
        history.append({"role": role, "content": m["content_raw"]})

    slots = engine.repo.get_active_slots(conv_id)
    return (
        session,
        history,
        _state_badge(conv_choice["state"]),
        _slots_html(slots),
        gr.update(interactive=True),
        gr.update(interactive=True),
    )


def on_send(message: str, history: list, session: dict):
    message = message.strip()
    if not message or not session.get("started"):
        yield history, "", "<em>—</em>", "<em>Sin slots activos</em>"
        return

    engine = AvailabilityEngine()
    phone = session["phone"]
    contact_id = session["contact_id"]

    history = list(history)
    history.append({"role": "user", "content": message})
    yield history, "", "<em>Procesando…</em>", "<em>—</em>"

    reply = engine.handle_message(phone, message)

    if reply:
        history.append({"role": "assistant", "content": reply})

    conv = engine.repo.get_conversation_for_contact(contact_id)
    state = conv["state"] if conv else STATE_CLOSED
    slots = engine.repo.get_active_slots(int(conv["id"])) if conv else []

    yield history, "", _state_badge(state), _slots_html(slots)


# ── UI layout ─────────────────────────────────────────────────────


def build_ui():
    projects = _load_projects()
    project_choices = [(label, pid) for label, pid in projects]
    existing_convs = _load_existing_conversations()
    existing_choices = [(label, val) for label, val in existing_convs]

    with gr.Blocks(title="Availability Bot Demo", theme=gr.themes.Soft()) as demo:
        session_state = gr.State(_make_session())

        gr.Markdown("## 🤖 Availability Bot — Simulador de conversación")

        with gr.Row():
            with gr.Column(scale=1, min_width=270):
                gr.Markdown("### Nueva conversación")
                project_dd = gr.Dropdown(
                    choices=project_choices,
                    label="Proyecto",
                    value=project_choices[0][1] if project_choices else None,
                )
                trigger_btn = gr.Button(
                    "Iniciar / Reanudar conversación", variant="primary"
                )

                gr.Markdown("---")
                gr.Markdown("### Conversaciones existentes")
                conv_dd = gr.Dropdown(
                    choices=existing_choices,
                    label="Cliente",
                    value=None,
                )
                with gr.Row():
                    load_btn = gr.Button(
                        "Cargar conversación", variant="secondary", scale=3
                    )
                    refresh_btn = gr.Button("↻", variant="secondary", scale=1)

                gr.Markdown("---")
                gr.Markdown("**Estado**")
                state_html = gr.HTML(
                    "<em style='color:#999'>Sin conversación activa</em>"
                )

                gr.Markdown("**Slots activos**")
                slots_html = gr.HTML("<em style='color:#999'>Sin slots activos</em>")

            with gr.Column(scale=3):
                chatbot = gr.Chatbot(
                    label="Conversación",
                    height=520,
                )
                with gr.Row():
                    msg_input = gr.Textbox(
                        placeholder="Escribe tu mensaje aquí…",
                        label="",
                        scale=5,
                        interactive=False,
                        autofocus=True,
                    )
                    send_btn = gr.Button(
                        "Enviar", variant="primary", scale=1, interactive=False
                    )

        trigger_outputs = [
            session_state,
            chatbot,
            state_html,
            slots_html,
            msg_input,
            send_btn,
            conv_dd,
        ]

        trigger_btn.click(
            on_trigger,
            inputs=[project_dd, session_state],
            outputs=trigger_outputs,
        )

        load_btn.click(
            on_load_conversation,
            inputs=[conv_dd, session_state],
            outputs=trigger_outputs[:-1],  # load doesn't need to refresh the list
        )

        refresh_btn.click(
            _fresh_conv_update,
            inputs=[],
            outputs=[conv_dd],
        )

        send_outputs = [chatbot, msg_input, state_html, slots_html]
        send_btn.click(
            on_send, inputs=[msg_input, chatbot, session_state], outputs=send_outputs
        )
        msg_input.submit(
            on_send, inputs=[msg_input, chatbot, session_state], outputs=send_outputs
        )

    return demo


if __name__ == "__main__":
    build_ui().launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
