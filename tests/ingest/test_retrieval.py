import re
import uuid
from dataclasses import replace

import pytest
from pgvector import Vector
from psycopg.types.json import Jsonb

from app.users import User
from ingest import repository as repo, retrieval
from ingest.chunking import tokenize
from ingest.config import get_settings

DIM = 1024


def _vec(index=0, weight=1.0):
    values = [0.0] * DIM
    values[index % DIM] = float(weight)
    return values


class FakeEmbedder:
    model = "bge-m3"
    dim = DIM

    def __init__(self, vector):
        self.vector = vector
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return [list(self.vector) for _ in texts]


class BoomEmbedder:
    model = "bge-m3"
    dim = DIM

    def embed(self, texts):
        raise AssertionError("不可见时不应调用嵌入服务")


def _user(**over):
    values = {"username": "zhang", "display_name": "张伟", "dept": "综合处",
              "max_level": 1, "role": "工作人员"}
    values.update(over)
    return User(**values)


def _doc(conn, *, dept="综合处", classification=1, title="预算说明"):
    doc = repo.create_document(
        conn, title=title, dept=dept, classification=classification,
        effective_date=None, created_by="zhang")
    version = repo.create_version(
        conn, document_id=doc["id"], version_no=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        filename="a.xlsx", mime="application/vnd.ms-excel", size_bytes=100,
        storage_path=f"aa/{uuid.uuid4().hex}", parser="xlsx",
        parser_version="0.1.0", status="ready")
    conn.execute(
        "UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
        (version["id"], doc["id"]))
    conn.commit()
    return repo.get_document(conn, doc["id"]), version


def _add_chunk(conn, doc, version, ordinal, content, vector, *,
               chunk_type="text", extra=None, active=True):
    chunk_id = f"{doc['id']}@v1#{ordinal}"
    metadata = {
        "chunk_id": chunk_id, "chunk_type": chunk_type, "title": doc["title"],
        "dept": doc["dept"], "classification": doc["classification"],
        "source": "a.xlsx", **(extra or {}),
    }
    tokens = " ".join(tokenize(content))
    conn.execute(
        """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type,
               content, content_tokens, tsv, embedding, embed_model, dept,
               classification, metadata, chunk_hash, active)
           VALUES (%s,%s,%s,%s,%s,%s,to_tsvector('simple', %s),%s,'bge-m3',
                   %s,%s,%s,%s,%s)""",
        (doc["id"], version["id"], ordinal, chunk_type, content, tokens, tokens,
         Vector(vector), doc["dept"], doc["classification"], Jsonb(metadata),
         uuid.uuid4().hex + uuid.uuid4().hex, active))
    conn.commit()
    return chunk_id


def test_acl_prefilters_before_retrieval(conn):
    user = _user()
    own, own_version = _doc(conn, dept="综合处", classification=1)
    _add_chunk(conn, own, own_version, 0, "预算 编制", _vec(0))
    public, public_version = _doc(conn, dept="人事处", classification=0,
                                  title="全局公开")
    _add_chunk(conn, public, public_version, 0, "预算 公开", _vec(1))
    secret, secret_version = _doc(conn, dept="综合处", classification=2,
                                  title="秘密文档")
    _add_chunk(conn, secret, secret_version, 0, "预算 秘密", _vec(0))
    other, other_version = _doc(conn, dept="人事处", classification=1,
                                title="他部门内部")
    _add_chunk(conn, other, other_version, 0, "预算 内部", _vec(0))

    outcome = retrieval.retrieve(conn, user, "预算",
                                 embedder=FakeEmbedder(_vec(0)))

    assert outcome.visible_chunks == 2
    assert outcome.visible_docs == 2
    assert {h.doc_id for h in outcome.hits} == {
        str(own["id"]), str(public["id"])}


