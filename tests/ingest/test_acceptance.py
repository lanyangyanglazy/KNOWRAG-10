from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from langchain_core.messages import AIMessage

from app import rag_service as rs
from app import server
from app.users import get_user
from ingest import embedding, pipeline
from tests.ingest.builders import build_xlsx

DIM = 1024


class FakeLLM:
    def invoke(self, messages):
        return AIMessage(content="")

    def stream(self, messages):
        yield AIMessage(content="住宿费按差旅费管理办法执行。")


class FakeEmbedder:
    model = "bge-m3"
    dim = DIM

    def embed(self, texts):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


@pytest.fixture()
def pg_service(monkeypatch):
    """pg 模式的真实服务，只替换 LLM 与嵌入模型，测试不依赖 Ollama。"""
    monkeypatch.setenv("RAG_MAX_DISTANCE", "999")
    service = rs.RagService(llm=FakeLLM(), backend="pg", embedder=FakeEmbedder())
    monkeypatch.setattr(server, "get_service", lambda: service)
    return service


def _upload(client, path, **form):
    return client.post(
        "/api/documents",
        files={"file": (path.name, path.read_bytes())},
        data={k: str(v) for k, v in form.items()},
    )


def _parse(conn, version_id):
    pipeline.handle_parse(conn, {"version_id": version_id})
    # 上传时入队的 parse job 已由测试直接执行，标记 done，避免留下僵尸任务
    conn.execute(
        """UPDATE jobs SET status='done', finished_at=now()
            WHERE kind='parse' AND status='queued'
              AND payload->>'version_id' = %s""", (version_id,))
    conn.commit()


def _queued_payload(conn, kind):
    row = conn.execute(
        """SELECT payload FROM jobs
            WHERE kind=%s AND status='queued'
            ORDER BY id DESC LIMIT 1""", (kind,)).fetchone()
    assert row is not None, f"没有待执行的 {kind} 任务"
    return row["payload"]


def _drain_post_parse(conn, monkeypatch, embedder):
    """执行 embed → activate；嵌入模型换成测试替身。"""
    def fake_embed(db, version_id):
        return embedding.embed_version(db, version_id, embedder=embedder)

    monkeypatch.setattr(pipeline, "embed_version", fake_embed)
    for kind in ("embed", "activate"):
        pipeline.HANDLERS[kind](conn, _queued_payload(conn, kind))


def _events(response):
    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def _source_url(chunk_id):
    # chunk_id 含 `#`，直接拼 URL 会被 httpx 当成 fragment 丢掉，必须百分号编码。
    return f"/api/chunks/{quote(chunk_id, safe='')}/source"


def test_full_chain_upload_review_embed_activate_chat_and_source(
        api, conn, pg_service, monkeypatch, tmp_path):
    make, _ = api
    client = make("wang")

    xlsx = build_xlsx(tmp_path / "预算表.xlsx",
                      [["部门", "预算金额"], ["综合处", 100], ["人事处", 200]])
    created = _upload(client, xlsx, title="预算表", effective_date="2026-01-01")
    assert created.status_code == 202, created.text
    doc_id = created.json()["document_id"]
    version_id = created.json()["version_id"]

    # 1) 解析：未定级 → 复核项，版本停在 needs_review，尚未产生切片
    _parse(conn, version_id)
    assert conn.execute(
        "SELECT status FROM doc_versions WHERE id=%s", (version_id,)
    ).fetchone()["status"] == "needs_review"
    assert conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 0

    review = client.get(f"/api/versions/{version_id}/review")
    assert review.status_code == 200, review.text
    assert "classification" in {item["kind"] for item in review.json()["open_items"]}

    # 2) 复核：定级为内部 → embedding，并入队 embed
    confirmed = client.post(f"/api/versions/{version_id}/confirm",
                            json={"meta": {"classification": 1}})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "embedding"

    # 3) 嵌入 + 激活
    _drain_post_parse(conn, monkeypatch, FakeEmbedder())
    doc = conn.execute(
        "SELECT status, current_version_id FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert doc["status"] == "active"
    assert str(doc["current_version_id"]) == version_id
    assert conn.execute(
        "SELECT count(*) AS n FROM chunks WHERE active").fetchone()["n"] > 0

    # 4) 问答：SSE 引用带 chunk_id
    chat = client.post("/api/chat", json={"question": "预算金额是多少"})
    assert chat.status_code == 200, chat.text
    events = _events(chat)
    citations = next(item for item in events if item["type"] == "citations")["items"]
    assert citations[0]["doc_id"] == doc_id
    chunk_id = citations[0]["chunk_id"]
    assert chunk_id.startswith(f"{doc_id}@v1#")
    assert next(item for item in events if item["type"] == "done")["answer"]

    # 5) 回源：本部门可见；跨部门一律 404（回源不复用管理台可见性）
    source = client.get(_source_url(chunk_id))
    assert source.status_code == 200, source.text
    assert "预算" in source.json()["content"]

    outsider = make("zhang")
    assert outsider.get(_source_url(chunk_id)).status_code == 404
    assert all(item["id"] != doc_id
               for item in outsider.get("/api/documents").json()["items"])


def test_unclassified_and_deactivated_documents_are_not_retrievable(
        api, conn, pg_service, monkeypatch, tmp_path):
    make, _ = api
    client = make("wang")

    xlsx = build_xlsx(tmp_path / "制度.xlsx", [["部门", "金额"], ["综合处", 10]])
    created = _upload(client, xlsx, title="制度")
    doc_id = created.json()["document_id"]
    version_id = created.json()["version_id"]

    # 未定级：即使解析完成，检索也必须拒答
    _parse(conn, version_id)
    assert pg_service.retrieve(get_user("wang"), "金额").grounded is False

    client.post(f"/api/versions/{version_id}/confirm",
                json={"meta": {"classification": 1}})
    _drain_post_parse(conn, monkeypatch, FakeEmbedder())
    assert pg_service.retrieve(get_user("wang"), "金额").grounded is True

    # 下线后立即不可检索（切片 active=false）
    assert client.post(f"/api/documents/{doc_id}/deactivate").status_code == 200
    result = pg_service.retrieve(get_user("wang"), "金额")
    assert result.grounded is False
    assert result.reason == "no_visible_docs"
