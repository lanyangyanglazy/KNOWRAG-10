"""政务试点原型的测试：密级解析、权限裁剪、拒答、审计、SSE 接口。"""

import json

import pytest
from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage

from app import rag_service as rs
from app import server
from app.users import CLASSIFICATION_LEVELS, User, can_view, get_user


# ---------- 测试替身 ----------

class _FakeEmbeddings(Embeddings):
    """确定性假 Embedding：不联网，按字符编码生成定维向量。"""

    def __init__(self, dim=24):
        self.dim = dim

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

    def _vec(self, text):
        base = sum(ord(c) for c in text)
        return [((base + i) % 97) / 97 for i in range(self.dim)]


class _FakeLLM:
    """假 LLM：stream 产出含 <think> 的分片，invoke 用于追问改写。"""

    def __init__(self, rewrite=None, pieces=None):
        self.rewrite = rewrite
        self.pieces = pieces or ["<think>内部推理</think>", "这是", "答案"]
        self.stream_called = False

    def invoke(self, messages):
        return AIMessage(content=self.rewrite or "")

    def stream(self, messages):
        self.stream_called = True
        for piece in self.pieces:
            yield AIMessage(content=piece)


@pytest.fixture()
def service(monkeypatch):
    """构造一个不落盘、不联网的 RagService（语料用真实 gov_docs）。"""
    monkeypatch.setattr(
        rs,
        "build_or_load_index",
        lambda documents, embed_model: FAISS.from_documents(documents, embed_model),
    )
    return rs.RagService(llm=_FakeLLM(), embed_model=_FakeEmbeddings())


# ---------- 语料与密级 ----------

