from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ingest.config import get_settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_pool: ConnectionPool | None = None


def _configure(conn: Connection) -> None:
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = get_settings().db_url
        if not url:
            raise RuntimeError("INGEST_DB_URL 未配置")
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=10,
            open=True,  # 显式打开：psycopg_pool 未来版本将把默认值改为 False
            kwargs={"row_factory": dict_row},
            configure=_configure,
        )
    return _pool


def reset_pool_for_tests() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
    _pool = None


@contextmanager
def connection() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        yield conn


def migrate(conn: Connection) -> None:
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()
