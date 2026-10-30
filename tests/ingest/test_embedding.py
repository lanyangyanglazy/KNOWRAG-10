from __future__ import annotations

import json

import pytest

from ingest import embedding, pipeline, repository as repo
from ingest.config import get_settings
from ingest.errors import ModelUnavailable
from ingest.ir import Block, Column, DocMeta, DocModel, ParserInfo, Table


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.001] * self.dim for _ in texts]


def _ir() -> DocModel:
    table = Table(
        table_key="t1", sheet="2025预算", range="A1:B3",
        header_rows=[1], header_values=[["部门", "预算"]],
        columns=[Column(key="c1", name="部门"), Column(key="c2", name="预算")],
        rows=[["综合处", "100"], ["人事处", "90"]],
        confidence=0.9, source="heuristic",
    )
    return DocModel(
        parser=ParserInfo(name="xlsx", version="0.1.0"),
        meta=DocMeta(title="预算", source_filename="预算.xlsx", sha256="b" * 64),
        blocks=[Block(block_id="b1", type="paragraph", text="2025 年预算如下。"),
                Block(block_id="b2", type="table", table=table)],
    )


def _seed(conn, *, classification: int = 1):
    doc = repo.create_document(conn, title="预算", dept="财务处",
                               classification=classification,
                               effective_date=None, created_by="wang")
    version = repo.create_version(
        conn, document_id=doc["id"], version_no=1, sha256="b" * 64,
        filename="预算.xlsx", mime="application/octet-stream", size_bytes=10,
        storage_path="bb/" + "b" * 64, parser="xlsx", parser_version="0.1.0")
    repo.set_version_status(conn, version["id"], "embedding", ir=_ir())
    conn.commit()
    return doc, version


def test_embed_settings_do_not_collide_with_faiss_embed_model(monkeypatch):
    monkeypatch.delenv("INGEST_EMBED_MODEL", raising=False)
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.embed_model == "bge-m3"
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    get_settings.cache_clear()


def test_embed_version_writes_chunks_and_tables(conn):
    doc, version = _seed(conn)
    result = embedding.embed_version(conn, version["id"], embedder=FakeEmbedder())

    rows = conn.execute(
        "SELECT * FROM chunks WHERE version_id=%s ORDER BY ordinal",
        (version["id"],),
    ).fetchall()
    assert result["chunks"] == len(rows) > 0
    assert [row["ordinal"] for row in rows] == list(range(len(rows)))
    assert all(row["embedding"] is not None for row in rows)
    assert all(row["embed_model"] == "fake-embed" for row in rows)
    assert all(row["active"] is False for row in rows)
    assert rows[0]["metadata"]["embed_model"] == "fake-embed"
    assert rows[0]["metadata"]["chunk_id"] == f"{doc['id']}@v1#0"
    tsv_ok = conn.execute(
        "SELECT tsv = to_tsvector('simple', content_tokens) AS ok "
        "FROM chunks WHERE id=%s", (rows[0]["id"],)).fetchone()["ok"]
    assert tsv_ok is True

    assert conn.execute("SELECT status FROM doc_versions WHERE id=%s",
                        (version["id"],)).fetchone()["status"] == "ready"
    job = conn.execute(
        "SELECT kind, payload FROM jobs WHERE kind='activate' AND status='queued'"
    ).fetchone()
    assert job["payload"]["version_id"] == str(version["id"])

    table = conn.execute("SELECT * FROM doc_tables WHERE version_id=%s",
                         (version["id"],)).fetchone()
    assert table["table_key"] == "t1"
    assert table["header_rows"] == [1]
    assert table["row_count"] == 2
    assert table["columns"][1]["name"] == "预算"


def test_embed_version_is_idempotent(conn):
    _, version = _seed(conn)
    embedder = FakeEmbedder()
    first = embedding.embed_version(conn, version["id"], embedder=embedder)
    second = embedding.embed_version(conn, version["id"], embedder=embedder)
    assert first["chunks"] == second["chunks"]
    assert conn.execute("SELECT count(*) AS n FROM chunks WHERE version_id=%s",
                        (version["id"],)).fetchone()["n"] == first["chunks"]
    queued = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind='activate' AND status='queued'"
    ).fetchone()["n"]
    assert queued == 1


def test_embed_keeps_active_for_current_version(conn):
    doc, version = _seed(conn)
    conn.execute("UPDATE documents SET status='active', current_version_id=%s "
                 "WHERE id=%s", (version["id"], doc["id"]))
    conn.commit()
    embedding.embed_version(conn, version["id"], embedder=FakeEmbedder())
    rows = conn.execute("SELECT active FROM chunks WHERE version_id=%s",
                        (version["id"],)).fetchall()
    assert rows and all(row["active"] is True for row in rows)


def test_dimension_mismatch_raises_and_keeps_version_embedding(conn):
    _, version = _seed(conn)
    with pytest.raises(ModelUnavailable):
        embedding.embed_version(conn, version["id"], embedder=FakeEmbedder(dim=8))
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM chunks WHERE version_id=%s",
                        (version["id"],)).fetchone()["n"] == 0
    assert conn.execute("SELECT status FROM doc_versions WHERE id=%s",
                        (version["id"],)).fetchone()["status"] == "embedding"


def test_embed_texts_batches_requests():
    embedder = FakeEmbedder(dim=2)
    vectors = embedding.embed_texts(embedder, ["a", "b", "c"], batch_size=2)
    assert len(vectors) == 3
    assert [len(call) for call in embedder.calls] == [2, 1]


def test_embed_texts_rejects_wrong_count():
    class ShortEmbedder(FakeEmbedder):
        def embed(self, texts):
            return [[0.0] * self.dim]

    with pytest.raises(ModelUnavailable):
        embedding.embed_texts(ShortEmbedder(dim=2), ["a", "b"])


def test_ollama_embedder_contract(monkeypatch):
    captured = {}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    embedder = embedding.OllamaEmbedder(model="bge-m3",
                                        base_url="http://127.0.0.1:11434", dim=2)
    vectors = embedder.embed(["甲", "乙"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"] == "http://127.0.0.1:11434/api/embed"
    assert captured["body"] == {"model": "bge-m3", "input": ["甲", "乙"]}


def test_ollama_embedder_rejects_bad_dimension(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"embeddings": [[0.1]]}).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: FakeResponse())
    embedder = embedding.OllamaEmbedder(model="bge-m3",
                                        base_url="http://127.0.0.1:11434", dim=2)
    with pytest.raises(ModelUnavailable):
        embedder.embed(["甲"])


def test_handle_embed_is_registered():
    assert pipeline.HANDLERS["embed"] is pipeline.handle_embed
