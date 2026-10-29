import pytest

from ingest import config
from ingest.ir import Table
from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.regions import Region
from ingest.parsers.table_build import build_table, rebuild_table
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows, merges=None):
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v)) if v not in (None, "") else Cell() for v in row]
              for row in rows],
        merges=merges or [],
    )


_COLUMNS_3 = [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "Q1"},
              {"key": "c3", "name": "Q2"}]


def _table_for_rebuild():
    sheet = grid([
        ["2025年预算", "", ""],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["合计", "120", "130"],
    ])
    table, _ = build_table(
        sheet, Region(1, 4, 1, 3, 1.0), HeaderCandidate(2, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    return table


def test_builds_columns_types_units_and_footer():
    sheet = grid([
        ["部门", "金额（万元）", "日期"],
        ["综合处", "1,200", "2026-01-01"],
        ["人事处", "980", "2026-01-02"],
        ["合计", "2,180", ""],
    ])
    region = Region(1, 4, 1, 3, 1.0)
    header = HeaderCandidate(1, 1, 0.9, {}, ())
    table, warnings = build_table(sheet, region, header, table_key="t1",
                                  settings=config.get_settings())
    assert [c.name for c in table.columns] == ["部门", "金额", "日期"]
    assert table.columns[1].unit == "万元"
    assert [c.dtype for c in table.columns] == ["string", "number", "date"]
    assert table.footer_rows == [4]
    assert table.data_rows() == [["综合处", "1,200", "2026-01-01"],
                                 ["人事处", "980", "2026-01-02"]]
    assert table.range == "A1:C4"
    assert table.header_values == [["部门", "金额（万元）", "日期"]]
    assert any(w.code == "merged_cells" for w in warnings) is False


def test_repeated_header_is_dropped():
    sheet = grid([
        ["部门", "金额"],
        ["综合处", "120"],
        ["部门", "金额"],
        ["人事处", "98"],
    ])
    table, warnings = build_table(
        sheet, Region(1, 4, 1, 2, 1.0), HeaderCandidate(1, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    assert table.data_rows() == [["综合处", "120"], ["人事处", "98"]]
    assert any(w.code == "repeated_header" for w in warnings)


def test_no_header_falls_back_to_column_names():
    sheet = grid([["1", "120"], ["2", "98"]])
    table, warnings = build_table(
        sheet, Region(1, 2, 1, 2, 1.0), None,
        table_key="t1", settings=config.get_settings())
    assert [c.name for c in table.columns] == ["列1", "列2"]
    assert table.header_rows == []
    assert table.confidence == 0.0
    assert any(w.code == "table_no_header" for w in warnings)


def test_forced_template_columns_override_names():
    sheet = grid([["部门", "Q1"], ["综合处", "120"]])
    table, _ = build_table(
        sheet, Region(1, 2, 1, 2, 1.0), HeaderCandidate(1, 1, 0.95, {}, (), "template"),
        table_key="t1", settings=config.get_settings(),
        forced_columns=[{"key": "c1", "name": "部门", "dtype": "string"},
                        {"key": "c2", "name": "一季度", "dtype": "number", "unit": "万元"}])
    assert [c.name for c in table.columns] == ["部门", "一季度"]
    assert table.source == "template"


def test_rebuild_with_new_header_rows():
    sheet = grid([
        ["2025年预算", "", ""],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["合计", "120", "130"],
    ])
    table, _ = build_table(
        sheet, Region(1, 4, 1, 3, 1.0), HeaderCandidate(2, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    columns = [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "Q1"},
               {"key": "c3", "name": "Q2"}]
    rebuilt, _ = rebuild_table(table, [2, 3], columns, settings=config.get_settings())
    assert rebuilt.header_rows == [2, 3]
    assert rebuilt.header_values == [["部门", "Q1", "Q2"], ["综合处", "120", "130"]]
    assert rebuilt.source == "human"


def test_repeated_header_keeps_absolute_rows_with_footer():
    # 裁决 1：重复表头不得从 rows 里删除，否则后续行的绝对行号整体前移，
    # footer 与 full_grid 全部错位。重复行仅记录在 repeated_header_rows。
    sheet = grid([
        ["部门", "金额"],
        ["综合处", "120"],
        ["部门", "金额"],
        ["人事处", "98"],
        ["合计", "218"],
    ])
    table, warnings = build_table(
        sheet, Region(1, 5, 1, 2, 1.0), HeaderCandidate(1, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    assert table.repeated_header_rows == [3]
    assert table.rows[1] == ["部门", "金额"]  # 重复行仍保留在 rows 中
    assert table.data_rows() == [["综合处", "120"], ["人事处", "98"]]
    assert table.footer_rows == [5]
    assert table.footer_values() == [["合计", "218"]]
    assert table.full_grid() == [
        ["部门", "金额"], ["综合处", "120"], ["部门", "金额"],
        ["人事处", "98"], ["合计", "218"],
    ]
    assert any(w.code == "repeated_header" for w in warnings)


def test_rebuild_recomputes_repeated_header_rows():
    # 裁决 1：rebuild 必须按新表头最后一行重算重复标记，不能沿用已经落进
    # 新表头范围内的旧标记（否则 validator 会拒绝新表）。
    sheet = grid([
        ["部门", "金额"],
        ["综合处", "120"],
        ["部门", "金额"],
        ["人事处", "98"],
    ])
    table, _ = build_table(
        sheet, Region(1, 4, 1, 2, 1.0), HeaderCandidate(1, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    assert table.repeated_header_rows == [3]
    rebuilt, _ = rebuild_table(
        table, [1, 2, 3],
        [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "金额"}],
        settings=config.get_settings())
    assert rebuilt.header_rows == [1, 2, 3]
    assert rebuilt.repeated_header_rows == []
    assert "repeated_header" not in rebuilt.flags
    assert rebuilt.data_rows() == [["人事处", "98"]]


def test_two_level_repeated_header_block_is_excluded():
    # Minor 2：两行表头的重复块必须整体剔除；只比对最后一行会把父级行
    # （"预算"）留在数据视图里，且不能影响其余行的绝对行号。
    rows = [
        ["预算", "", ""],          # 1 表头（父级，合并展开）
        ["部门", "Q1", "Q2"],      # 2 表头（叶子）
        ["综合处", "120", "130"],  # 3 数据
        ["预算", "", ""],          # 4 重复块
        ["部门", "Q1", "Q2"],      # 5 重复块
        ["人事处", "98", "101"],   # 6 数据
        ["合计", "218", "231"],    # 7 表尾
    ]
    table, warnings = build_table(
        grid(rows), Region(1, 7, 1, 3, 1.0), HeaderCandidate(1, 2, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    assert table.header_rows == [1, 2]
    assert table.repeated_header_rows == [4, 5]
    assert table.footer_rows == [7]
    assert table.rows[1:3] == [["预算", "", ""], ["部门", "Q1", "Q2"]]
    assert table.data_rows() == [["综合处", "120", "130"], ["人事处", "98", "101"]]
    assert table.footer_values() == [["合计", "218", "231"]]
    # 周围行与表尾的绝对行号编号不变
    assert table.full_grid() == rows
    data_start = table.data_start_row
    assert all(table.rows[i] == rows[data_start - 1 + i]
               for i in range(len(table.rows)))
    assert any(w.code == "repeated_header" for w in warnings)


def test_rebuild_rejects_non_contiguous_header_rows():
    # Important 1：[2, 4] 会让 data_start 与 rows[0] 错位，必须显式拒绝。
    with pytest.raises(ValueError, match="连续"):
        rebuild_table(_table_for_rebuild(), [2, 4], _COLUMNS_3,
                      settings=config.get_settings())


def test_rebuild_rejects_header_rows_out_of_range():
    with pytest.raises(ValueError, match="超出"):
        rebuild_table(_table_for_rebuild(), [5], _COLUMNS_3,
                      settings=config.get_settings())


def test_rebuild_rejects_column_count_mismatch():
    with pytest.raises(ValueError, match="列数"):
        rebuild_table(_table_for_rebuild(), [2], _COLUMNS_3[:2],
                      settings=config.get_settings())


def test_rebuild_twice_reslices_pre_rows():
    # 改表头前进一格再退回来：pre_rows/header_values/rows 必须按绝对行号重切。
    table = _table_for_rebuild()
    forward, _ = rebuild_table(table, [2, 3], _COLUMNS_3,
                               settings=config.get_settings())
    assert forward.pre_rows == [["2025年预算", "", ""]]
    assert forward.header_values == [["部门", "Q1", "Q2"], ["综合处", "120", "130"]]
    assert forward.rows == [["合计", "120", "130"]]
    backward, _ = rebuild_table(forward, [2], _COLUMNS_3,
                                settings=config.get_settings())
    assert backward.pre_rows == [["2025年预算", "", ""]]
    assert backward.header_values == [["部门", "Q1", "Q2"]]
    assert backward.rows == [["综合处", "120", "130"], ["合计", "120", "130"]]
    assert backward.footer_rows == [4]
    assert backward.footer_values() == [["合计", "120", "130"]]
    assert backward.full_grid() == table.full_grid()
    assert backward.source == "human"
    assert backward.confidence == 1.0
    # 走完整校验构建的表可以原样往返（model_copy 版本不满足该契约）
    assert Table.model_validate(backward.model_dump()) == backward


def test_rebuild_flags_drop_stale_header_codes():
    # Minor 1：人工改表头后，旧的自动表头状态码不再成立；其余 flag 保留。
    sheet = grid([["1", "120"], ["2", "98"]], merges=["A1:B1"])
    table, _ = build_table(
        sheet, Region(1, 2, 1, 2, 1.0), None,
        table_key="t1", settings=config.get_settings())
    assert "table_no_header" in table.flags
    assert "merged_cells" in table.flags
    rebuilt, _ = rebuild_table(
        table, [1],
        [{"key": "c1", "name": "序号"}, {"key": "c2", "name": "数量"}],
        settings=config.get_settings())
    assert "human_confirmed" in rebuilt.flags
    assert "merged_cells" in rebuilt.flags
    assert "table_no_header" not in rebuilt.flags
    assert "repeated_header" not in rebuilt.flags
    assert rebuilt.header_rows == [1]
    assert rebuilt.data_rows() == [["2", "98"]]
