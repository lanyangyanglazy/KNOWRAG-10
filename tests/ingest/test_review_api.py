from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from pgvector import Vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.users import User
from ingest import repository as repo
from ingest import retrieval, review_service
from ingest.errors import Forbidden
from ingest.ir import Block, Column, DocMeta, DocModel, ParserInfo, Table, Warning
from tests.ingest.conftest import fetch_all

DIM = 1024


def build_ir(*, repeated_header: bool = False,
             second_table: bool = False) -> DocModel:
    rows = [["综合处", "100"], ["人事处", "90"]]
    repeated: list[int] = []
    if repeated_header:
        # 数据区第二行重复表头（绝对行号 4），用于 footer 与重复表头重叠的用例。
        rows = [["综合处", "100"], ["部门", "预算"]]
        repeated = [4]
    table = Table(
        table_key="t1", sheet="2025预算", range="A1:B4",
        header_rows=[2], header_values=[["部门", "预算"]],
        pre_rows=[["2025年部门预算表", ""]],
        columns=[Column(key="c1", name="部门"), Column(key="c2", name="预算")],
        rows=rows, repeated_header_rows=repeated,
        confidence=0.6, source="heuristic",
        candidates=[{"start_row": 2, "depth": 1, "confidence": 0.6}],
        flags=["merged_cells"],
    )
    blocks = [Block(block_id="b1", type="table", table=table)]
    if second_table:
        # 多表场景：apply_template 只为 tables[0] 保存模板，审计不应带上其余补丁表。
        blocks.append(Block(block_id="b2", type="table", table=Table(
            table_key="t2", sheet="2025预算", range="D1:E2",
            header_rows=[1], header_values=[["项目", "金额"]],
            columns=[Column(key="c1", name="项目"), Column(key="c2", name="金额")],
            rows=[["差旅", "10"]],
            confidence=0.5, source="heuristic",
        )))
    return DocModel(
        parser=ParserInfo(name="xlsx", version="0.1.0"),
        meta=DocMeta(title="2025年部门预算表", source_filename="预算.xlsx",
                     sha256="b" * 64),
        blocks=blocks,
        warnings=[Warning(code="merged_cells", detail="1 个合并区间")],
    )


def seed(db_url: str, *, classification: int | None = 1,
         structure: bool = True, dept: str = "财务处",
         repeated_header: bool = False,
         second_table: bool = False) -> tuple[str, str]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        doc = repo.create_document(conn, title="2025年部门预算表", dept=dept,
                                   classification=classification,
                                   effective_date=None, created_by="wang")
        version = repo.create_version(
            conn, document_id=doc["id"], version_no=1, sha256="b" * 64,
            filename="预算.xlsx", mime="application/octet-stream", size_bytes=10,
            storage_path="bb/" + "b" * 64, parser="xlsx", parser_version="0.1.0")
        repo.set_version_status(
            conn, version["id"], "needs_review",
            ir=build_ir(repeated_header=repeated_header,
                        second_table=second_table),
            warnings=[{"code": "merged_cells", "detail": "1 个合并区间"}])

        if structure:
            repo.ensure_review_item(conn, version["id"], "structure",
                                    {"reason": "structure", "tables": []})
        if classification is None:
            repo.ensure_review_item(conn, version["id"], "classification",
                                    {"reason": "unclassified"})
        elif classification == 2:
            repo.ensure_review_item(conn, version["id"], "classification",
                                    {"reason": "secret_two_person"})
        conn.commit()
        return str(doc["id"]), str(version["id"])


def seed_chunks(db_url: str, doc_id: str, version_id: str, *,
                classification: int = 2, dept: str = "财务处",
                count: int = 2) -> None:
    """给版本塞几片 active 切片，用于验证改密级会同步到所有切片。"""
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        for ordinal in range(count):
            content = f"预算 片段{ordinal}"
            conn.execute(
                """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type,
                       content, content_tokens, embed_model, dept, classification,
                       metadata, chunk_hash, active)
                   VALUES (%s,%s,%s,'text',%s,%s,'bge-m3',%s,%s,%s,%s,true)""",
                (doc_id, version_id, ordinal, content, content, dept,
                 classification, Jsonb({}), f"{ordinal:064d}"),
            )
        conn.commit()


