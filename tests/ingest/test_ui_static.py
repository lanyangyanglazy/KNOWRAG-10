from __future__ import annotations

from fastapi.testclient import TestClient

from app import server


def client() -> TestClient:
    return TestClient(server.app)


def test_index_exposes_ingest_view_and_navigation():
    html = client().get("/").text
    for element_id in ("ingest-view", "open-ingest-btn", "back-chat-btn",
                       "ingest-upload-form", "ingest-library-body",
                       "ingest-review-panel", "ingest-source-panel"):
        assert f'id="{element_id}"' in html
    assert 'href="/static/ingest.css"' in html
    assert 'src="/static/ingest.js"' in html


def test_static_assets_are_served_locally():
    c = client()
    js = c.get("/static/ingest.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "window.IngestUI" in js.text

    css = c.get("/static/ingest.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert ".ingest-grid-table" in css.text


def test_assets_have_no_external_origins():
    js = client().get("/static/ingest.js").text.lower()
    css = client().get("/static/ingest.css").text.lower()
    for text in (js, css):
        assert "http://" not in text
        assert "https://" not in text
        assert "cdn." not in text


def test_citations_carry_chunk_id_for_source_lookup():
    html = client().get("/").text
    assert 'data-chunk-id="${escapeHtml(it.chunk_id' in html
    js = client().get("/static/ingest.js").text
    assert '.cite-item[data-chunk-id]' in js
    assert "/source" in js


def test_ingest_js_calls_documented_endpoints():
    js = client().get("/static/ingest.js").text
    for fragment in ("/api/documents", "/api/jobs/", "/api/versions/",
                     "/api/chunks/", "/api/me"):
        assert fragment in js


def test_ingest_controls_have_accessible_names():
    html = client().get("/").text
    js = client().get("/static/ingest.js").text
    assert 'aria-label="解析进度"' in html
    assert 'aria-describedby="ingest-job-status"' in html
    assert 'aria-label="waive 理由"' in js
    assert 'for="ingest-waive-reason"' in js
