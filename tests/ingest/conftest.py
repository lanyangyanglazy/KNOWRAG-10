import os

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from ingest.db import migrate

TEST_DB_URL = os.getenv("INGEST_TEST_DB_URL", "")
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
