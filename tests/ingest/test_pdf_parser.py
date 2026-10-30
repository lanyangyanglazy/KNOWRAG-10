from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ingest.config import Settings, get_settings
from ingest.errors import TooLarge
from ingest.parsers.base import ParseError
from ingest.parsers.pdf_parser import PdfParser
from ingest.parsers.table_build import build_table as real_build_table


def _text_pdf(path, pages=2):
    import pymupdf as fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 60), "内部资料", fontname="china-s", fontsize=9)
        page.insert_text((72, 100), f"第 {i + 1} 页 政务服务中心办公时间", fontname="china-s", fontsize=14)
        page.insert_text((72, 130), "工作日 8:30 至 17:00。", fontname="china-s", fontsize=10)
        page.insert_text((72, 800), f"第 {i + 1} 页", fontname="china-s", fontsize=9)
    doc.save(path)
    doc.close()


def _draw_table(page, top, rows, *, x0=72, col_width=100, row_height=30):
    x1 = x0 + col_width * len(rows[0])
    y1 = top + row_height * len(rows)
    for c in range(len(rows[0]) + 1):
        page.draw_line((x0 + c * col_width, top), (x0 + c * col_width, y1))
    for r in range(len(rows) + 1):
        page.draw_line((x0, top + r * row_height), (x1, top + r * row_height))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            page.insert_text((x0 + 8 + c * col_width, top + 20 + r * row_height),
                             value, fontname="helv", fontsize=10)


def _tables_pdf(path, specs):
    """specs: [(top_y, rows)]，每张表按行列线绘制并写入文本。"""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    for top, rows in specs:
        _draw_table(page, top, rows)
    doc.save(path)
    doc.close()


def _table_pdf(path, rows, *, top=100):
    _tables_pdf(path, [(top, rows)])


def _paged_table_pdf(path, pages):
    """每页一张同坐标表格，用于验证跨页续表的表头继承。"""
    import pymupdf as fitz

    doc = fitz.open()
    for rows in pages:
        page = doc.new_page()
        _draw_table(page, 100, rows)
    doc.save(path)
    doc.close()


def _template(columns_norm, names, *, relative_header_row=0):
    return {
        "id": "t1", "name": "测试模板", "scope_dept": None,
        "signature": {
            "ncols": len(columns_norm), "header_depth": 1,
            "relative_header_row": relative_header_row,
            "columns_norm": list(columns_norm),
            "sheet_pattern": None, "sheet_name": None,
        },
        "config": {"columns": [
            {"key": f"c{i + 1}", "name": name, "dtype": "string"}
            for i, name in enumerate(names)
        ]},
    }


class _CountingLLM:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content="无法识别")


def test_extracts_text_bbox_and_removes_repeated_header(tmp_path):
    path = tmp_path / "a.pdf"
    _text_pdf(path)
    model = PdfParser().parse(path)
    texts = [b.text for b in model.blocks]
    assert any("办公时间" in t for t in texts)
    assert "内部资料" not in texts
    assert all(b.page in (1, 2) for b in model.blocks)
    assert all(b.bbox for b in model.blocks)


def test_scanned_pdf_is_flagged(tmp_path):
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    # pymupdf 2.x 里 alpha 是位置参数（关键字被拒绝），语义仍是 alpha=False
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    page.insert_image(fitz.Rect(72, 72, 272, 272), pixmap=pix)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()

    model = PdfParser().parse(path)
    assert any(w.code == "scanned_pdf" for w in model.warnings)


def test_page_limit_is_enforced(tmp_path):
    path = tmp_path / "a.pdf"
    _text_pdf(path, pages=1)
    settings = replace(get_settings(), max_pdf_pages=0)
    with pytest.raises(TooLarge):
        PdfParser(settings=settings).parse(path)


def test_corrupt_pdf_raises_parse_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nbroken\n%%EOF")
    with pytest.raises(ParseError):
        PdfParser().parse(path)


def test_table_is_extracted_with_bbox(tmp_path):
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    x0, y0, x1, y1 = 72, 100, 372, 200
    for x in (x0, x0 + 100, x0 + 200, x1):
        page.draw_line((x, y0), (x, y1))
    for y in (y0, y0 + 40, y0 + 70, y1):
        page.draw_line((x0, y), (x1, y))
    rows = [["Dept", "Q1", "Q2"], ["A", "120", "130"], ["B", "98", "101"]]
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            page.insert_text((x0 + 8 + c * 100, y0 + 25 + r * 30), value,
                             fontname="helv", fontsize=10)
    path = tmp_path / "table.pdf"
    doc.save(path)
    doc.close()

    model = PdfParser().parse(path)
    tables = model.tables()
    assert len(tables) >= 1
    assert tables[0].page == 1
    assert tables[0].bbox
    assert len(tables[0].columns) == 3


def test_rotated_page_bboxes_use_display_space(tmp_path):
    # 90 度旋转页：文本 bbox 与 pdfplumber 表格 bbox 必须统一到显示空间，
    # 否则表格单元格文本会重复成段落，且回源坐标不可比。
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    rows = [["Dept", "Q1", "Q2"], ["A", "120", "130"], ["B", "98", "101"]]
    _draw_table(page, 100, rows)
    page.insert_text((72, 50), "Report", fontname="helv", fontsize=14)
    page.set_rotation(90)
    path = tmp_path / "rotated.pdf"
    doc.save(path)
    doc.close()

    model = PdfParser().parse(path)
    tables = model.tables()
    assert len(tables) == 1
    assert tables[0].bbox[0] > 500  # 显示空间里表格落在页面右侧（约 x=652）

    cell_fragments = ("Dept", "Q1", "Q2", "120", "130", "98", "101")
    paragraphs = [b for b in model.blocks if b.type == "paragraph"]
    assert not any(fragment in b.text
                   for b in paragraphs for fragment in cell_fragments)

    # 旋转后显示区域为 842 x 595；所有块 bbox 都必须落在该区域内。
    for block in model.blocks:
        x0, y0, x1, y1 = block.bbox
        assert 0 <= x0 < x1 <= 842
        assert 0 <= y0 < y1 <= 595

    title = [b for b in model.blocks if b.text == "Report"]
    assert title
    # 未旋转空间下 x0 应为 72；归一化后约 788，只有转换过的块才会在右侧。
    assert title[0].bbox[0] > 700