def test_dense_and_sparse_fuse_by_rrf(conn):
    user = _user()
    doc, version = _doc(conn)
    dense_chunk = _add_chunk(conn, doc, version, 0, "第一条 总则", _vec(0))
    sparse_chunk = _add_chunk(conn, doc, version, 1, "预算 编制 说明", _vec(1))

    outcome = retrieval.retrieve(conn, user, "预算",
                                 embedder=FakeEmbedder(_vec(0)))

    ids = [h.chunk_id for h in outcome.hits]
    assert ids[0] == sparse_chunk   # 双路命中，RRF 分数更高
    assert dense_chunk in ids
    assert outcome.sparse_hits >= 1
    assert outcome.grounded is True


def test_low_relevance_refuses_with_best_distance(conn):
    user = _user()
    doc, version = _doc(conn)
    _add_chunk(conn, doc, version, 0, "预算 数据", _vec(0))

    outcome = retrieval.retrieve(conn, user, "预算",
                                 embedder=FakeEmbedder(_vec(5)))

    assert outcome.grounded is False
    assert outcome.reason == "low_relevance"
    assert outcome.best_distance == pytest.approx(2 ** 0.5)
    assert outcome.hits


def test_no_visible_chunks_refuses_without_embedding(conn):
    user = _user()
    doc, version = _doc(conn, classification=2)
    _add_chunk(conn, doc, version, 0, "预算 秘密", _vec(0))

    outcome = retrieval.retrieve(conn, user, "预算", embedder=BoomEmbedder())

    assert outcome.visible_chunks == 0
    assert outcome.reason == "no_visible_docs"
    assert outcome.hits == []


def test_table_rows_pull_adjacent_windows(conn):
    user = _user()
    doc, version = _doc(conn)
    for ordinal, (text, vector) in enumerate([
        ("第 1-10 行 其他 内容", _vec(1)),
        ("第 11-20 行 预算 数据", _vec(0)),
        ("第 21-30 行 其他 内容", _vec(2)),
    ]):
        _add_chunk(conn, doc, version, ordinal, text, vector,
                   chunk_type="table_rows",
                   extra={"table_key": "t1",
                          "row_range": f"{ordinal * 10 + 1}-{ordinal * 10 + 10}"})
    settings = replace(get_settings(), dense_top=1, sparse_top=1)

    outcome = retrieval.retrieve(conn, user, "预算", settings=settings,
                                 embedder=FakeEmbedder(_vec(0)))

    assert [h.ordinal for h in outcome.hits] == [1, 0, 2]
    assert [h.expanded for h in outcome.hits] == [False, True, True]


def test_fusion_top_caps_context(conn):
    user = _user()
    doc, version = _doc(conn)
    for ordinal in range(7):
        _add_chunk(conn, doc, version, ordinal, f"预算 条目 {ordinal}", _vec(0))

    outcome = retrieval.retrieve(conn, user, "预算",
                                 embedder=FakeEmbedder(_vec(0)))

    assert len(outcome.hits) == 5


def test_sparse_query_tolerates_punctuation(conn):
    user = _user()
    doc, version = _doc(conn)
    _add_chunk(conn, doc, version, 0, "2025 年 预算 （万元）", _vec(1))

    outcome = retrieval.retrieve(conn, user, "预算（万元）& 2025!",
                                 embedder=FakeEmbedder(_vec(9)))

    assert outcome.sparse_hits == 1
    assert outcome.grounded is False


def test_hits_carry_citation_fields(conn):
    user = _user()
    doc, version = _doc(conn)
    _add_chunk(conn, doc, version, 0, "预算 数据", _vec(0))

    outcome = retrieval.retrieve(conn, user, "预算",
                                 embedder=FakeEmbedder(_vec(0)))

    hit = outcome.hits[0]
    assert re.fullmatch(r"[0-9a-f-]{36}@v\d+#\d+", hit.chunk_id)
    assert hit.title == "预算说明"
    assert hit.dept == "综合处"
    assert hit.classification == 1
    assert hit.effective_date == ""
    assert hit.metadata["source"] == "a.xlsx"
    assert hit.expanded is False
