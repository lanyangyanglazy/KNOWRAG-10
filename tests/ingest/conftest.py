import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from ingest.config import get_settings
from ingest.db import migrate, reset_pool_for_tests

TEST_DB_URL = os.getenv("INGEST_TEST_DB_URL", "")
PASSWORD = "test-pass-123"
TRUNCATE_SQL = (
    "TRUNCATE review_approvals, review_items, chunks, doc_tables, doc_versions, "
    "documents, jobs, layout_templates RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session")
def db_url():
    if not TEST_DB_URL:
        if os.getenv("INGEST_REQUIRE_DB"):
            pytest.fail("INGEST_REQUIRE_DB=1 但未设置 INGEST_TEST_DB_URL")
        pytest.skip("未设置 INGEST_TEST_DB_URL；先 docker compose up -d postgres")
    return TEST_DB_URL


@pytest.fixture(scope="session")
def migrated(db_url):
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        migrate(conn)
    return db_url


@pytest.fixture()
def conn(migrated):
    with psycopg.connect(migrated, row_factory=dict_row) as conn:
        register_vector(conn)
        conn.execute(TRUNCATE_SQL)
        conn.commit()
        yield conn
        conn.rollback()


@pytest.fixture()
def clean_db(migrated):
    """给 API 测试用：不依赖 conn fixture，也能保证每个用例从空库开始。"""
    with psycopg.connect(migrated, row_factory=dict_row) as conn:
        conn.execute(TRUNCATE_SQL)
        conn.commit()
    return migrated


@pytest.fixture()
def api(clean_db, tmp_path, monkeypatch):
    """返回 (make_client, db_url)；make_client(username) 登录并返回 TestClient。"""
    monkeypatch.setenv("INGEST_DB_URL", clean_db)
    monkeypatch.setenv("INGEST_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    reset_pool_for_tests()
    from app import server

    # 问答路径 audit_ready() 是 fail-closed：把审计文件指到 tmp，既不写脏仓库 logs/，
    # 也避免并发测试互相关掉对方的可写性判断。
    monkeypatch.setattr(server, "AUDIT_FILE", tmp_path / "audit.jsonl")

    def make(username: str = "wang") -> TestClient:
        client = TestClient(server.app)
        resp = client.post("/api/login",
                           json={"username": username, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        return client

    yield make, clean_db
    reset_pool_for_tests()
    get_settings.cache_clear()


def fetch_all(db_url: str, sql: str, params: tuple = ()) -> list[dict]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()
