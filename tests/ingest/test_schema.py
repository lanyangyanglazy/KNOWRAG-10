from ingest.db import migrate


def test_migrate_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    names = {row["tablename"] for row in rows}
    assert {
        "documents", "doc_versions", "chunks", "doc_tables", "jobs",
        "review_items", "review_approvals", "layout_templates",
    } <= names


def test_migrate_is_idempotent(conn):
    migrate(conn)
    migrate(conn)
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
