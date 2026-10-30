from ingest.config import get_settings


def test_defaults_match_design(monkeypatch):
    for key in ("INGEST_DB_URL", "INGEST_MAX_FILE_MB", "EMBED_DIM", "DENSE_TOP",
                "HEADER_ACCEPT", "HEADER_LLM", "INGEST_QUEUE_MAX"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.db_url == ""
    assert s.max_file_mb == 50
    assert s.max_pdf_pages == 2000
    assert s.max_rows_per_table == 5000
    assert s.max_cells_per_sheet == 200_000
    assert s.chunk_size == 400 and s.chunk_overlap == 60
    assert s.embed_dim == 1024
    assert s.dense_top == 50 and s.sparse_top == 50 and s.fusion_top == 5
    assert s.header_accept == 0.75 and s.header_margin == 0.12
    assert s.header_llm == "auto"
    assert s.header_llm_enabled is False  # auto 在门禁前等同 off
    assert s.queue_max == 200


def test_env_override(monkeypatch):
    monkeypatch.setenv("INGEST_MAX_FILE_MB", "8")
    monkeypatch.setenv("HEADER_LLM", "always")
    get_settings.cache_clear()
    s = get_settings()
    assert s.max_file_mb == 8
    assert s.header_llm_enabled is True
