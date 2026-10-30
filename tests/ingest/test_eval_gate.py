from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import replace

import pytest
from psycopg.types.json import Jsonb

from evaluation import compare_backends, runner
from evaluation.cases import EvalCase
from evaluation.pg_corpus import load_active_chunks, validate_against_pg
from ingest import import_gov_docs as importer
from ingest import pipeline
from ingest import repository as repo
from ingest.config import get_settings
from ingest.embedding import embed_version

DIM = 1024


class FakeEmbedder:
    model = "bge-m3"
    dim = DIM

    def embed(self, texts):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


def _seed(conn, *, doc_status="active", chunk_active=True,
          content="差旅住宿费标准为每人每天 900 元", title="差旅费管理办法"):
    doc = repo.create_document(
        conn, title=title, dept="综合处", classification=1,
        effective_date="2026-01-01", created_by="zhang")
    version = repo.create_version(
        conn, document_id=doc["id"], version_no=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        filename="差旅.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=10, storage_path=f"aa/{uuid.uuid4().hex}",
        parser="docx", parser_version="0.1.0", status="ready")
    conn.execute(
        "UPDATE documents SET status=%s, current_version_id=%s WHERE id=%s",
        (doc_status, version["id"], doc["id"]))
    conn.execute(
        """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type, content,
               content_tokens, embed_model, dept, classification, metadata,
               chunk_hash, active)
           VALUES (%s,%s,0,'text',%s,%s,'bge-m3','综合处',1,%s,%s,%s)""",
        (doc["id"], version["id"], content, content,
         Jsonb({"chunk_id": f"{doc['id']}@v1#0", "source": "差旅.docx"}),
         uuid.uuid4().hex + uuid.uuid4().hex, chunk_active))
    conn.commit()
    return doc, version


def _case(**over):
    values = {"id": "c1", "user": "zhang", "question": "住宿费标准？",
              "expected_doc_ids": [], "evidence": []}
    values.update(over)
    return EvalCase(**values)


def test_load_active_chunks_only_returns_searchable(conn):
    doc, _ = _seed(conn)
    _seed(conn, doc_status="draft")      # 未激活文档
    _seed(conn, chunk_active=False)      # 已下线切片

    chunks = load_active_chunks(conn)

    assert len(chunks) == 1
    assert chunks[0].metadata["doc_id"] == str(doc["id"])
    assert "900 元" in chunks[0].page_content


def test_validate_against_pg_accepts_matching_evidence(conn):
    doc, _ = _seed(conn)
    case = _case(expected_doc_ids=[str(doc["id"])], evidence=["每人每天 900 元"])
    assert validate_against_pg([case], conn) == []


def test_validate_against_pg_reports_unknown_document(conn):
    _seed(conn)
    case = _case(expected_doc_ids=["00000000-0000-0000-0000-000000000000"],
                 evidence=["不存在的证据"])
    problems = validate_against_pg([case], conn)
    assert any("不在语料中" in problem for problem in problems)


def test_validate_against_pg_explains_empty_database(conn):
    problems = validate_against_pg([_case(expected_doc_ids=["x"], evidence=["y"])], conn)
    assert len(problems) == 1
    assert "没有任何已激活切片" in problems[0]


def _report(**over):
    metrics = {
        "cases": 33, "answerable": 24, "refusal_expected": 9,
        "evidence_recall": 1.0, "doc_recall": 1.0, "mrr": 1.0,
        "citation_recall": None, "refusal_accuracy": 1.0,
        "false_refusal_rate": 0.0, "missed_refusal_rate": 0.0,
        "leaks": 0,
    }
    metrics.update(over)
    return {"metrics": metrics}


def test_gate_blocks_drop_beyond_tolerance():
    verdict = compare_backends.judge(_report(evidence_recall=0.96),
                                     _report(evidence_recall=1.0))
    assert verdict["ok"] is False
    assert [item["metric"] for item in verdict["regressions"]] == ["evidence_recall"]


def test_gate_allows_drop_within_tolerance():
    verdict = compare_backends.judge(_report(evidence_recall=0.97),
                                     _report(evidence_recall=1.0))
    assert verdict["ok"] is True