class TestGovCorpus:
    def test_documents_have_required_metadata(self):
        docs = rs.load_gov_documents()
        assert docs, "演示语料不应为空"
        chunk_ids = set()
        for doc in docs:
            md = doc.metadata
            assert md["doc_id"] and md["title"] and md["dept"]
            assert md["classification"] in CLASSIFICATION_LEVELS
            assert md["classification_level"] == CLASSIFICATION_LEVELS[md["classification"]]
            assert md["chunk_id"] not in chunk_ids, "chunk_id 必须唯一，RRF 依赖它去重"
            chunk_ids.add(md["chunk_id"])
            assert "doc_id:" not in doc.page_content, "front matter 不应进入切片正文"

    def test_demo_corpus_covers_three_levels(self):
        levels = {doc.metadata["classification"] for doc in rs.load_gov_documents()}
        assert levels == {"公开", "内部", "秘密"}

    def test_front_matter_requires_fields(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text("---\ndoc_id: x\n---\n正文", encoding="utf-8")
        with pytest.raises(ValueError, match="缺少必填元数据"):
            rs.load_gov_docs(tmp_path)

    def test_unknown_classification_rejected(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text(
            "---\ndoc_id: x\ntitle: t\ndept: d\nclassification: 绝密\n---\n正文",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="classification"):
            rs.load_gov_docs(tmp_path)

    def test_signature_changes_with_corpus(self, tmp_path, monkeypatch):
        (tmp_path / "a.md").write_text(
            "---\ndoc_id: a\ntitle: t\ndept: d\nclassification: 公开\n---\n正文",
            encoding="utf-8",
        )
        sig1 = rs.corpus_signature(tmp_path)
        (tmp_path / "a.md").write_text(
            "---\ndoc_id: a\ntitle: t\ndept: d\nclassification: 内部\n---\n正文",
            encoding="utf-8",
        )
        assert rs.corpus_signature(tmp_path) != sig1, "改密级也必须触发索引重建"

    def test_bad_documents_are_skipped_in_lenient_mode(self, tmp_path):
        """单个坏文件不应拖垮整库：运行时跳过并留下可定位的 issue。"""
        (tmp_path / "good.md").write_text(
            "---\ndoc_id: a\ntitle: t\ndept: d\nclassification: 公开\n---\n正文",
            encoding="utf-8",
        )
        (tmp_path / "bom.md").write_bytes(
            "\ufeff---\ndoc_id: b\ntitle: t\ndept: d\nclassification: 公开\n---\n正文".encode("utf-8")
        )
        (tmp_path / "missing.md").write_text(
            "---\ndoc_id: c\ntitle: t\ndept:\nclassification: 公开\n---\n正文",
            encoding="utf-8",
        )

        issues = []
        docs = rs.load_gov_docs(tmp_path, strict=False, issues=issues)

        assert [d.metadata["doc_id"] for d in docs] == ["a"]
        assert {i.path.name for i in issues} == {"bom.md", "missing.md"}
        assert all(i.reason for i in issues)

    def test_all_bad_documents_still_fail(self, tmp_path):
        """全部文档都坏时不能静默变成「空知识库」。"""
        (tmp_path / "bad.md").write_text("没有 front matter", encoding="utf-8")
        with pytest.raises(RuntimeError, match="没有任何可加载的文档"):
            rs.load_gov_docs(tmp_path, strict=False)

    def test_strict_mode_still_fails_fast(self, tmp_path):
        (tmp_path / "bad.md").write_text("---\ndoc_id: x\n---\n正文", encoding="utf-8")
        with pytest.raises(ValueError, match="缺少必填元数据"):
            rs.load_gov_docs(tmp_path, strict=True)


# ---------- 文档级权限 ----------

class TestAccessControl:
    @pytest.mark.parametrize(
        "username,doc_id,expected",
        [
            ("zhang", "gov-001", True),   # 公开，全员可见
            ("zhang", "gov-002", False),  # 内部，别的部门
            ("zhang", "gov-003", False),  # 秘密，超密级且别部门
            ("li", "gov-002", True),      # 本部门内部文档
            ("li", "gov-003", False),     # 超密级
            ("wang", "gov-003", True),    # 本部门 + 足够密级
            ("wang", "gov-002", False),   # 同密级但别部门
            ("chen", "gov-004", True),
            ("chen", "gov-003", False),   # 高密级也不能跨部门看秘密
        ],
    )
    def test_can_view_matrix(self, username, doc_id, expected):
        user = get_user(username)
        docs = {
            d.metadata["doc_id"]: d
            for d in rs.load_gov_documents()
        }
        assert can_view(user, docs[doc_id].metadata) is expected

    def test_fail_closed_on_missing_or_bad_level(self):
        user = get_user("wang")
        assert can_view(user, {"dept": "财务处"}) is False
        assert can_view(user, {"dept": "财务处", "classification_level": "2"}) is False

    def test_visible_documents_are_prefiltered(self, service):
        zhang = get_user("zhang")
        visible = service.visible_documents(zhang)
        assert visible, "张伟至少能看到公开文档"
        assert all(can_view(zhang, d.metadata) for d in visible)
        assert all(d.metadata["doc_id"] != "gov-003" for d in visible)

        wang = get_user("wang")
        wang_docs = {d.metadata["doc_id"] for d in service.visible_documents(wang)}
        assert "gov-003" in wang_docs

    def test_retrieval_never_returns_hidden_docs(self, service, monkeypatch):
        """权限裁剪发生在检索前：无论问什么，结果都只来自可见集合。"""
        monkeypatch.setattr(rs, "max_distance", lambda: 999.0)  # 关掉相关性门限
        zhang = get_user("zhang")
        result = service.retrieve(zhang, "差旅住宿费标准 预算 审批权限")
        assert result.docs
        for doc in result.docs:
            assert can_view(zhang, doc.metadata), "越权切片进入了结果集"
            assert doc.metadata["doc_id"] != "gov-003"

    def test_no_visible_docs_refuses_without_llm(self, service):
        outsider = User("outsider", "外部人员", "外部单位", max_level=-1, role="访客")
        events = list(service.answer_stream(outsider, "差旅住宿费标准是多少？"))
        assert events[-1]["type"] == "refusal"
        assert events[-1]["reason"] == "no_visible_docs"
        assert service.llm.stream_called is False, "拒答不应调用生成模型"


# ---------- 拒答与生成 ----------

class TestAnswerStream:
    def test_low_relevance_refuses(self, service, monkeypatch):
        monkeypatch.setattr(rs, "max_distance", lambda: -1.0)  # 任何距离都算不相关
        user = get_user("zhang")
        events = list(service.answer_stream(user, "完全无关的问题"))
        assert events[-1]["type"] == "refusal"
        assert events[-1]["reason"] in {"low_relevance", "no_dense_hits"}
        assert service.llm.stream_called is False

    def test_grounded_streams_clean_answer(self, service, monkeypatch):
        monkeypatch.setattr(rs, "max_distance", lambda: 999.0)
        user = get_user("wang")
        events = list(service.answer_stream(user, "差旅住宿费标准是多少？"))

        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        assert tokens == "这是答案", "<think> 段必须被增量过滤掉"
        assert all("<think>" not in t for t in [tokens])

        citations = next(e for e in events if e["type"] == "citations")["items"]
        assert citations and all("classification" in c for c in citations)

        done = events[-1]
        assert done["type"] == "done"
        assert done["answer"] == "这是答案"
        assert service.history(user.username), "成功回答后应写入会话历史"

    def test_history_trimmed_to_max_turns(self, service):
        service.history_turns = 2
        user = get_user("wang")
        for i in range(5):
            service._remember(user.username, f"问题{i}", f"答案{i}")
        assert len(service.history(user.username)) == 4

        service.clear_history(user.username)
        assert service.history(user.username) == []


# ---------- 审计 ----------

def test_write_audit_appends_jsonl(tmp_path, monkeypatch):
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(server, "AUDIT_FILE", audit_file)

    server.write_audit({"user": "wang", "question": "差旅住宿费标准是多少？", "refused": False})
    server.write_audit({"user": "zhang", "question": "差旅住宿费标准是多少？", "refused": True})

    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["ts"] and first["user"] == "wang"
    assert second["refused"] is True


# ---------- HTTP 接口（不依赖 Ollama） ----------

class _FakeService:
    """HTTP 层测试替身：固定返回拒答事件。"""

    def history(self, username):
        return []

    def clear_history(self, username):
        pass

    def visible_docs(self, user):
        return []

    def stats(self):
        return {
            "docs": 0, "chunks": 0, "llm_model": "fake-llm",
            "embed_model": "fake-embed", "llm_provider": "fake",
            "chunks_by_classification": {}, "max_distance": 0.75,
        }

    def answer_stream(self, user, question, history):
        yield {
            "type": "refusal", "reason": "no_visible_docs", "message": "无依据",
            "visible_docs": 0, "visible_chunks": 0, "best_distance": None,
            "elapsed_ms": 1,
        }


TEST_PASSWORD = "test-pass"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DEMO_PASSWORD", TEST_PASSWORD)
    monkeypatch.setattr(server, "get_service", lambda: _FakeService())
    monkeypatch.setattr(server, "AUDIT_FILE", tmp_path / "audit.jsonl")
    server._LOGIN_FAILURES.clear()
    server._REVOKED.clear()
    return TestClient(server.app)


def _login(client, username="zhang", password=TEST_PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


def _audit_records(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class _BrokenService(_FakeService):
    """生成阶段抛异常的替身，用于验证审计的 error 状态与信息脱敏。"""

    def answer_stream(self, user, question, history):
        raise RuntimeError("boom: internal endpoint http://10.0.0.1:11434")
        yield  # pragma: no cover - 让函数成为生成器


class TestHttpApi:
    def test_chat_requires_login(self, client):
        res = client.post("/api/chat", json={"question": "你好"})
        assert res.status_code == 401

    def test_login_requires_password(self, client):
        assert client.post("/api/login", json={"username": "zhang"}).status_code == 401
        assert _login(client, password="wrong").status_code == 401
        # 正确口令仍可登录，且失败计数被清零
        assert _login(client).status_code == 200

    def test_login_then_sse_refusal_and_audit(self, client, tmp_path):
        res = _login(client)
        assert res.status_code == 200
        assert res.json()["user"]["dept"] == "综合处"

        with client.stream("POST", "/api/chat", json={"question": "差旅住宿费标准？"}) as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            assert res.headers["x-request-id"]
            body = "".join(res.iter_text())

        assert "refusal" in body
        assert "data: " in body

        record = _audit_records(tmp_path / "audit.jsonl")[-1]
        assert record["event"] == "chat"
        assert record["status"] == "refused"
        assert record["error"] is None
        assert record["user"] == "zhang"
        assert record["refused"] is True
        assert record["question"] == "差旅住宿费标准？"
        assert record["request_id"]
        assert record["visible_docs"] == [], "审计只记文档 ID 列表"
        # 审计时间必须带时区偏移，否则跨时区无法与业务时间对齐
        from datetime import datetime

        assert datetime.fromisoformat(record["ts"]).utcoffset() is not None

    def test_login_lockout_after_failures(self, client):
        for _ in range(server.LOGIN_MAX_FAILURES):
            assert _login(client, password="wrong").status_code == 401
        blocked = _login(client)  # 口令正确，但已被锁定
        assert blocked.status_code == 429
        assert blocked.headers.get("retry-after")

    def test_unknown_user_cannot_login(self, client):
        assert client.post("/api/login", json={"username": "nobody"}).status_code == 401

    def test_users_endpoint_does_not_leak_classification(self, client):
        users = client.get("/api/users").json()["users"]
        assert users
        for item in users:
            assert "max_level" not in item
            assert "max_classification" not in item

    def test_health_returns_503_when_not_ready(self, client):
        res = client.get("/api/health")
        assert res.status_code == 503
        payload = res.json()
        assert payload["ok"] is False
        assert "chunks_by_classification" not in json.dumps(payload)

    def test_clear_history_endpoint(self, client):
        _login(client, "li")
        assert client.post("/api/history/clear").json() == {"ok": True}

    def test_logout_invalidates_session(self, client):
        _login(client, "li")
        client.post("/api/logout")
        assert client.get("/api/me").status_code == 401

    def test_logout_revokes_token_server_side(self, client):
        """旧实现只删浏览器 Cookie，服务端 token 仍可重放；现在必须吊销。"""
        from fastapi.testclient import TestClient

        assert _login(client, "wang").status_code == 200
        token = client.cookies.get(server.COOKIE_NAME)
        assert token
        client.post("/api/logout")

        replayed = TestClient(server.app)
        replayed.cookies.set(server.COOKIE_NAME, token)
        assert replayed.get("/api/me").status_code == 401

    def test_expired_token_rejected(self, client, monkeypatch):
        from fastapi.testclient import TestClient

        assert _login(client, "li").status_code == 200
        token = client.cookies.get(server.COOKIE_NAME)
        monkeypatch.setattr(server, "SESSION_MAX_AGE", -1)  # 任何 token 都已过期

        stale = TestClient(server.app)
        stale.cookies.set(server.COOKIE_NAME, token)
        assert stale.get("/api/me").status_code == 401

    def test_audit_login_and_logout_events(self, client, tmp_path):
        _login(client, "zhang")
        client.post("/api/logout")
        events = [(r["event"], r.get("status")) for r in _audit_records(tmp_path / "audit.jsonl")]
        assert ("auth.login", "ok") in events
        assert ("auth.logout", "ok") in events

    def test_audit_error_status_and_no_internal_leak(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "get_service", lambda: _BrokenService())
        _login(client)

        with client.stream("POST", "/api/chat", json={"question": "任意问题"}) as res:
            body = "".join(res.iter_text())

        assert "10.0.0.1" not in body, "内部地址不应出现在前端响应里"
        record = _audit_records(tmp_path / "audit.jsonl")[-1]
        assert record["status"] == "error"
        assert "RuntimeError" in record["error"]

    def test_chat_fail_closed_when_audit_unavailable(self, client, monkeypatch):
        _login(client)
        monkeypatch.setattr(server, "audit_ready", lambda: (False, "disk full"))
        assert client.post("/api/chat", json={"question": "你好"}).status_code == 503

    def test_chat_rejects_when_concurrency_saturated(self, client, monkeypatch):
        import threading

        slots = threading.BoundedSemaphore(1)
        assert slots.acquire(blocking=False)
        monkeypatch.setattr(server, "_chat_slots", slots)
        _login(client)

        with client.stream("POST", "/api/chat", json={"question": "任意问题"}) as res:
            body = "".join(res.iter_text())

        assert "请求过多" in body

    def test_chat_times_out_and_reports_error(self, client, monkeypatch, tmp_path):
        _login(client)
        monkeypatch.setattr(server.core, "LLM_TIMEOUT_S", 0.0)

        with client.stream("POST", "/api/chat", json={"question": "任意问题"}) as res:
            body = "".join(res.iter_text())

        assert '"type": "error"' in body
        record = _audit_records(tmp_path / "audit.jsonl")[-1]
        assert record["status"] == "error"
        assert "TimeoutError" in record["error"]
