from __future__ import annotations

import re
from dataclasses import replace

from ingest.chunking import CHINESE_SEPARATORS, chunk_document, tokenize
from ingest.config import get_settings
from ingest.ir import Block, Column, DocMeta, DocModel, ParserInfo, Table

DOC_ID = "11111111-1111-1111-1111-111111111111"


def _model(blocks, *, title="差旅费管理办法", parser="docx") -> DocModel:
    return DocModel(
        parser=ParserInfo(name=parser, version="0.1.0"),
        meta=DocMeta(title=title, source_filename=f"{title}.docx", sha256="a" * 64),
        blocks=blocks,
    )


def _chunk(ir, **over):
    kwargs = dict(doc_id=DOC_ID, version_no=2, title=ir.meta.title, dept="综合处",
                  classification=1, source_filename=ir.meta.source_filename)
    kwargs.update(over)
    return chunk_document(ir, **kwargs)


def _text_blocks():
    return [
        Block(block_id="b1", type="heading", level=1, text="第一章 总则"),
        Block(block_id="b2", type="paragraph", text="差旅住宿费标准按照职级执行。" * 30),
        Block(block_id="b3", type="heading", level=2, text="第一节 住宿"),
        Block(block_id="b4", type="paragraph", text="住宿费凭发票报销。"),
        Block(block_id="b5", type="heading", level=2, text="第二节 交通"),
        Block(block_id="b6", type="paragraph", text="交通费按实际发生额报销，不得重复报销。"),
    ]


def _budget_table() -> Table:
    rows = [[f"部门{i}", str(i * 10), str(i * 10 + 1)] for i in range(1, 23)]
    rows.append(["合计", "2530", "2552"])
    return Table(
        table_key="t1", sheet="2025预算", range="A1:C24",
        header_rows=[1],
        header_values=[["部门", "Q1预算", "Q2预算"]],
        columns=[Column(key="c1", name="部门"),
                 Column(key="c2", name="Q1预算", unit="万元"),
                 Column(key="c3", name="Q2预算", unit="万元")],
        rows=rows, footer_rows=[24], confidence=0.9, source="heuristic",
    )


def test_chinese_separators_match_main():
    from main import CHINESE_SEPARATORS as MAIN_SEPARATORS

    assert CHINESE_SEPARATORS == MAIN_SEPARATORS