def test_gate_treats_any_leak_as_failure_even_with_flat_metrics():
    verdict = compare_backends.judge(_report(leaks=1), _report(leaks=1))
    assert verdict["ok"] is False
    assert verdict["reasons"]


def test_gate_blocks_shrinking_case_set():
    verdict = compare_backends.judge(_report(cases=30), _report(cases=33))
    assert verdict["ok"] is False
    assert [item["metric"] for item in verdict["regressions"]] == ["cases"]


def test_incomparable_embed_model_exits_with_code_2(monkeypatch, capsys):
    """不可比必须以 2 退出：1 已被「有回归」占用，混用会让运维误读结论。"""
    import main as core
    import ingest.config as ingest_config

    class _Settings:
        embed_model = "nomic-embed-text"

    monkeypatch.setattr(core, "EMBED_MODEL", "bge-m3")
    monkeypatch.setattr(ingest_config, "get_settings", lambda: _Settings())

    with pytest.raises(SystemExit) as excinfo:
        compare_backends._assert_comparable()
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "不可比" in captured.err
    assert not captured.out  # 诊断信息走 stderr，不能污染机器可读输出

    class _Same:
        embed_model = "bge-m3"

    # 阳性对照：模型一致时不得退出，否则门禁自己也跑不起来了。
    monkeypatch.setattr(ingest_config, "get_settings", lambda: _Same())
    compare_backends._assert_comparable()


def test_validate_gold_reads_pg_corpus(monkeypatch, conn):
    calls: list[str] = []

    monkeypatch.setattr(runner, "validate_against_corpus",
                        lambda cases, documents: calls.append("faiss") or [])
    import evaluation.pg_corpus as pg_corpus

    monkeypatch.setattr(pg_corpus, "validate_against_pg",
                        lambda cases, connection: calls.append("pg") or [])
    from ingest import db

    monkeypatch.setattr(db, "connection", lambda: nullcontext(conn))

    class PgService:
        backend = "pg"

    assert runner._validate_gold([], PgService()) == []
    assert calls == ["pg"]


def test_imported_gov_doc_keeps_gold_id_and_uuid_chunk_id(conn, tmp_path, monkeypatch):
    """导入文档以 origin_id 同时保住金标（gov-xxx）与 UUID 切片定位契约。"""
    monkeypatch.setattr(importer, "write_audit", lambda record: None)
    import ingest.activation as activation

    monkeypatch.setattr(activation, "write_audit", lambda record: None)

    path = tmp_path / "测试.md"
    path.write_text(
        "---\n"
        "doc_id: gov-999\n"
        "title: 测试差旅费办法\n"
        "dept: 综合处\n"
        "classification: 内部\n"
        "effective_date: 2026-01-01\n"
        "---\n\n"
        "# 差旅费管理办法\n\n"
        "差旅住宿费标准为每人每天 900 元，凭正规发票据实报销。\n",
        encoding="utf-8",
    )
    settings = replace(get_settings(), upload_dir=tmp_path / "uploads")

    result = importer.import_file(conn, path, settings=settings)
    embed_version(conn, result["version_id"], embedder=FakeEmbedder())
    # embed_version 写入的切片 active=false（激活任务负责上线）；这里显式跑一次
    # activate job（等价于 worker --once），不依赖后台进程。
    pipeline.HANDLERS["activate"](conn, {"version_id": result["version_id"]})
    conn.execute(
        "UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
        (result["version_id"], result["document_id"]))
    conn.commit()

    doc = conn.execute(
        "SELECT * FROM documents WHERE id=%s", (result["document_id"],)
    ).fetchone()
    assert doc["origin_id"] == "gov-999"

    chunks = load_active_chunks(conn)
    target = next(
        chunk for chunk in chunks
        if chunk.metadata["chunk_id"].startswith(f"{result['document_id']}@v1#"))
    assert target.metadata["doc_id"] == "gov-999"

    case = EvalCase(id="c999", user="zhang", question="差旅住宿费标准是多少？",
                    expected_doc_ids=["gov-999"], evidence=["每人每天 900 元"])
    assert validate_against_pg([case], conn) == []