def chunk_levels(db_url: str, doc_id: str) -> list[int]:
    rows = fetch_all(
        db_url,
        "SELECT classification FROM chunks WHERE document_id=%s ORDER BY version_id, ordinal",
        (doc_id,))
    return [row["classification"] for row in rows]


def test_review_summary_and_preview(api):
    make, db_url = api
    doc_id, version_id = seed(db_url)
    client = make("wang")

    summary = client.get(f"/api/versions/{version_id}/review")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["document"]["id"] == doc_id
    assert body["tables"][0]["table_key"] == "t1"
    assert body["tables"][0]["header_rows"] == [2]
    assert [c["name"] for c in body["tables"][0]["columns"]] == ["部门", "预算"]
    assert len(body["open_items"]) == 1

    preview = client.get(f"/api/versions/{version_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["kind"] == "xlsx"
    assert preview.json()["tables"][0]["grid"][0][0] == "2025年部门预算表"


def test_confirm_rebuilds_header_and_enqueues_embed(api):
    make, db_url = api
    _, version_id = seed(db_url)
    client = make("wang")

    resp = client.post(f"/api/versions/{version_id}/confirm", json={
        "tables": [{
            "table_key": "t1",
            "header_rows": [1, 2],
            "columns": [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "预算"}],
        }],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "embedding"

    version = fetch_all(db_url, "SELECT status, ir FROM doc_versions")[0]
    assert version["status"] == "embedding"
    table = version["ir"]["blocks"][0]["table"]
    assert table["header_rows"] == [1, 2]
    assert table["header_values"] == [["2025年部门预算表", ""], ["部门", "预算"]]
    assert table["rows"] == [["综合处", "100"], ["人事处", "90"]]
    assert table["source"] == "human" and table["confidence"] == 1.0
    assert fetch_all(db_url, "SELECT status FROM review_items")[0]["status"] == "resolved"
    assert [j["kind"] for j in fetch_all(db_url, "SELECT kind FROM jobs")] == ["embed"]


def test_confirm_sets_unclassified_document_level(api):
    make, db_url = api
    _, version_id = seed(db_url, classification=None, structure=False)
    client = make("wang")

    missing = client.post(f"/api/versions/{version_id}/confirm", json={})
    assert missing.status_code == 422

    resp = client.post(f"/api/versions/{version_id}/confirm",
                       json={"meta": {"classification": 1}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "embedding"
    assert fetch_all(db_url, "SELECT classification FROM documents")[0]["classification"] == 1
    assert fetch_all(db_url, "SELECT status FROM review_items")[0]["status"] == "resolved"


def test_unknown_table_key_returns_422(api):
    make, db_url = api
    _, version_id = seed(db_url)
    client = make("wang")

    resp = client.post(f"/api/versions/{version_id}/confirm", json={
        "tables": [{
            "table_key": "nope",
            "header_rows": [2],
            "columns": [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "预算"}],
        }],
    })
    assert resp.status_code == 422
    assert resp.json()["code"] == "review_required"
    assert fetch_all(db_url, "SELECT status FROM doc_versions")[0]["status"] == "needs_review"


def test_footer_overlap_header_rejected(api):
    make, db_url = api
    _, version_id = seed(db_url)
    client = make("wang")

    resp = client.post(f"/api/versions/{version_id}/confirm", json={
        "tables": [{
            "table_key": "t1",
            "header_rows": [2],
            "columns": [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "预算"}],
            "footer_rows": [2],
        }],
    })
    assert resp.status_code == 422
    assert resp.json()["code"] == "review_required"


def test_footer_overlapping_repeated_header_returns_422(api):
    make, db_url = api
    _, version_id = seed(db_url, repeated_header=True)
    client = make("wang")

    # 手工范围内校验能过，但 Table 校验器会发现 row 4 同时是重复表头：
    # 不能把 pydantic ValidationError 漏成 500，必须转成 422 复核提示。
    resp = client.post(f"/api/versions/{version_id}/confirm", json={
        "tables": [{
            "table_key": "t1",
            "header_rows": [2],
            "columns": [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "预算"}],
            "footer_rows": [4],
        }],
    })
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "review_required"
    assert "footer_rows" in resp.json()["detail"]

    version = fetch_all(db_url, "SELECT status, ir FROM doc_versions")[0]
    assert version["status"] == "needs_review"
    table = version["ir"]["blocks"][0]["table"]
    assert table["footer_rows"] == []
    assert table["repeated_header_rows"] == [4]


def test_secret_needs_two_distinct_approvers(api):
    make, db_url = api
    _, version_id = seed(db_url, classification=2, structure=False)
    wang = make("wang")

    first = wang.post(f"/api/versions/{version_id}/confirm",
                      json={"meta": {"classification": 2}})
    assert first.status_code == 200
    assert first.json()["status"] == "needs_review"
    assert first.json()["pending_approvals"] == 1
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs")[0]["n"] == 0

    same_user = wang.post(f"/api/versions/{version_id}/approve",
                          json={"decision": "approve"})
    assert same_user.status_code == 403

    chen = make("chen")
    second = chen.post(f"/api/versions/{version_id}/approve",
                       json={"decision": "approve"})
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "embedding"
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs WHERE kind='embed'")[0]["n"] == 1


def test_secret_downgrade_requires_two_approvers(api):
    make, db_url = api
    _, version_id = seed(db_url, classification=2, structure=False)
    wang = make("wang")

    first = wang.post(f"/api/versions/{version_id}/confirm",
                      json={"meta": {"classification": 2}})
    assert first.status_code == 200
    assert first.json()["status"] == "needs_review"
    assert first.json()["pending_approvals"] == 1

    # 同一人降密：不得新增选票，也不能因为密级已变为 1 就跳过双人确认入队。
    second = wang.post(f"/api/versions/{version_id}/confirm",
                       json={"meta": {"classification": 1}})
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "needs_review"
    assert second.json()["pending_approvals"] == 1
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs")[0]["n"] == 0

    # 降密已落库后 previous 不可见，同一人再 confirm 仍必须停在 pending，
    # 不能借「已不是秘密级」的分支直接解决密级复核项。
    repeat = wang.post(f"/api/versions/{version_id}/confirm",
                       json={"meta": {"classification": 1}})
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["status"] == "needs_review"
    assert repeat.json()["pending_approvals"] == 1
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs")[0]["n"] == 0

    chen = make("chen")
    final = chen.post(f"/api/versions/{version_id}/confirm",
                      json={"meta": {"classification": 1}})
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "embedding"
    assert fetch_all(db_url, "SELECT classification FROM documents")[0][
        "classification"] == 1
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs WHERE kind='embed'")[0][
        "n"] == 1


