"""切片 profile：参数解析、结构感知切片、指纹隔离的回归测试。"""

from dataclasses import replace

import pytest

from app import rag_service as rs
from ingest.chunking import chunk_document
from ingest.config import get_settings
from ingest.ir import Block, DocMeta, DocModel, ParserInfo
from ingest.profiles import (
    DEFAULT_PROFILE_ID,
    PROFILES,
    resolve_profile,
    profile_from_settings,
)

DOC_ID = "11111111-1111-1111-1111-111111111111"


def _model(paragraph: str, *, title: str = "测试办法") -> DocModel:
    return DocModel(
        parser=ParserInfo(name="markdown", version="0.1.0"),
        meta=DocMeta(title=title, source_filename="a.md", sha256="a" * 64),
        blocks=[
            Block(block_id="b1", type="heading", level=1, text="第一章 总则"),
            Block(block_id="b2", type="paragraph", text=paragraph),
            Block(block_id="b3", type="heading", level=2, text="第一节 细则"),
            Block(block_id="b4", type="paragraph", text="短段落。"),
        ],
    )


def _chunk(paragraph: str, profile: str):
    return chunk_document(
        _model(paragraph),
        doc_id=DOC_ID, version_no=1, title="测试办法", dept="综合处",
        classification=1, source_filename="a.md", profile=profile,
    )


def test_default_profile_is_registered(monkeypatch):
    monkeypatch.delenv("RAG_CHUNK_PROFILE", raising=False)
    assert resolve_profile(None).profile_id == DEFAULT_PROFILE_ID
    assert DEFAULT_PROFILE_ID in PROFILES


def test_unknown_profile_fails_fast(monkeypatch):
    monkeypatch.delenv("RAG_CHUNK_PROFILE", raising=False)
    with pytest.raises(ValueError, match="未知切片 profile"):
        resolve_profile("no-such-profile")


def test_profile_controls_chunk_size():
    long_text = "差旅住宿费标准按照职级执行。" * 200
    small = _chunk(long_text, "p300")
    large = _chunk(long_text, "p600")
    assert max(len(c.content) for c in small) <= 300
    assert max(len(c.content) for c in large) <= 600
    assert len(small) >= len(large)


def test_title_prefix_can_be_disabled():
    long_text = "差旅住宿费标准按照职级执行。" * 40
    with_prefix = _chunk(long_text, "p400")
    without_prefix = _chunk(long_text, "p400-noprefix")
    assert with_prefix[0].content.startswith("《测试办法》 > 第一章 总则")
    assert not without_prefix[0].content.startswith("《测试办法》")


def test_chunk_metadata_records_profile_and_heading_path():
    chunks = _chunk("正文内容。" * 30, "p400")
    assert chunks
    for chunk in chunks:
        assert chunk.metadata["chunk_profile"] == "p400"
        assert isinstance(chunk.metadata["heading_path"], list)


def test_profile_from_settings_marks_env_override():
    settings = replace(get_settings(), chunk_size=120)
    profile = profile_from_settings(settings)
    assert profile.profile_id.endswith("+env")
    assert profile.chunk_size == 120


def test_faiss_corpus_signature_changes_with_profile(tmp_path):
    (tmp_path / "a.md").write_text(
        "---\ndoc_id: gov-999\ntitle: 测试\ndept: 综合处\nclassification: 公开\n---\n"
        "正文内容。\n",
        encoding="utf-8",
    )
    sig_400 = rs.corpus_signature(tmp_path, profile="p400")
    sig_600 = rs.corpus_signature(tmp_path, profile="p600")
    assert sig_400 != sig_600
    assert sig_400["chunk_profile"]["profile_id"] == "p400"
    assert sig_400["separators_sha256"]


def test_faiss_loader_keeps_contract_and_applies_profile():
    documents = rs.load_gov_documents(profile="p600")
    assert documents
    chunk_ids = set()
    for doc in documents:
        md = doc.metadata
        assert md["chunk_profile"] == "p600"
        assert len(doc.page_content) <= 600
        assert md["chunk_id"].startswith(f"{md['doc_id']}#")
        assert md["chunk_id"].split("#")[-1].isdigit()
        assert md["chunk_id"] not in chunk_ids
        chunk_ids.add(md["chunk_id"])
        assert md["classification"] in {"公开", "内部", "秘密"}
        assert md["classification_level"] in {0, 1, 2}
        assert "heading_path" in md
