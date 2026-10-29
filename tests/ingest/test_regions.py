from ingest.parsers.regions import Region, split_regions
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows: list[list[str]]) -> SheetGrid:
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v)) if v not in (None, "") else Cell() for v in row]
              for row in padded],
        merges=[],
    )


def test_single_region():
    regions = split_regions(grid([
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["人事处", "98", "101"],
    ]))
    assert regions == [Region(1, 3, 1, 3, regions[0].density)]
    assert regions[0].density == 1.0


def test_blank_row_splits_two_tables():
    regions = split_regions(grid([
        ["部门", "金额"],
        ["综合处", "120"],
        ["", ""],
        ["部门", "人数"],
        ["综合处", "3"],
    ]))
    assert [(r.start_row, r.end_row) for r in regions] == [(1, 2), (4, 5)]


def test_two_blank_columns_split_side_by_side():
    regions = split_regions(grid([
        ["部门", "金额", "", "", "姓名", "人数"],
        ["综合处", "120", "", "", "张伟", "3"],
        ["人事处", "98", "", "", "李娜", "2"],
    ]))
    assert [(r.start_col, r.end_col) for r in regions] == [(1, 2), (5, 6)]


def test_single_blank_column_does_not_split():
    regions = split_regions(grid([
        ["部门", "", "金额"],
        ["综合处", "", "120"],
        ["人事处", "", "98"],
    ]))
    assert len(regions) == 1


def test_sparse_region_is_dropped():
    regions = split_regions(grid([
        ["x", "", "", "", ""],
        ["", "", "", "", "y"],
    ]))
    assert regions == []


def test_region_properties():
    region = Region(2, 5, 3, 7, 0.5)
    assert region.nrows == 4
    assert region.ncols == 5


def test_density_below_threshold_drops_region():
    # 6x11 网格，每行仅第 2r-1 列非空（r=1..6）：
    # 行间隙为 0、列间隙为 1 个空列，均不触发切分，唯一候选区域为 6 行 x 11 列；
    # 密度 6/66 约 0.0909，严格小于 0.10，应被密度规则丢弃。
    rows = [[""] * 11 for _ in range(6)]
    for i in range(6):
        rows[i][2 * i] = "x"
    sheet = grid(rows)
    assert (sheet.nrows, sheet.ncols) == (6, 11)

    assert split_regions(sheet) == []

    # 对照：关闭密度门槛后同一 6x11 候选区域会被保留，
    # 证明上面为空是密度过滤的结果，而非最小尺寸（行/列均不小于 2）规则。
    probe = split_regions(sheet, min_density=0.0)
    assert [(r.start_row, r.end_row, r.start_col, r.end_col) for r in probe] == [
        (1, 6, 1, 11)
    ]
    assert probe[0].density == 6 / 66


def test_density_equal_to_threshold_is_kept():
    # 10x10 仅主对角线非空：密度恰为 10/100 = 0.10；
    # 阈值比较为严格小于，因此该区域应保留。
    rows = [[""] * 10 for _ in range(10)]
    for i in range(10):
        rows[i][i] = "x"
    regions = split_regions(grid(rows))
    assert len(regions) == 1
    region = regions[0]
    assert (region.start_row, region.end_row, region.start_col, region.end_col) == (
        1, 10, 1, 10,
    )
    assert region.density == 0.10


def test_fully_blank_sheet_returns_no_regions():
    assert split_regions(grid([["", "", ""], ["", "", ""]])) == []


def test_two_by_two_bands_are_disjoint_and_row_major():
    # 2 个行带（空行 3 切分）x 2 个列带（空列 3、4 切分），每块均完全填充。
    regions = split_regions(grid([
        ["A", "B", "", "", "C", "D"],
        ["1", "2", "", "", "3", "4"],
        ["", "", "", "", "", ""],
        ["E", "F", "", "", "G", "H"],
        ["5", "6", "", "", "7", "8"],
    ]))
    assert len(regions) == 4
    assert [
        (r.start_row, r.end_row, r.start_col, r.end_col) for r in regions
    ] == [(1, 2, 1, 2), (1, 2, 5, 6), (4, 5, 1, 2), (4, 5, 5, 6)]
    assert all(r.density == 1.0 for r in regions)

    cells = [
        {(row, col) for row in range(r.start_row, r.end_row + 1)
         for col in range(r.start_col, r.end_col + 1)}
        for r in regions
    ]
    assert sum(len(c) for c in cells) == len(set().union(*cells))
