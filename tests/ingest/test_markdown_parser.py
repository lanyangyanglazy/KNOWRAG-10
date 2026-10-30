from pathlib import Path

from ingest.parsers.markdown_parser import MarkdownParser, read_front_matter

CORPUS = Path(__file__).resolve().parents[2] / "data" / "gov_docs"


def test_reads_front_matter():
    meta, body = read_front_matter(CORPUS / "01_政务公开_办事指南.md")
    assert meta["doc_id"] == "gov-001"
    assert meta["classification"] == "公开"
    assert body.lstrip().startswith("#")


def test_parses_existing_corpus():
    model = MarkdownParser().parse(CORPUS / "01_政务公开_办事指南.md")
    assert model.meta.title == "政务公开与办事指南"
    assert model.meta.effective_date == "2026-01-01"
    assert model.meta.classification is None
    headings = [b.text for b in model.blocks if b.type == "heading"]
    assert "一、办公时间与地点" in headings
    assert any("8:30" in b.text for b in model.blocks if b.type == "paragraph")
    assert model.warnings == []


def test_all_corpus_documents_parse():
    models = [MarkdownParser().parse(p) for p in sorted(CORPUS.glob("*.md"))]
    assert len(models) == 4
    assert all(m.blocks for m in models)