def test_text_chunks_carry_title_and_heading_path():
    ir = _model(_text_blocks())
    chunks = _chunk(ir)
    assert chunks
    assert all(c.chunk_type == "text" for c in chunks)
    assert all(c.content.startswith("《差旅费管理办法》 > 第一章 总则") for c in chunks)
    assert all(len(c.content) <= 400 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    first = chunks[0]
    assert first.metadata["heading_path"] == ["第一章 总则"]
    assert first.metadata["chunk_id"] == f"{DOC_ID}@v2#0"
    assert re.fullmatch(r"[0-9a-f-]{36}@v\d+#\d+", first.metadata["chunk_id"])
    assert first.metadata["title"] == "差旅费管理办法"
    assert first.metadata["dept"] == "综合处"
    assert first.metadata["classification"] == 1
    assert first.metadata["parser"] == "docx@0.1.0"
    assert "差旅" in first.content_tokens
    assert first.content_tokens


def test_short_sections_merge_forward():
    chunks = _chunk(_model(_text_blocks()))
    joined = "\n".join(c.content for c in chunks)
    assert "住宿费凭发票报销。" in joined
    assert "交通费按实际发生额报销" in joined
    assert any("住宿费凭发票报销。" in c.content
               and "交通费按实际发生额报销" in c.content for c in chunks)
    assert all(len(c.content) >= 40 for c in chunks)


def test_tokenize_matches_bm25_conventions():
    tokens = tokenize("部门预算 Department 2025")
    assert "部门" in tokens
    assert "department" in tokens
    assert "2025" in tokens
    assert all(token == token.strip() and token for token in tokens)


def test_table_summary_and_row_windows():
    ir = _model([
        Block(block_id="b1", type="paragraph", text="2025 年预算如下。"),
        Block(block_id="b2", type="table", table=_budget_table()),
    ], title="2025年部门预算表", parser="xlsx")
    chunks = _chunk(ir, classification=2)
    summary = next(c for c in chunks if c.chunk_type == "table_summary")
    assert summary.metadata["sheet"] == "2025预算"
    assert summary.metadata["table_key"] == "t1"
    assert summary.metadata["cell_range"] == "A1:C24"
    assert summary.metadata["classification"] == 2
    assert "工作表「2025预算」" in summary.content
    assert "区域 A1:C24" in summary.content
    assert "共 22 行 × 3 列" in summary.content
    assert "列：部门 | Q1预算（万元） | Q2预算（万元）" in summary.content
    assert "合计：合计 | 2530 | 2552（第 24 行）" in summary.content
    assert "预算" in summary.content_tokens

    windows = [c for c in chunks if c.chunk_type == "table_rows"]
    assert len(windows) == 3
    assert [c.metadata["row_range"] for c in windows] == ["2-11", "12-21", "22-23"]
    for window in windows:
        assert "列：部门 | Q1预算（万元） | Q2预算（万元）" in window.content
        assert "2530" not in window.content  # 合计行不进窗口
    assert "部门1 | 10 | 11" in windows[0].content
    assert "部门22" in windows[2].content


def test_wide_table_splits_into_column_groups():
    names = [f"列{i}" for i in range(1, 16)]
    table = Table(
        table_key="t1", sheet="宽表", range="A1:O3",
        header_rows=[1], header_values=[names],
        columns=[Column(key=f"c{i}", name=f"列{i}") for i in range(1, 16)],
        rows=[["第1列"] + [f"第{i}列值" for i in range(2, 16)]],
        confidence=0.9, source="heuristic",
    )
    ir = _model([Block(block_id="b1", type="table", table=table)],
                title="宽表", parser="xlsx")
    windows = [c for c in _chunk(ir) if c.chunk_type == "table_rows"]
    assert len(windows) == 2
    assert windows[0].metadata["col_group"] == "1-10"
    assert windows[1].metadata["col_group"] == "1,11-15"
    assert "列：列1 | 列2 | 列3 | 列4 | 列5 | 列6 | 列7 | 列8 | 列9 | 列10" in windows[0].content
    assert "列1 | 列11 | 列12 | 列13 | 列14 | 列15" in windows[1].content
    assert "第15列值" not in windows[0].content
    assert "第15列值" in windows[1].content


def test_cell_truncation_marks_metadata():
    long_cell = "长" * 300
    table = Table(
        table_key="t1", sheet="S", range="A1:B2",
        header_rows=[1], header_values=[["名称", "内容"]],
        columns=[Column(key="c1", name="名称"), Column(key="c2", name="内容")],
        rows=[["甲", long_cell]], confidence=0.8, source="heuristic",
    )
    ir = _model([Block(block_id="b1", type="table", table=table)],
                title="截断测试", parser="xlsx")
    window = next(c for c in _chunk(ir) if c.chunk_type == "table_rows")
    assert window.metadata["truncated"] is True
    assert "长" * 200 in window.content
    assert "长" * 201 not in window.content


def test_large_table_is_sampled():
    rows = [[f"行{i}", str(i)] for i in range(1, 102)]
    table = Table(
        table_key="t1", sheet="S", range=f"A1:B{len(rows) + 1}",
        header_rows=[1], header_values=[["名称", "数值"]],
        columns=[Column(key="c1", name="名称"), Column(key="c2", name="数值")],
        rows=rows, confidence=0.8, source="heuristic",
    )
    ir = _model([Block(block_id="b1", type="table", table=table)],
                title="大表", parser="xlsx")
    settings = replace(get_settings(), max_rows_per_table=100)
    windows = [c for c in _chunk(ir, settings=settings)
               if c.chunk_type == "table_rows"]
    assert windows
    assert all(c.metadata.get("sampled") is True for c in windows)
    assert len(windows) <= 10
    assert "行1" in windows[0].content
    assert "行101" in windows[-1].content


def test_ordinals_and_hashes_are_stable():
    ir = _model([
        Block(block_id="b1", type="paragraph", text="正文内容。" * 30),
        Block(block_id="b2", type="table", table=_budget_table()),
    ], title="2025年部门预算表", parser="xlsx")
    first = _chunk(ir)
    second = _chunk(ir)
    assert [(c.ordinal, c.chunk_hash) for c in first] == \
        [(c.ordinal, c.chunk_hash) for c in second]
    types = [c.chunk_type for c in first]
    assert types.index("table_summary") < types.index("table_rows")


def test_pdf_table_keeps_page_and_bbox():
    table = _budget_table()
    table = table.model_copy(update={"page": 3, "bbox": [10.0, 20.0, 30.0, 40.0]})
    ir = _model([Block(block_id="b1", type="table", table=table)],
                title="扫描预算表", parser="pdf")
    summary = next(c for c in _chunk(ir) if c.chunk_type == "table_summary")
    assert summary.metadata["page"] == 3
    assert summary.metadata["bbox"] == [10.0, 20.0, 30.0, 40.0]


def _named_table(rows: list[list[str]]) -> Table:
    return Table(
        table_key="t1", sheet="S", range=f"A1:B{len(rows) + 1}",
        header_rows=[1], header_values=[["名称", "数值"]],
        columns=[Column(key="c1", name="名称"), Column(key="c2", name="数值")],
        rows=rows, confidence=0.8, source="heuristic",
    )


def _expand_runs(runs: str) -> set[int]:
    """把 `22-31、100-109` 展开成绝对行号集合，用来校验 row_range 与内容一致。"""
    rows: set[int] = set()
    for part in runs.split("、"):
        if "-" in part:
            low, high = part.split("-", 1)
            rows.update(range(int(low), int(high) + 1))
        else:
            rows.add(int(part))
    return rows


def _content_row_numbers(content: str) -> list[int]:
    """从窗口内容里反解实际出现的绝对行号（数据单元格为 `行N`）。"""
    lines = content.split("\n")
    header = next(i for i, line in enumerate(lines) if line.startswith("第 "))
    numbers: list[int] = []
    for line in lines[header + 1:]:
        first_cell = line.split(" | ")[0].strip()
        match = re.fullmatch(r"行(\d+)", first_cell)
        if match:
            numbers.append(int(match.group(1)) + 1)
    return numbers


def test_sampled_windows_report_non_contiguous_row_ranges():
    rows = [[f"行{i}", str(i)] for i in range(1, 102)]
    ir = _model([Block(block_id="b1", type="table", table=_named_table(rows))],
                title="大表", parser="xlsx")
    settings = replace(get_settings(), max_rows_per_table=50, table_rows_per_chunk=7)
    windows = [c for c in _chunk(ir, settings=settings)
               if c.chunk_type == "table_rows"]
    assert windows
    assert all(c.metadata.get("sampled") is True for c in windows)
    assert any("、" in c.metadata["row_range"] for c in windows)
    seen: list[int] = []
    for window in windows:
        runs = window.metadata["row_range"]
        # 内容头与 metadata 使用同一串；且必须等于窗口里真正出现的行号
        assert f"第 {runs} 行：" in window.content
        actual = _content_row_numbers(window.content)
        assert actual == sorted(actual)
        assert _expand_runs(runs) == set(actual)
        seen.extend(actual)
    assert len(seen) == len(set(seen))


def test_small_row_limit_sampling_dedupes_and_marks_sampled():
    rows = [[f"行{i}", str(i)] for i in range(1, 102)]
    ir = _model([Block(block_id="b1", type="table", table=_named_table(rows))],
                title="大表", parser="xlsx")
    settings = replace(get_settings(), max_rows_per_table=25)
    windows = [c for c in _chunk(ir, settings=settings)
               if c.chunk_type == "table_rows"]
    assert windows
    assert all(c.metadata.get("sampled") is True for c in windows)
    seen = [number for window in windows
            for number in _content_row_numbers(window.content)]
    assert len(seen) == len(set(seen))

    # 行数不足 head+tail 时直接返回全部行，不采样、不重复
    small_rows = [[f"行{i}", str(i)] for i in range(1, 36)]
    small_ir = _model([Block(block_id="b1", type="table",
                             table=_named_table(small_rows))],
                      title="小大表", parser="xlsx")
    small_settings = replace(get_settings(), max_rows_per_table=30)
    small_windows = [c for c in _chunk(small_ir, settings=small_settings)
                     if c.chunk_type == "table_rows"]
    small_seen = [number for window in small_windows
                  for number in _content_row_numbers(window.content)]
    assert sorted(small_seen) == list(range(2, 37))
    assert len(small_seen) == len(set(small_seen))
    assert all("sampled" not in c.metadata for c in small_windows)


def test_tiny_document_emits_chunk_instead_of_dropping():
    ir = _model([
        Block(block_id="b1", type="heading", level=1, text="第一章 总则"),
        Block(block_id="b2", type="paragraph", text="短。"),
    ])
    chunks = _chunk(ir)
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "text"
    assert "短。" in chunks[0].content


def test_repeated_header_rows_excluded_from_windows_and_summary():
    rows: list[list[str]] = []
    for i in range(1, 13):
        if i == 6:
            rows.append(["部门", "金额"])
        else:
            rows.append([f"部门{i}", str(i)])
    table = Table(
        table_key="t1", sheet="S", range="A1:B13",
        header_rows=[1], header_values=[["部门", "金额"]],
        columns=[Column(key="c1", name="部门"), Column(key="c2", name="金额")],
        rows=rows, repeated_header_rows=[7], confidence=0.9, source="heuristic",
    )
    ir = _model([Block(block_id="b1", type="table", table=table)],
                title="重复表头表", parser="xlsx")
    chunks = _chunk(ir)
    summary = next(c for c in chunks if c.chunk_type == "table_summary")
    assert "共 11 行 × 2 列" in summary.content
    windows = [c for c in chunks if c.chunk_type == "table_rows"]
    assert [c.metadata["row_range"] for c in windows] == ["2-6、8-12", "13"]
    for window in windows:
        assert "部门 | 金额" not in window.content.split("\n")
    joined = "\n".join(c.content for c in windows)
    assert "部门6" not in joined
    assert "部门12" in windows[-1].content


def test_chunk_size_limit_is_never_exceeded():
    blocks = [
        Block(block_id="b1", type="heading", level=1,
              text="第一章 总则与适用范围及基本原则及审批权限"),
        Block(block_id="b2", type="heading", level=2,
              text="第一节 住宿费报销标准与审批流程及发票要求"),
        Block(block_id="b3", type="paragraph",
              text="差旅住宿费标准按照职级执行。" * 30),
        Block(block_id="b4", type="paragraph", text="住宿费凭发票报销。"),
    ]
    ir = _model(blocks, title="2025年度差旅费管理办法与报销实施细则")
    settings = replace(get_settings(), chunk_size=120)
    chunks = _chunk(ir, settings=settings)
    assert len(chunks) > 1
    assert all(c.chunk_type == "text" for c in chunks)
    assert all(len(c.content) <= 120 for c in chunks)

    # 前缀比预算还长时截断前缀，仍保证每块总量不超预算
    tiny_ir = _model([
        Block(block_id="b1", type="heading", level=1,
              text="第一章 总则与适用范围及基本原则及审批权限"),
        Block(block_id="b2", type="heading", level=2,
              text="第一节 住宿费报销标准与审批流程及发票要求"),
        Block(block_id="b3", type="paragraph", text="住宿费凭发票报销。"),
    ], title="2025年度差旅费管理办法与报销实施细则")
    tiny_chunks = _chunk(tiny_ir, settings=replace(get_settings(), chunk_size=60))
    assert tiny_chunks
    assert all(len(c.content) <= 60 for c in tiny_chunks)


def test_short_pre_table_paragraph_joins_summary():
    ir = _model([
        Block(block_id="b1", type="heading", level=1, text="第一章 总则"),
        Block(block_id="b2", type="paragraph", text="见下表。"),
        Block(block_id="b3", type="table", table=_budget_table()),
    ], title="2025年部门预算表", parser="xlsx")
    chunks = _chunk(ir)
    assert not [c for c in chunks if c.chunk_type == "text"]
    summary = next(c for c in chunks if c.chunk_type == "table_summary")
    lines = summary.content.split("\n")
    assert lines[0].startswith("《2025年部门预算表》 > 第一章 总则")
    assert lines[1] == "见下表。"
    assert "工作表「2025预算」" in summary.content
    assert "区域 A1:C24" in summary.content
    assert "共 22 行 × 3 列" in summary.content
    assert "列：部门 | Q1预算（万元） | Q2预算（万元）" in summary.content
    assert "合计：合计 | 2530 | 2552（第 24 行）" in summary.content
