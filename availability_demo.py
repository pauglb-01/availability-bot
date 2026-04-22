"""
Interactive terminal demo for the availability chatbot.

Uses the REAL AvailabilityEngine, real DB, and real LLM.
WhatsApp is replaced by terminal stdin/stdout.

Requires:
  - .env with POSTGRES_* and OPENAI_API_KEY
  - A project with a linked contact in tfm_bot.projects / tfm_bot.contacts

Usage:
    # Trigger a new conversation for a project:
    python scripts/availability_demo.py --project-id 1

    # Resume an existing open conversation:
    python scripts/availability_demo.py --project-id 1 --resume
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from availability.availability_share.constants import (
    STATE_AWAITING_REPLY,
    STATE_CLOSED,
    STATE_COLLECTING_AVAILABILITY,
    STATE_CONFIRMING_AVAILABILITY,
)
from availability.availability_share.engine import AvailabilityEngine, TriggerError

# ── Project root on sys.path ────────────────────────────────────
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── Colours ──────────────────────────────────────────────────────

_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _state_colour(state: str) -> str:
    return {
        STATE_AWAITING_REPLY: _YELLOW,
        STATE_COLLECTING_AVAILABILITY: _CYAN,
        STATE_CONFIRMING_AVAILABILITY: _GREEN,
        STATE_CLOSED: _DIM,
    }.get(state, "")


def _print_slots(engine: AvailabilityEngine, conversation_id: int) -> None:
    """Show current slots from the last LLM response that had them."""
    import json

    messages = engine.repo.get_conversation_messages(conversation_id)
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
            print(f"\n{_DIM}Slots actuales:{_RESET}")
            for s in slots:
                print(f"  {_DIM}• {s['start_ts']}  →  {s['end_ts']}{_RESET}")
            return


def _print_state(state: str) -> None:
    colour = _state_colour(state)
    print(f"{_DIM}[estado: {colour}{state}{_RESET}{_DIM}]{_RESET}")


# ── Main loop ────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Terminal demo: availability bot with real DB + LLM, no WhatsApp.",
    )
    parser.add_argument(
        "--project-id",
        type=int,
        required=True,
        help="Project ID (must exist in tfm_bot.projects)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume existing open conversation instead of triggering a new one",
    )
    args = parser.parse_args()

    engine = AvailabilityEngine()

    # Resolve contact from project (for resume and for the message loop)
    project = engine.contacts_repo.find_project_by_id(args.project_id)
    if project is None:
        print(f"ERROR: No se encontró el proyecto {args.project_id}.")
        return 1

    contact_id = project.get("contact_id")
    if not contact_id:
        print(f"ERROR: El proyecto {args.project_id} no tiene contacto asociado.")
        return 1

    contact = engine.contacts_repo.find_by_id(contact_id)
    if contact is None:
        print(f"ERROR: Contacto {contact_id} no encontrado.")
        return 1

    phone = contact.get("phone", "")
    contact_name = contact.get("name", phone)
    project_name = project.get("name") or project.get("description") or ""
    print(f"\n{_BOLD}Proyecto:{_RESET} {project_name} (id={args.project_id})")
    print(f"{_BOLD}Contacto:{_RESET} {contact_name} (id={contact_id}, phone={phone})")

    if args.resume:
        existing = engine.repo.get_conversation_for_contact(contact_id)
        if existing is None or existing["state"] == STATE_CLOSED:
            print(
                "ERROR: No hay conversación abierta. Quita --resume para crear una nueva."
            )
            return 1
        conversation_id = int(existing["id"])
        print(
            f"{_BOLD}Reanudando conversación:{_RESET} id={conversation_id}, estado={existing['state']}\n"
        )

        # Show conversation history
        history = engine.repo.get_conversation_messages(conversation_id)
        if history:
            print(f"{_DIM}── Historial ──{_RESET}")
            for m in history:
                role = f"{_CYAN}Tú" if m["direction"] == "inbound" else f"{_GREEN}Bot"
                print(f"  {role}:{_RESET} {m['content_raw']}")
            print(f"{_DIM}── Fin historial ──{_RESET}\n")

        _print_slots(engine, conversation_id)
    else:
        # Same call the router makes — all business logic is in the engine
        try:
            result = engine.trigger_conversation(args.project_id)
        except TriggerError as exc:
            print(f"ERROR: {exc}")
            return 1

        conversation_id = result.conversation_id
        print(f"\n{_GREEN}Bot:{_RESET} {result.greeting}")
        print(f"{_DIM}Conversación id={conversation_id}{_RESET}\n")

    # Interactive loop — each input goes through engine.handle_message()
    while True:
        conv = engine.repo.get_conversation_for_contact(contact_id)
        state = conv["state"] if conv else STATE_CLOSED

        _print_state(state)

        if state == STATE_CLOSED:
            print(f"{_DIM}Conversación cerrada. Ctrl+C para salir.{_RESET}")

        try:
            user_input = input(f"{_CYAN}Tú:{_RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nAdiós.")
            return 0

        if not user_input:
            continue

        reply = engine.handle_message(phone, user_input)

        if reply:
            print(f"\n{_GREEN}Bot:{_RESET} {reply}")
        else:
            print(f"\n{_DIM}(sin respuesta del bot){_RESET}")

        conv = engine.repo.get_conversation_for_contact(contact_id)
        if conv:
            _print_slots(engine, int(conv["id"]))
        print()


if __name__ == "__main__":
    raise SystemExit(main())
