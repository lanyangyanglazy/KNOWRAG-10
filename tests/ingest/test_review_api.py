from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row

from ingest import repository as repo
from ingest.ir import Block, Column, DocMeta, DocModel, ParserInfo, Table, Warning
from tests.ingest.conftest import fetch_all


def build_ir() -> DocModel:
    table = Table(
        table_key="t1", sheet="2025预算", range="A1:B4",
        header_rows=[2], header_values=[["部门", "预算"]],
        pre_rows=[["2025年部门预算表", ""]],
        columns=[Column(key="c1", name="部门"), Column(key="c2", name="预算")],
        rows=[["综合处", "100"], ["人事处", "90"]],
        confidence=0.6, source="heuristic",
        candidates=[{"start_row": 2, "depth": 1, "confidence": 0.6}],
        flags=["merged_cells"],
    )
    return DocModel(
        parser=ParserInfo(name="xlsx", version="0.1.0"),
        meta=DocMeta(title="2025年部门预算表", source_filename="预算.xlsx",
                     sha256="b" * 64),
        blocks=[Block(block_id="b1", type="table", table=table)],
        warnings=[Warning(code="merged_cells", detail="1 个合并区间")],
    )


def seed(db_url: str, *, classification: int | None = 1,
         structure: bool = True, dept: str = "财务处") -> tuple[str, str]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        doc = repo.create_document(conn, title="2025年部门预算表", dept=dept,
                                   classification=classification,
                                   effective_date=None, created_by="wang")
        version = repo.create_version(
            conn, document_id=doc["id"], version_no=1, sha256="b" * 64,
            filename="预算.xlsx", mime="application/octet-stream", size_bytes=10,
            storage_path="bb/" + "b" * 64, parser="xlsx", parser_version="0.1.0")
        repo.set_version_status(conn, version["id"], "needs_review", ir=build_ir(),
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
    _, version_id = seed(db_url)
    client = make("wang")

    resp = client.post(f"/api/versions/{version_id}/confirm", json={
        "tables": [{
            "table_key": "t1",
            "header_rows": [2],
            "columns": [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "预算"}],
        }],
        "apply_template": True,
    })
    assert resp.status_code == 200, resp.text

    lines = [json.loads(line) for line in
             audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    applied = [event for event in lines if event.get("action") == "template_apply"]
    assert len(applied) == 1
    assert isinstance(applied[0]["version_id"], str)
    assert applied[0]["version_id"] == version_id
    assert applied[0]["detail"]["table_keys"] == ["t1"]
