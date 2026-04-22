from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection

from availability.availability_share.constants import (
    DEFAULT_MAX_CLARIFICATIONS,
    STATE_AWAITING_REPLY,
    STATE_CLOSED,
)
from connect.postgres_client import get_postgres_pool

logger = logging.getLogger(__name__)


class AvailabilityRepo:
    """CRUD for tfm_bot.conversations, messages, and contact_availabilities."""

    def __init__(self) -> None:
        self.pool = get_postgres_pool()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    # ── Conversations ────────────────────────────────────────────

    def get_open_conversation(
        self,
        contact_id: int,
        *,
        conn: Connection | None = None,
    ) -> dict | None:
        """Return the single non-CLOSED conversation for a contact, or None."""
        query = """
            SELECT * FROM tfm_bot.conversations
            WHERE contact_id = %s AND state != %s
            ORDER BY created_at DESC
            LIMIT 1
        """
        return self._fetch_one(query, (contact_id, STATE_CLOSED), conn=conn)

    def get_conversation_for_contact(
        self,
        contact_id: int,
        *,
        conn: Connection | None = None,
    ) -> dict | None:
        """Return the unique conversation row for a contact (any state), or None."""
        query = """
            SELECT * FROM tfm_bot.conversations
            WHERE contact_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """
        return self._fetch_one(query, (contact_id,), conn=conn)

    def reopen_conversation(
        self,
        conversation_id: int,
        *,
        conn: Connection,
    ) -> dict:
        """Reset a CLOSED conversation to AWAITING_REPLY, clearing counters."""
        query = """
            UPDATE tfm_bot.conversations
            SET state = %s, clarification_count = 0,
                closed_at = NULL, updated_at = now()
            WHERE id = %s
            RETURNING *
        """
        with conn.cursor() as cur:
            cur.execute(query, (STATE_AWAITING_REPLY, conversation_id))
            return cur.fetchone()

    def get_conversation_by_id(
        self,
        conversation_id: int,
        *,
        conn: Connection | None = None,
    ) -> dict | None:
        query = "SELECT * FROM tfm_bot.conversations WHERE id = %s"
        return self._fetch_one(query, (conversation_id,), conn=conn)

    def create_conversation(
        self,
        *,
        contact_id: int,
        max_clarifications: int = DEFAULT_MAX_CLARIFICATIONS,
        conn: Connection,
    ) -> dict:
        """Create a new conversation in AWAITING_REPLY state. Returns the row."""
        query = """
            INSERT INTO tfm_bot.conversations
                (contact_id, state, clarification_count, max_clarifications, created_at)
            VALUES (%s, %s, 0, %s, now())
            RETURNING *
        """
        with conn.cursor() as cur:
            cur.execute(query, (contact_id, STATE_AWAITING_REPLY, max_clarifications))
            return cur.fetchone()

    def update_conversation_state(
        self,
        conversation_id: int,
        *,
        state: str,
        conn: Connection,
        increment_clarification: bool = False,
        close: bool = False,
    ) -> dict:
        """Update conversation state and optional counters."""
        parts = ["state = %s", "updated_at = now()"]
        params: list = [state]

        if increment_clarification:
            parts.append("clarification_count = clarification_count + 1")

        if close:
            parts.append("closed_at = now()")

        parts.append("last_message_at = now()")
        set_clause = ", ".join(parts)
        query = (
            f"UPDATE tfm_bot.conversations SET {set_clause} WHERE id = %s RETURNING *"
        )
        params.append(conversation_id)

        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            return cur.fetchone()

    # ── Messages ─────────────────────────────────────────────────

    def insert_message(
        self,
        *,
        conversation_id: int,
        contact_id: int | None,
        direction: str,
        content_raw: str,
        intent: str | None = None,
        llm_raw_response: dict | None = None,
        conn: Connection,
    ) -> dict:
        query = """
            INSERT INTO tfm_bot.messages
                (conversation_id, contact_id, direction, content_raw, intent, llm_raw_response, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            RETURNING *
        """
        llm_json = (
            json.dumps(llm_raw_response, ensure_ascii=False)
            if llm_raw_response
            else None
        )
        with conn.cursor() as cur:
            cur.execute(
                query,
                (conversation_id, contact_id, direction, content_raw, intent, llm_json),
            )
            return cur.fetchone()

    def get_conversation_messages(
        self,
        conversation_id: int,
        *,
        conn: Connection | None = None,
    ) -> list[dict]:
        """Return all messages for a conversation, oldest first."""
        query = """
            SELECT * FROM tfm_bot.messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
        """
        return self._fetch_all(query, (conversation_id,), conn=conn)

    # ── Contact Availabilities ───────────────────────────────────

    def insert_slots(
        self,
        *,
        contact_id: int,
        conversation_id: int,
        source_message_id: int,
        slots: list[dict],
        project_id: int | None = None,
        conn: Connection,
    ) -> list[dict]:
        """Insert resolved availability slots. Each slot dict has start_ts and end_ts."""
        if not slots:
            return []
        query = """
            INSERT INTO tfm_bot.contact_availabilities
                (contact_id, project_id, conversation_id, source_message_id,
                 start_ts, end_ts, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', now())
            RETURNING *
        """
        inserted = []
        with conn.cursor() as cur:
            for slot in slots:
                cur.execute(
                    query,
                    (
                        contact_id,
                        project_id,
                        conversation_id,
                        source_message_id,
                        slot["start_ts"],
                        slot["end_ts"],
                    ),
                )
                inserted.append(cur.fetchone())
        return inserted

    def cancel_active_slots(
        self,
        conversation_id: int,
        *,
        conn: Connection,
    ) -> int:
        """Cancel all active slots for a conversation (e.g. on reschedule). Returns count."""
        query = """
            UPDATE tfm_bot.contact_availabilities
            SET status = 'cancelled'
            WHERE conversation_id = %s AND status = 'active'
        """
        with conn.cursor() as cur:
            cur.execute(query, (conversation_id,))
            return cur.rowcount

    def get_active_slots(
        self,
        conversation_id: int,
        *,
        conn: Connection | None = None,
    ) -> list[dict]:
        query = """
            SELECT * FROM tfm_bot.contact_availabilities
            WHERE conversation_id = %s AND status = 'active'
            ORDER BY start_ts ASC
        """
        return self._fetch_all(query, (conversation_id,), conn=conn)

    # ── helpers ───────────────────────────────────────────────────

    def _fetch_one(
        self, query: str, params: tuple, *, conn: Connection | None = None
    ) -> dict | None:
        if conn is None:
            with self.pool.connection() as local_conn:
                with local_conn.cursor() as cur:
                    cur.execute(query, params)
                    return cur.fetchone()
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()

    def _fetch_all(
        self, query: str, params: tuple, *, conn: Connection | None = None
    ) -> list[dict]:
        if conn is None:
            with self.pool.connection() as local_conn:
                with local_conn.cursor() as cur:
                    cur.execute(query, params)
                    return cur.fetchall()
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
