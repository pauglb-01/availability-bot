from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection

from connect.postgres_client import get_postgres_pool

logger = logging.getLogger(__name__)


class ContactsRepo:
    """Queries against tfm_bot.contacts."""

    def __init__(self) -> None:
        self.pool = get_postgres_pool()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    def find_by_phone(
        self, phone: str, *, conn: Connection | None = None
    ) -> dict | None:
        """Return the contact row for the given phone, or None."""
        query = "SELECT * FROM tfm_bot.contacts WHERE phone = %s AND deleted_at IS NULL"
        return self._fetch_one(query, (phone,), conn=conn)

    def find_by_id(
        self, contact_id: int, *, conn: Connection | None = None
    ) -> dict | None:
        query = "SELECT * FROM tfm_bot.contacts WHERE id = %s AND deleted_at IS NULL"
        return self._fetch_one(query, (contact_id,), conn=conn)

    def find_project_by_id(
        self, project_id: int, *, conn: Connection | None = None
    ) -> dict | None:
        query = "SELECT * FROM tfm_bot.projects WHERE id = %s"
        return self._fetch_one(query, (project_id,), conn=conn)

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
