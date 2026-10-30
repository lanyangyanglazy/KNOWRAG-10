from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ingest import import_gov_docs as importer
from ingest.config import get_settings

CORPUS = Path(__file__).resolve().parents[2] / "data" / "gov_docs"


def _settings(tmp_path):
    return replace(get_settings(), upload_dir=tmp_path / "uploads")


def _write(path: Path, *, classification="内部", doc_id="doc-1",
           effective_date="2026-05-01"):
    path.write_text(
        "---\n"
        f"doc_id: {doc_id}\n"
        "title: 测试文档\n"
        "dept: 综合处\n"
        f"classification: {classification}\n"
        f"effective_date: {effective_date}\n"
        "---\n\n"
        "# 标题\n\n正文内容\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def audited(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(importer, "write_audit", records.append)
    return records


def test_import_file_parses_stores_and_enqueues(conn, tmp_path, audited):
    path = _write(tmp_path / "测试.md")
    settings = _settings(tmp_path)

    result = importer.import_file(conn, path, settings=settings)

    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert str(doc["id"]) == result["document_id"]
    assert doc["status"] == "draft"
    assert doc["classification"] == 1
    assert str(doc["effective_date"]) == "2026-05-01"
    assert doc["created_by"] == "import"

    version = conn.execute("SELECT * FROM doc_versions").fetchone()
    assert version["status"] == "embedding"
    assert version["parser"] == "markdown"
    assert version["ir"]["blocks"]
    assert version["sha256"] == result["sha256"]

    job = conn.execute("SELECT * FROM jobs WHERE kind='embed'").fetchone()
    assert job["payload"]["version_id"] == result["version_id"]
    assert job["priority"] == 50

    stored = settings.upload_dir / result["sha256"][:2] / result["sha256"]
    assert stored.read_bytes() == path.read_bytes()
    assert audited[-1]["action"] == "import"


def test_import_is_idempotent_by_content(conn, tmp_path, audited):
    path = _write(tmp_path / "测试.md")
    settings = _settings(tmp_path)

    first = importer.import_file(conn, path, settings=settings)
    second = importer.import_file(conn, path, settings=settings)

    assert first["skipped"] is False
    assert second["skipped"] is True
    assert second["document_id"] == first["document_id"]

    counts = conn.execute(
        """SELECT (SELECT count(*) FROM documents) AS docs,
                  (SELECT count(*) FROM doc_versions) AS versions,
                  (SELECT count(*) FROM jobs) AS jobs"""
    ).fetchone()
    assert dict(counts) == {"docs": 1, "versions": 1, "jobs": 1}


def test_invalid_classification_creates_nothing(conn, tmp_path, audited):
    path = _write(tmp_path / "坏.md", classification="绝密")
    settings = _settings(tmp_path)

    with pytest.raises(ValueError, match="classification"):
        importer.import_file(conn, path, settings=settings)

    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
    assert not settings.upload_dir.exists() or not any(settings.upload_dir.rglob("*"))
    assert audited == []


def test_imports_existing_corpus(conn, tmp_path, audited):
    results = importer.import_dir(conn, CORPUS, settings=_settings(tmp_path))

    assert len(results) == 4
    assert all(r["skipped"] is False and r["job_id"] for r in results)
    assert {r["doc_id"] for r in results} == {
        "gov-001", "gov-002", "gov-003", "gov-004"}
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 4
    assert conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind='embed'").fetchone()["n"] == 4

    classes = conn.execute(
        """SELECT classification, count(*) AS n FROM documents
           GROUP BY classification ORDER BY classification"""
    ).fetchall()
    assert [(row["classification"], row["n"]) for row in classes] == [
        (0, 1), (1, 2), (2, 1)]


def test_import_dir_reports_bad_file_and_continues(conn, tmp_path, audited):
    _write(tmp_path / "good.md", doc_id="ok-1")
    _write(tmp_path / "bad.md", doc_id="bad-1", classification="绝密")

    results = importer.import_dir(conn, tmp_path, settings=_settings(tmp_path))

    by_file = {item["file"]: item for item in results}
    assert by_file["good.md"]["error"] is None
    assert "classification" in by_file["bad.md"]["error"]
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 1


def test_audit_failure_does_not_flip_import_result(conn, tmp_path, monkeypatch):
    def boom(record):
        raise OSError("disk full")

    monkeypatch.setattr(importer, "write_audit", boom)
    path = _write(tmp_path / "测试.md")

    result = importer.import_file(conn, path, settings=_settings(tmp_path))

    assert result["skipped"] is False
    assert result["error"] is None
    assert conn.execute(
        "SELECT count(*) AS n FROM documents").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind='embed'").fetchone()["n"] == 1
