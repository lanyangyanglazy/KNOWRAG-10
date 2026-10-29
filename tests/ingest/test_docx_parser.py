from unittest.mock import patch

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from ingest.convert import ConversionError
from ingest.parsers.base import ParseError
from ingest.parsers.docx_parser import DocParser, DocxParser
from ingest.parsers.table_build import build_table as real_build_table
from tests.ingest.builders import build_docx


def test_parses_headings_paragraphs_and_table(tmp_path):
    path = build_docx(
        tmp_path / "a.docx",
        paragraphs=[
            ("Heading 1", "二、预算管理"),
            (None, "各部门按季度编制预算。"),
            ("Heading 2", "（一）编制口径"),
            (None, "预算金额以万元为单位。"),
        ],
        table=[
            ["部门", "Q1预算（万元）", "Q2预算（万元）"],
            ["综合处", "120", "130"],
            ["人事处", "98", "101"],
        ],
    )
    model = DocxParser().parse(path)
    blocks = model.blocks
    assert [b.type for b in blocks] == ["heading", "paragraph", "heading", "paragraph", "table"]
    assert blocks[0].level == 1
    assert blocks[2].path == ["二、预算管理", "（一）编制口径"]
    table = model.tables()[0]
    assert [c.name for c in table.columns] == ["部门", "Q1预算", "Q2预算"]
    assert table.columns[1].unit == "万元"
    assert table.confidence >= 0.75
    assert blocks[4].path == ["二、预算管理", "（一）编制口径"]


def test_merged_cell_table(tmp_path):
    path = tmp_path / "merged.docx"
    doc = Document()
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).merge(table.cell(0, 2))
    values = [["预算", "", ""], ["部门", "Q1", "Q2"], ["综合处", "120", "130"]]
    for r, row in enumerate(values):
        for c, value in enumerate(row):
            # (0,0)-(0,2) 合并后三格指向同一个 tc，给子格赋空串会清掉父格文本，
            # 因此合并区只在左上角写一次值。
            if r == 0 and c > 0:
                continue
            table.cell(r, c).text = value
    doc.save(path)

    model = DocxParser().parse(path)
    assert len(model.tables()) == 1
    names = [c.name for c in model.tables()[0].columns]
    assert names == ["预算/部门", "预算/Q1", "预算/Q2"]


def test_textbox_content_is_captured(tmp_path):
    path = tmp_path / "textbox.docx"
    doc = Document()
    doc.add_paragraph("正文段落")
    xml = (
        f'<w:p {nsdecls("w")}><w:r><w:t>另一段</w:t></w:r>'
        '<w:r><w:pict xmlns:v="urn:schemas-microsoft-com:vml"><v:shape>'
        '<v:textbox><w:txbxContent><w:p><w:r><w:t>文本框内容</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>'
    )
    doc.element.body.append(parse_xml(xml))
    doc.save(path)

    model = DocxParser().parse(path)
    texts = [b.text for b in model.blocks]
    assert any("文本框内容" in t for t in texts)


def test_empty_docx_is_flagged(tmp_path):
    path = tmp_path / "empty.docx"
    Document().save(path)
    model = DocxParser().parse(path)
    assert model.blocks == []
    assert any(w.code == "empty_document" for w in model.warnings)


def test_doc_conversion_failure_is_parse_error(tmp_path):
    path = tmp_path / "a.doc"
    path.write_bytes(b"fake")
    with patch("ingest.parsers.docx_parser.convert_document",
               side_effect=ConversionError("no soffice")):
        with pytest.raises(ParseError) as exc:
            DocParser().parse(path)
    assert "soffice" in str(exc.value)


def test_revision_is_warned(tmp_path):
    path = tmp_path / "rev.docx"
    doc = Document()
    doc.add_paragraph("正文")
    xml = (f'<w:p {nsdecls("w")}><w:ins w:id="1" w:author="a" w:date="2026-01-01T00:00:00Z">'
           '<w:r><w:t>插入</w:t></w:r></w:ins></w:p>')
    doc.element.body.append(parse_xml(xml))
    doc.save(path)
    model = DocxParser().parse(path)
    assert any(w.code == "revision_accepted" for w in model.warnings)


def test_table_failure_is_isolated(tmp_path):
    # 裁决 4：单表构建失败只记 parse_partial，后续表格继续解析。
    path = tmp_path / "two_tables.docx"
    doc = Document()
    for _ in range(2):
        table = doc.add_table(rows=2, cols=2)
        for r, row in enumerate([["部门", "金额"], ["综合处", "120"]]):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
    doc.save(path)

    calls = {"count": 0}

    def flaky(sheet, region, header, *, table_key, settings, forced_columns=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("boom")
        return real_build_table(sheet, region, header, table_key=table_key,
                                settings=settings, forced_columns=forced_columns)

    with patch("ingest.parsers.docx_parser.build_table", side_effect=flaky):
        model = DocxParser().parse(path)

    assert len(model.tables()) == 1
    partial = [w for w in model.warnings if w.code == "parse_partial"]
    assert len(partial) == 1
    assert "boom" in partial[0].detail
