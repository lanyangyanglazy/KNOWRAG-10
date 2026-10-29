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


def main(argv: list[str] | None = None) -> int:
    """python -m ingest.db：部署第一步，幂等应用 schema。"""
    import argparse
    import sys

    import psycopg
    from psycopg.rows import dict_row

    parser = argparse.ArgumentParser(description="初始化/升级摄取库 schema（幂等）")
    parser.parse_args(argv)

    # Windows 控制台默认 GBK，中文成功提示会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    url = get_settings().db_url
    if not url:
        raise RuntimeError("INGEST_DB_URL 未配置")
    # 空库冷启动必须绕过连接池：池的 configure 会 register_vector，
    # 而 vector 类型要等本命令把 schema 建好后才存在，否则永远连不上。
    with psycopg.connect(url, row_factory=dict_row) as conn:
        migrate(conn)
    print("schema 已应用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
