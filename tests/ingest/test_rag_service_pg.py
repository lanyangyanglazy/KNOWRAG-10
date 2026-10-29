from __future__ import annotations

import contextlib
import uuid

import pytest
from langchain_community.vectorstores import FAISS
from langchain_core.messages import AIMessage
from pgvector import Vector
from psycopg.types.json import Jsonb

from app import rag_service as rs
from app.users import User, get_user
from ingest import repository as repo
from ingest.chunking import tokenize

DIM = 1024


class FakeLLM:
    def __init__(self):
        self.stream_called = False

    def invoke(self, messages):
        return AIMessage(content="")

    def stream(self, messages):
        self.stream_called = True
        yield AIMessage(content="这是答案")


class FakeEmbedder:
    model = "bge-m3"
    dim = DIM

    def __init__(self, index=0):
        self.vector = [0.0] * DIM
        self.vector[index] = 1.0
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return [list(self.vector) for _ in texts]


class FakeEmbeddings:
    """LangChain Embeddings 替身，只用于验证 faiss 分支仍能构建。"""

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


@pytest.fixture()
def service(conn):
    return rs.RagService(
        llm=FakeLLM(), backend="pg", embedder=FakeEmbedder(),
        conn_factory=lambda: contextlib.nullcontext(conn),
    )


def _seed(conn, *, dept="综合处", classification=1, title="预算说明",
          content="2025 年预算为 100 万元"):
    doc = repo.create_document(
        conn, title=title, dept=dept, classification=classification,
        effective_date="2026-01-01", created_by="zhang")
    version = repo.create_version(
        conn, document_id=doc["id"], version_no=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        filename="预算.xlsx", mime="application/vnd.ms-excel", size_bytes=10,
        storage_path=f"aa/{uuid.uuid4().hex}", parser="xlsx",
        parser_version="0.1.0", status="ready")
    conn.execute(
        "UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
        (version["id"], doc["id"]))
    tokens = " ".join(tokenize(content))
    metadata = {
        "chunk_id": f"{doc['id']}@v1#0", "chunk_type": "text",
        "title": title, "dept": dept, "classification": classification,
        "source": "预算.xlsx",
    }
    conn.execute(
        """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type,
               content, content_tokens, tsv, embedding, embed_model, dept,
               classification, metadata, chunk_hash, active)
           VALUES (%s,%s,0,'text',%s,%s,to_tsvector('simple', %s),%s,'bge-m3',
                   %s,%s,%s,%s,true)""",
        (doc["id"], version["id"], content, tokens, tokens,
         Vector([1.0] + [0.0] * (DIM - 1)), dept, classification,
         Jsonb(metadata), uuid.uuid4().hex + uuid.uuid4().hex))
    conn.commit()
    return repo.get_document(conn, doc["id"]), version


def test_resolve_backend_precedence(monkeypatch):
    monkeypatch.delenv("RETRIEVAL_BACKEND", raising=False)
    monkeypatch.delenv("INGEST_DB_URL", raising=False)
    assert rs.resolve_backend() == "faiss"

    # 配了数据库但没写 RETRIEVAL_BACKEND：默认走 pg，避免「数据入 pg、查询却查 FAISS」
    monkeypatch.setenv("INGEST_DB_URL", "postgresql://rag:rag@127.0.0.1:5433/rag")
    assert rs.resolve_backend() == "pg"

    monkeypatch.setenv("RETRIEVAL_BACKEND", " FAISS ")
    assert rs.resolve_backend() == "faiss"
    assert rs.resolve_backend("pg") == "pg"

    with pytest.raises(ValueError, match="RETRIEVAL_BACKEND"):
        rs.resolve_backend("chroma")


def test_pg_mode_does_not_bootstrap_faiss(monkeypatch, conn):
    def boom(*args, **kwargs):
        raise AssertionError("pg 后端不应加载语料、FAISS 或 LangChain embedding")

    monkeypatch.setattr(rs, "load_gov_documents", boom)
    monkeypatch.setattr(rs, "build_or_load_index", boom)
    monkeypatch.setattr(rs.core, "init_embedding", boom)

    service = rs.RagService(
        llm=FakeLLM(), backend="pg", embedder=FakeEmbedder(),
        conn_factory=lambda: contextlib.nullcontext(conn))

    assert service.backend == "pg"
    assert service.documents == []
    assert service.vectorstore is None
    assert service.embed_model is None


def test_pg_retrieve_maps_citation_metadata(service, conn, monkeypatch):
    monkeypatch.setattr(rs, "max_distance", lambda: 999.0)
    visible, _ = _seed(conn)
    hidden, _ = _seed(conn, dept="人事处", title="考勤细则")

    result = service.retrieve(get_user("zhang"), "2025 年预算")

    assert result.grounded is True
    assert result.visible_docs == 1
    assert result.visible_chunks == 1
    assert result.docs
    metadata = result.docs[0].metadata
    assert metadata["doc_id"] == str(visible["id"])
    assert metadata["classification"] == "内部"
    assert metadata["classification_level"] == 1
    assert metadata["effective_date"] == "2026-01-01"
    assert metadata["source"] == "预算.xlsx"
    assert metadata["chunk_id"].startswith(str(visible["id"]))
    assert metadata["expanded"] is False
    assert all(d.metadata["doc_id"] != str(hidden["id"]) for d in result.docs)


def test_pg_refuses_without_embedding_when_nothing_visible(service, conn):
    _seed(conn)
    outsider = User("outsider", "外部人员", "外部单位", max_level=-1, role="访客")

    result = service.retrieve(outsider, "预算")

    assert result.reason == "no_visible_docs"
    assert result.grounded is False
    assert result.docs == []
    assert service.embedder.calls == 0


def test_pg_visible_docs_and_stats(service, conn):
    public, _ = _seed(conn, dept="信息中心", classification=0, title="公开指南")
    internal, _ = _seed(conn, title="预算说明")
    _seed(conn, dept="人事处", title="考勤细则")

    visible = service.visible_docs(get_user("zhang"))

    assert {item["doc_id"] for item in visible} == {str(public["id"]), str(internal["id"])}
    assert {item["classification"] for item in visible} == {"公开", "内部"}

    stats = service.stats()
    assert stats["backend"] == "pg"
    assert stats["docs"] == 3
    assert stats["chunks"] == 3
    assert stats["chunks_by_classification"] == {"公开": 1, "内部": 2}
    assert stats["load_issues"] == []
    assert stats["max_distance"] == rs.max_distance()


def test_pg_visible_documents_is_explicitly_unsupported(service):
    with pytest.raises(RuntimeError, match="visible_documents"):
        service.visible_documents(get_user("zhang"))


def test_null_classification_is_never_retrievable(service, conn):
    _seed(conn, classification=None, title="未定级")
    user = get_user("zhang")

    assert service.visible_docs(user) == []

    result = service.retrieve(user, "预算")

    assert result.visible_chunks == 0
    assert result.visible_docs == 0
    assert result.reason == "no_visible_docs"
    assert result.docs == []
    assert service.embedder.calls == 0


def test_faiss_mode_reports_backend(monkeypatch):
    monkeypatch.setattr(
        rs, "build_or_load_index",
        lambda documents, embed_model: FAISS.from_documents(documents, embed_model))

    service = rs.RagService(llm=FakeLLM(), embed_model=FakeEmbeddings(),
                            backend="faiss")

    assert service.backend == "faiss"
    assert service.stats()["backend"] == "faiss"
    assert service.stats()["docs"] == 4