def test_table_only_page_with_image_is_not_scanned(tmp_path):
    # 文本全部落在表格里时，单元格字符数也要计入扫描件判定。
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    rows = [["Department", "First Quarter", "Second Quarter"],
            ["Administration", "120", "130"],
            ["Finance", "98", "101"]]
    _draw_table(page, 100, rows)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    page.insert_image(fitz.Rect(420, 420, 440, 440), pixmap=pix)
    path = tmp_path / "table_with_image.pdf"
    doc.save(path)
    doc.close()

    model = PdfParser().parse(path)
    assert len(model.tables()) == 1
    assert not any(w.code == "scanned_pdf" for w in model.warnings)


def test_template_is_preferred_over_accepted_rules(tmp_path):
    # 裁决：模板 > 规则 > LLM。规则本可接受该表，模板命中后 source/confidence
    # 必须来自模板。
    path = tmp_path / "table.pdf"
    rows = [["Dept", "Q1", "Q2"], ["A", "120", "130"], ["B", "98", "101"]]
    _table_pdf(path, rows)
    plain = PdfParser().parse(path)
    assert plain.tables()[0].source == "heuristic"
    # 规则不仅“接受”了候选，确实把第 1 行定为表头，模板优先才有意义。
    assert plain.tables()[0].header_rows == [1]

    template = _template(["dept", "q1", "q2"], ["部门", "一季度", "二季度"])
    table = PdfParser(templates=[template]).parse(path).tables()[0]
    assert table.source == "template"
    assert table.confidence == 0.95
    assert [c.name for c in table.columns] == ["部门", "一季度", "二季度"]


def test_template_fallback_scan_finds_numeric_header(tmp_path):
    # 纯数字表头不产生规则候选，模板应通过区域顶部兜底扫描命中。
    path = tmp_path / "numeric.pdf"
    _table_pdf(path, [["2024", "2025"], ["1", "2"]])
    plain = PdfParser().parse(path)
    assert plain.tables()[0].source == "heuristic"
    assert any(w.code == "table_no_header" for w in plain.warnings)

    template = _template(["2024", "2025"], ["年份", "数量"])
    table = PdfParser(templates=[template]).parse(path).tables()[0]
    assert table.source == "template"
    assert [c.name for c in table.columns] == ["年份", "数量"]


def test_llm_header_calls_capped_per_document(tmp_path):
    # 裁决：每份文档最多 3 次 try_llm_header；第 4 张表不再调用。
    path = tmp_path / "numeric_tables.pdf"
    rows = [["2024", "2025"], ["1", "2"]]
    _tables_pdf(path, [(top, rows) for top in (60, 240, 420, 600)])

    llm = _CountingLLM()
    settings = Settings(db_url="", upload_dir=tmp_path, header_llm="always")
    model = PdfParser(llm=llm, settings=settings).parse(path)
    assert len(model.tables()) == 4
    assert llm.calls == 3
    assert sum(w.code == "ambiguous_header" for w in model.warnings) == 3


def test_continuation_page_inherits_header(tmp_path):
    # 长表跨页时，表头只在第一页出现；续页必须沿用第一页的列名，
    # 否则检测列名会退化成 列1..列N，检索与引用都失去语义。
    path = tmp_path / "continued.pdf"
    # 用 ASCII 文本：测试 PDF 使用 helv 字体，中文需要 china-s 才能正确嵌入/提取。
    header = ["No", "Dept", "Amount"]
    _paged_table_pdf(path, [
        [header, ["1", "HR", "120"], ["2", "Finance", "98"]],
        [["3", "Audit", "150"], ["4", "Office", "80"]],
        [["5", "Legal", "60"], ["6", "Admin", "70"]],
    ])

    model = PdfParser().parse(path)
    tables = model.tables()
    assert len(tables) == 3
    assert tables[0].header_rows == [1]
    assert [c.name for c in tables[0].columns] == header
    for continuation in tables[1:]:
        assert continuation.header_rows == []
        assert [c.name for c in continuation.columns] == header
        assert "header_inherited" in continuation.flags
    assert not any(w.code == "table_no_header" for w in model.warnings)


def test_table_failure_is_isolated(tmp_path):
    # 裁决：单表 build_table 失败只记 parse_partial，后续表格继续解析。
    path = tmp_path / "two_tables.pdf"
    rows = [["Dept", "Q1", "Q2"], ["A", "120", "130"], ["B", "98", "101"]]
    _tables_pdf(path, [(60, rows), (300, rows)])

    calls = {"count": 0}

    def flaky(sheet, region, header, *, table_key, settings, forced_columns=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("boom")
        return real_build_table(sheet, region, header, table_key=table_key,
                                settings=settings, forced_columns=forced_columns)

    with patch("ingest.parsers.pdf_parser.build_table", side_effect=flaky):
        model = PdfParser().parse(path)

    assert len(model.tables()) == 1
    partial = [w for w in model.warnings if w.code == "parse_partial"]
    assert len(partial) == 1
    assert "t1" in partial[0].detail
    assert "boom" in partial[0].detail