def test_approve_completes_downgrade_review(api):
    make, db_url = api
    _, version_id = seed(db_url, classification=2, structure=False)
    wang = make("wang")

    first = wang.post(f"/api/versions/{version_id}/confirm",
                      json={"meta": {"classification": 1}})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "needs_review"
    assert first.json()["pending_approvals"] == 1
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs")[0]["n"] == 0

    # 降密后文档已不是秘密级，但挂着的密级复核项仍可经 /approve 完成第二票。
    chen = make("chen")
    approved = chen.post(f"/api/versions/{version_id}/approve",
                         json={"decision": "approve"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "embedding"
    assert approved.json()["pending_approvals"] == 2
    assert approved.json()["classification"] == 1
    assert fetch_all(db_url, "SELECT status FROM doc_versions")[0][
        "status"] == "embedding"
    assert fetch_all(db_url, "SELECT status FROM review_items WHERE kind='classification'")[
        0]["status"] == "resolved"
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs WHERE kind='embed'")[0][
        "n"] == 1

    # 非秘密级且没有待确认的密级复核项时，/approve 仍必须拒绝。
    _, plain_version = seed(db_url, structure=False)
    plain = make("wang").post(f"/api/versions/{plain_version}/approve",
                              json={"decision": "approve"})
    assert plain.status_code == 422
    assert plain.json()["code"] == "review_required"


def test_waive_requires_reason_and_secret_cannot_waive(api):
    make, db_url = api
    _, version_id = seed(db_url)
    client = make("wang")

    missing = client.post(f"/api/versions/{version_id}/waive", json={})
    assert missing.status_code == 400

    ok = client.post(f"/api/versions/{version_id}/waive",
                     json={"reason": "版式特殊，已人工逐行核对"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "embedding"
    assert fetch_all(db_url, "SELECT status FROM review_items")[0]["status"] == "waived"

    _, secret_version = seed(db_url, classification=2, structure=False)
    denied = client.post(f"/api/versions/{secret_version}/waive",
                         json={"reason": "想跳过"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"


def test_template_create_list_and_scope(api):
    make, db_url = api
    _, version_id = seed(db_url)
    client = make("wang")

    created = client.post("/api/templates", json={
        "version_id": version_id, "table_key": "t1", "name": "部门预算表模板",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scope_dept"] == "财务处"
    assert body["signature"]["ncols"] == 2
    assert body["signature"]["header_depth"] == 1
    assert body["signature"]["relative_header_row"] == 1
    assert body["signature"]["columns_norm"] == ["部门", "预算"]
    assert body["config"]["header_rows"] == [2]

    listing = client.get("/api/templates").json()["items"]
    assert len(listing) == 1

    li = make("li")
    assert li.get("/api/templates").json()["items"] == []


def test_template_cross_department_forbidden(api):
    make, db_url = api
    _, version_id = seed(db_url, dept="人事处")
    li = make("li")

    resp = li.post("/api/templates", json={
        "version_id": version_id, "table_key": "t1", "name": "跨部门模板",
        "scope_dept": "财务处",
    })
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
    assert fetch_all(db_url, "SELECT count(*) AS n FROM layout_templates")[0]["n"] == 0


def test_review_requires_same_department_or_admin(api):
    make, db_url = api
    _, version_id = seed(db_url)

    li = make("li")
    assert li.get(f"/api/versions/{version_id}/review").status_code == 404

    chen = make("chen")
    assert chen.get(f"/api/versions/{version_id}/review").status_code == 200


def test_secret_review_hidden_from_low_clearance_same_dept(api):
    make, db_url = api
    _, version_id = seed(db_url, classification=2, structure=False, dept="综合处")

    # zhang 与文档同部门且有 reviewer 角色，但 max_level=1 < 2：不得泄露存在性。
    zhang = make("zhang")
    assert zhang.get(f"/api/versions/{version_id}/review").status_code == 404

    wang = make("wang")
    assert wang.get(f"/api/versions/{version_id}/review").status_code == 200


def test_review_mutations_write_audit(api, tmp_path, monkeypatch):
    from app import server

    make, db_url = api
    audit_file = tmp_path / "review-audit.jsonl"
    monkeypatch.setattr(server, "AUDIT_FILE", audit_file)
    client = make("wang")

    _, confirmed = seed(db_url, structure=False)
    assert client.post(f"/api/versions/{confirmed}/confirm", json={}).status_code == 200

    _, waived = seed(db_url)
    assert client.post(f"/api/versions/{waived}/waive",
                       json={"reason": "人工逐行核对"}).status_code == 200

    _, templated = seed(db_url)
    assert client.post("/api/templates", json={
        "version_id": templated, "table_key": "t1", "name": "审计模板",
    }).status_code == 201

    _, secret = seed(db_url, classification=2, structure=False)
    assert client.post(f"/api/versions/{secret}/confirm",
                       json={"meta": {"classification": 2}}).status_code == 200
    chen = make("chen")
    assert chen.post(f"/api/versions/{secret}/approve",
                     json={"decision": "approve"}).status_code == 200

    lines = [json.loads(line) for line in
             audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    actions = {event.get("action") for event in lines}
    assert {"review_submit", "review_waive", "template_create",
            "review_approve"} <= actions


def test_apply_template_is_audited(api, tmp_path, monkeypatch):
    from app import server

    make, db_url = api
    audit_file = tmp_path / "template-apply-audit.jsonl"
    monkeypatch.setattr(server, "AUDIT_FILE", audit_file)
    _, version_id = seed(db_url, second_table=True)
    client = make("wang")

    resp = client.post(f"/api/versions/{version_id}/confirm", json={
        "tables": [{
            "table_key": "t1",
            "header_rows": [2],
            "columns": [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "预算"}],
        }, {
            "table_key": "t2",
            "header_rows": [1],
            "columns": [{"key": "c1", "name": "项目"}, {"key": "c2", "name": "金额"}],
        }],
        "apply_template": True,
    })
    assert resp.status_code == 200, resp.text

    # apply_template=True 但没有表格补丁：服务端不会保存模板，不得写假的审计事件。
    _, empty_version = seed(db_url)
    empty = client.post(f"/api/versions/{empty_version}/confirm",
                        json={"apply_template": True})
    assert empty.status_code == 200, empty.text

    lines = [json.loads(line) for line in
             audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    applied = [event for event in lines if event.get("action") == "template_apply"]
    assert len(applied) == 1
    assert isinstance(applied[0]["version_id"], str)
    assert applied[0]["version_id"] == version_id
    # 实际只为 tables[0] 保存了模板，审计不能把其余补丁表也算进去。
    assert applied[0]["detail"]["table_keys"] == ["t1"]


def test_secret_downgrade_keeps_document_secret_until_second_approver(api):
    make, db_url = api
    doc_id, version_id = seed(db_url, classification=2, structure=False)
    seed_chunks(db_url, doc_id, version_id, classification=2)
    wang = make("wang")

    first = wang.post(f"/api/versions/{version_id}/confirm",
                      json={"meta": {"classification": 1}})
    assert first.status_code == 200, first.text
    body = first.json()
    # 一票即降密 = 单人把秘密内容暴露给内部级用户：目标密级必须挂起，
    # 文档与全部切片在第二票之前仍是秘密级（设计 §10.1）。
    assert body["status"] == "needs_review"
    assert body["pending_approvals"] == 1
    assert body["classification"] == 2
    assert body["previous_classification"] == 2
    assert body["reclassified"] is None
    assert fetch_all(db_url, "SELECT classification FROM documents WHERE id=%s",
                     (doc_id,))[0]["classification"] == 2
    assert chunk_levels(db_url, doc_id) == [2, 2]
    assert fetch_all(db_url, "SELECT count(*) AS n FROM jobs")[0]["n"] == 0

    chen = make("chen")
    final = chen.post(f"/api/versions/{version_id}/confirm",
                      json={"meta": {"classification": 1}})
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "embedding"
    assert final.json()["classification"] == 1
    assert final.json()["reclassified"] == {
        "classification": {"from": 2, "to": 1},
        "dept": {"from": "财务处", "to": "财务处"},
    }
    # 改密级要连该文档所有版本切片一起改，否则旧切片按旧密级继续可检索。
    assert chunk_levels(db_url, doc_id) == [1, 1]


def test_low_clearance_reviewer_cannot_vote_on_secret(api):
    make, db_url = api
    doc_id, version_id = seed(db_url, classification=2, structure=False,
                              dept="综合处")
    zhang = make("zhang")  # 同部门 reviewer，max_level=1

    denied = zhang.post(f"/api/versions/{version_id}/confirm",
                        json={"meta": {"classification": 1}})
    assert denied.status_code == 404
    assert fetch_all(db_url, "SELECT count(*) AS n FROM review_approvals")[0]["n"] == 0
    assert fetch_all(db_url, "SELECT classification FROM documents WHERE id=%s",
                     (doc_id,))[0]["classification"] == 2

    # 服务层兜底：即便以「管理员但密级不足」的身份绕过可见性检查，也不能替秘密级投票。
    weak_admin = User(username="weak", display_name="越权管理员", dept="综合处",
                      max_level=1, role="管理员", roles=("reviewer", "admin"))
    payload = SimpleNamespace(
        tables=[], apply_template=False,
        meta=SimpleNamespace(title=None, dept=None, classification=None,
                             effective_date=None))
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with pytest.raises(Forbidden):
            review_service.confirm(conn, version_id, payload, user=weak_admin)
    assert fetch_all(db_url, "SELECT count(*) AS n FROM review_approvals")[0]["n"] == 0


def test_downgrade_audit_records_before_and_after(api, tmp_path, monkeypatch):
    from app import server

    make, db_url = api
    audit_file = tmp_path / "reclassify-audit.jsonl"
    monkeypatch.setattr(server, "AUDIT_FILE", audit_file)
    doc_id, version_id = seed(db_url, classification=2, structure=False)

    assert make("wang").post(f"/api/versions/{version_id}/confirm",
                             json={"meta": {"classification": 1}}).status_code == 200
    assert make("chen").post(f"/api/versions/{version_id}/approve",
                             json={"decision": "approve"}).status_code == 200

    lines = [json.loads(line) for line in
             audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 同一文件里还有登录审计（只有 event、没有 action），必须用 .get 过滤。
    events = [event for event in lines if event.get("action") == "review_submit"]
    assert len(events) == 1
    submit = events[0]
    assert submit["detail"]["previous_classification"] == 2
    assert submit["detail"]["reclassified"] is None

    approvals = [event for event in lines if event.get("action") == "review_approve"]
    assert len(approvals) == 1
    approve = approvals[0]
    assert approve["classification"] == 1
    assert approve["detail"]["previous_classification"] == 2
    assert approve["detail"]["reclassified"] == {
        "classification": {"from": 2, "to": 1},
        "dept": {"from": "财务处", "to": "财务处"},
    }


class _Embedder:
    model = "bge-m3"
    dim = DIM

    def embed(self, texts):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


def test_completed_reclassify_changes_retrieval_immediately(api, conn):
    """改密级「立即影响检索」（§10.1）：降密前低密级用户不可见，第二票后立即可见。"""
    from ingest.chunking import tokenize

    make, db_url = api
    doc_id, version_id = seed(db_url, classification=2, structure=False)
    content = "秘密预算 编制说明"
    tokens = " ".join(tokenize(content))
    conn.execute(
        """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type,
               content, content_tokens, tsv, embedding, embed_model, dept,
               classification, metadata, chunk_hash, active)
           VALUES (%s,%s,0,'text',%s,%s,to_tsvector('simple', %s),%s,'bge-m3',
                   '财务处',2,%s,%s,true)""",
        (doc_id, version_id, content, tokens, tokens,
         Vector([1.0] + [0.0] * (DIM - 1)),
         Jsonb({"chunk_id": f"{doc_id}@v1#0", "source": "预算.xlsx"}),
         uuid.uuid4().hex + uuid.uuid4().hex))
    conn.execute(
        "UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
        (version_id, doc_id))
    conn.commit()

    finance_l1 = User(username="zha", display_name="财务科员", dept="财务处",
                      max_level=1, role="工作人员", roles=("uploader",))
    finance_l2 = User(username="wang", display_name="王强", dept="财务处",
                      max_level=2, role="财务主管", roles=("reviewer", "admin"))

    blocked = retrieval.retrieve(conn, finance_l1, "预算", embedder=_Embedder())
    assert blocked.visible_chunks == 0
    assert blocked.reason == "no_visible_docs"
    assert retrieval.retrieve(conn, finance_l2, "预算",
                              embedder=_Embedder()).grounded is True

    assert make("wang").post(f"/api/versions/{version_id}/confirm",
                             json={"meta": {"classification": 1}}).status_code == 200
    assert make("chen").post(f"/api/versions/{version_id}/approve",
                             json={"decision": "approve"}).status_code == 200

    # 同一连接要看到别的事务写入的结果：先结束当前事务再检索。
    conn.commit()
    outcome = retrieval.retrieve(conn, finance_l1, "预算", embedder=_Embedder())
    assert outcome.grounded is True
    assert [h.classification for h in outcome.hits] == [1]
