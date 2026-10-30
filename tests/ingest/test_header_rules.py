from ingest import config
from ingest.parsers import header_rules as hr
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows: list[list], bold_rows: tuple[int, ...] = ()) -> SheetGrid:
    width = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v) if v not in (None, "") else "",
                    bold=(r + 1) in bold_rows) for v in row]
            for r, row in enumerate(padded)],
        merges=[],
    )


def detect(rows, bold_rows=()):
    sheet = grid(rows, bold_rows)
    region = Region(1, len(rows), 1, sheet.ncols, 1.0)
    return hr.detect_header(sheet, region, config.get_settings())


def test_single_row_header_is_accepted():
    decision = detect([
        ["序号", "部门", "金额"],
        ["1", "综合处", "120"],
        ["2", "人事处", "98"],
        ["3", "财务处", "150"],
    ], bold_rows=(1,))
    assert decision.accepted is True
    assert decision.candidate.start_row == 1
    assert decision.candidate.depth == 1
    assert decision.candidate.confidence >= 0.75


def test_double_header_with_merged_parent():
    decision = detect([
        ["", "预算", "预算"],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["人事处", "98", "101"],
    ], bold_rows=(1, 2))
    assert decision.accepted is True
    assert decision.candidate.depth == 2
    assert hr.compose_header_names(
        grid([["", "预算", "预算"], ["部门", "Q1", "Q2"]]),
        Region(1, 2, 1, 3, 1.0), 1, 2,
    ) == ["部门", "预算/Q1", "预算/Q2"]


def test_numeric_only_block_has_no_candidate():
    decision = detect([
        ["1", "120", "130"],
        ["2", "98", "101"],
        ["3", "150", "160"],
    ])
    assert decision.accepted is False
    assert decision.candidate is None
    assert decision.reason == "no_candidates"


def test_duplicate_header_is_penalized():
    decision = detect([
        ["金额", "金额", "金额"],
        ["120", "130", "140"],
        ["98", "101", "105"],
    ], bold_rows=(1,))
    assert decision.accepted is False


def test_margin_requires_lead(monkeypatch):
    # margin 用于判断"表头起始行是否唯一"：两个不同的起始行分数接近时必须拒绝。
    candidates = [
        hr.HeaderCandidate(1, 1, 0.90, {}, ()),
        hr.HeaderCandidate(2, 1, 0.85, {}, ()),
    ]
    monkeypatch.setattr(hr, "generate_candidates", lambda *a, **k: candidates)
    sheet = grid([["部门", "金额"], ["综合处", "120"], ["人事处", "98"]])
    decision = hr.detect_header(sheet, Region(1, 3, 1, 2, 1.0), config.get_settings())
    assert decision.accepted is False
    assert decision.reason == "margin_too_small"


def test_same_start_depth_variants_do_not_trigger_margin(monkeypatch):
    # 同一 start_row 的 depth=1/2 是同一个假设：不能因为"两行版本分数略低"
    # 就否掉正确表头（真实 PDF 里这会导致整页表格退化成 列1..列N）。
    candidates = [
        hr.HeaderCandidate(1, 1, 0.80, {}, ()),
        hr.HeaderCandidate(1, 2, 0.69, {}, ()),
    ]
    monkeypatch.setattr(hr, "generate_candidates", lambda *a, **k: candidates)
    sheet = grid([["序号", "部门"], ["1", "综合处"], ["2", "人事处"]])
    decision = hr.detect_header(sheet, Region(1, 3, 1, 2, 1.0), config.get_settings())
    assert decision.accepted is True
    assert decision.candidate.depth == 1


def test_header_above_data_is_penalized():
    sheet = grid([
        ["1", "120", "130"],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
    ], bold_rows=(2,))
    scored = hr.score_candidate(sheet, Region(1, 3, 1, 3, 1.0), 2, 1)
    assert "data_above" in scored.penalties


def test_numeric_year_row_extends_two_row_header():
    # 年份标签是合法的数字型表头：继续行 66.7% 数字，但含关键词“部门”
    sheet = grid([
        ["", "预算", "预算"],
        ["部门", "2024", "2025"],
        ["综合处", "120", "130"],
    ], bold_rows=(1, 2))
    decision = hr.detect_header(sheet, Region(1, 3, 1, 3, 1.0), config.get_settings())
    assert decision.accepted is True
    assert decision.candidate.start_row == 1
    assert decision.candidate.depth == 2
    assert decision.candidate.confidence >= 0.75


def test_sequence_column_extension_is_penalized():
    # 首列是"序号"，下一行首格是纯数字：这是数据行，不能并入表头。
    sheet = grid([
        ["序号", "部门", "状态"],
        ["15", "科技部门", "在用"],
        ["16", "教育部门", "在用"],
    ])
    scored = hr.score_candidate(sheet, Region(1, 3, 1, 3, 1.0), 1, 2)
    assert "extension_looks_like_data" in scored.penalties


def test_numeric_month_row_with_keyword_extends_two_row_header():
    # 月份标签同理：继续行后两列全是数字，靠关键词“部门”豁免
    sheet = grid([
        ["", "支出", "支出"],
        ["部门", "1", "2"],
        ["综合处", "120", "130"],
        ["人事处", "98", "101"],
    ], bold_rows=(1, 2))
    decision = hr.detect_header(sheet, Region(1, 4, 1, 3, 1.0), config.get_settings())
    assert decision.accepted is True
    assert decision.candidate.start_row == 1
    assert decision.candidate.depth == 2
