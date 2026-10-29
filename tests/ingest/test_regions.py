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
