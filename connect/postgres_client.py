from __future__ import annotations

import atexit
from functools import lru_cache
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import get_settings


@lru_cache(
    maxsize=1
)  # Crea un connection pool por worker, IMPORTANTE: cerrarlo en shutdown con get_postgres_pool().close()
def get_postgres_pool() -> ConnectionPool[Any]:
    s = get_settings()
    dsn = (
        f"host={s.POSTGRES_HOST} "
        f"port={s.POSTGRES_PORT} "
        f"dbname={s.POSTGRES_DB} "
        f"user={s.POSTGRES_USER} "
        f"password={s.POSTGRES_PASSWORD} "
        f"sslmode={s.POSTGRES_SSLMODE}"
    )
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
    )
    atexit.register(pool.close)
    return pool
