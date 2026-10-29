from __future__ import annotations

from dataclasses import dataclass

from ingest.parsers.xlsx_reader import SheetGrid


@dataclass(frozen=True)
class Region:
    start_row: int
    end_row: int
    start_col: int
    end_col: int
    density: float

    @property
    def nrows(self) -> int:
        return self.end_row - self.start_row + 1

    @property
    def ncols(self) -> int:
        return self.end_col - self.start_col + 1


def _bands(indices: list[int], max_blank: int) -> list[tuple[int, int]]:
    """把非空行/列号切成连续带；相邻非空行/列之间空出至少 max_blank 个即断开。"""
    if not indices:
        return []
    bands: list[tuple[int, int]] = []
    start = prev = indices[0]
    for idx in indices[1:]:
        if idx - prev - 1 >= max_blank:
            bands.append((start, prev))
            start = idx
        prev = idx
    bands.append((start, prev))
    return bands


def split_regions(sheet: SheetGrid, *, min_density: float = 0.10) -> list[Region]:
    rows = [r for r in range(1, sheet.nrows + 1)
            if any(sheet.cell(r, c).text.strip() for c in range(1, sheet.ncols + 1))]
    cols = [c for c in range(1, sheet.ncols + 1)
            if any(sheet.cell(r, c).text.strip() for r in range(1, sheet.nrows + 1))]
    if not rows or not cols:
        return []

    row_bands = _bands(rows, max_blank=1)      # 1 个全空行即切分
    col_bands = _bands(cols, max_blank=2)      # 连续 2 个全空列才切分

    regions: list[Region] = []
    for r0, r1 in row_bands:
        for c0, c1 in col_bands:
            total = (r1 - r0 + 1) * (c1 - c0 + 1)
            filled = sum(
                1 for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)
                if sheet.cell(r, c).text.strip()
            )
            density = filled / total if total else 0.0
            if (r1 - r0 + 1) < 2 or (c1 - c0 + 1) < 2 or density < min_density:
                continue
            regions.append(Region(r0, r1, c0, c1, density))
    return sorted(regions, key=lambda r: (r.start_row, r.start_col))
