"""表头识别：七特征打分、候选生成、接受判定与列名合成。

打分只看网格与相邻行，不依赖外部模型；`HEADER_LLM=auto` 时不在这里调用 LLM
（计划差异 4：评测门禁建立前只有显式 always 才走 LLM，见 Task 8）。

层次列名用 "/" 拼接父级与子级（合并单元格已由读取层展开到每个子列），
例如两行表头 ``预算 / Q1`` 合成为 ``预算/Q1``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest.config import Settings
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import Cell, SheetGrid

KEYWORDS = (
    "序号", "编号", "名称", "项目", "部门", "单位", "金额", "数量", "单价",
    "合计", "预算", "实际", "支出", "收入", "比例", "日期", "时间", "备注",
    "说明", "类型", "状态", "人数", "季度", "月份", "年度",
)
WEIGHTS = {
    "fill": 0.12, "type_contrast": 0.28, "uniqueness": 0.14, "style": 0.14,
    "keyword": 0.10, "length": 0.08, "context": 0.14,
}
PENALTIES = {
    "data_above": 0.20, "duplicate_names": 0.20,
    "empty_headers": 0.30, "too_verbose": 0.20,
}
_NUMBER_RE = re.compile(r"^[+-]?[\d,]+(\.\d+)?%?$")
_MAX_SCAN_ROWS = 20
_MAX_HEADER_DEPTH = 3
_MAX_CELL_CHARS = 40
# 扩展表头深度时，下一行数字占比超过该值即视为数据行，不再吞进表头
_MAX_EXTENSION_NUMERIC_RATIO = 0.5


def _is_number(text: str) -> bool:
    return bool(_NUMBER_RE.match(text.replace("￥", "").replace("¥", "").strip()))


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _ratio(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass(frozen=True)
class HeaderCandidate:
    start_row: int
    depth: int
    confidence: float
    features: dict[str, float] = field(default_factory=dict)
    penalties: tuple[str, ...] = ()
    source: str = "heuristic"

    def to_dict(self) -> dict:
        return {
            "start_row": self.start_row, "depth": self.depth,
            "confidence": round(self.confidence, 4),
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "penalties": list(self.penalties), "source": self.source,
        }


@dataclass
class HeaderDecision:
    candidate: HeaderCandidate | None
    accepted: bool
    reason: str
    candidates: list[HeaderCandidate]


def _window(sheet: SheetGrid, region: Region, start_row: int,
            depth: int) -> list[list[Cell]]:
    return [
        [sheet.cell(r, c) for c in range(region.start_col, region.end_col + 1)]
        for r in range(start_row, start_row + depth)
    ]


def compose_header_names(sheet: SheetGrid, region: Region, start_row: int,
                         depth: int) -> list[str]:
    """按列把多行表头拼成层次列名；空白与相邻重复片段（合并展开）被压掉。"""
    names: list[str] = []
    for c in range(region.start_col, region.end_col + 1):
        parts: list[str] = []
        for r in range(start_row, start_row + depth):
            text = _norm(sheet.cell(r, c).text)
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        names.append("/".join(parts))
    return names


def _header_text_ratio(window: list[list[Cell]]) -> float:
    cells = [cell for row in window for cell in row if cell.text.strip()]
    return _ratio([not cell.is_numeric and not _is_number(cell.text) for cell in cells])


def _below_numeric_ratio(sheet: SheetGrid, region: Region, data_start: int) -> float:
    ratios = []
    for c in range(region.start_col, region.end_col + 1):
        values = [sheet.cell(r, c).text
                  for r in range(data_start, min(region.end_row, data_start + 19) + 1)]
        nonempty = [v for v in values if v.strip()]
        if nonempty:
            ratios.append(_ratio([_is_number(v) or "-" in v or "/" in v for v in nonempty]))
    return sum(ratios) / len(ratios) if ratios else 0.0


def _style_ratio(window: list[list[Cell]], sheet: SheetGrid) -> float:
    if not sheet.styles_available:
        return 0.5
    cells = [cell for row in window for cell in row if cell.text.strip()]
    return _ratio([cell.bold or cell.has_fill for cell in cells])


def _looks_like_title(sheet: SheetGrid, region: Region, row: int) -> bool:
    if row < region.start_row:
        return True
    texts = [sheet.cell(row, c).text for c in range(region.start_col, region.end_col + 1)]
    nonempty = [t for t in texts if t.strip()]
    if not nonempty:
        return True
    numeric = _ratio([_is_number(t) for t in nonempty])
    avg_len = sum(len(t) for t in nonempty) / len(nonempty)
    return numeric < 0.2 and avg_len > 12


def _is_header_like_row(sheet: SheetGrid, region: Region, row: int) -> bool:
    """扩展表头深度时用：整行非空且以文字为主，才可能仍是表头。"""
    texts = [sheet.cell(row, c).text.strip()
             for c in range(region.start_col, region.end_col + 1)]
    nonempty = [t for t in texts if t]
    if not nonempty:
        return False
    return _ratio([_is_number(t) for t in nonempty]) <= _MAX_EXTENSION_NUMERIC_RATIO


def score_candidate(sheet: SheetGrid, region: Region, start_row: int,
                    depth: int) -> HeaderCandidate:
    window = _window(sheet, region, start_row, depth)
    names = compose_header_names(sheet, region, start_row, depth)
    nonempty_names = [n for n in names if n]
    data_start = start_row + depth
    prev_row = start_row - 1
    next_ok = data_start <= region.end_row

    fill = _ratio([bool(cell.text.strip()) for row in window for cell in row])
    type_contrast = 0.5 * _header_text_ratio(window) + 0.5 * (
        _below_numeric_ratio(sheet, region, data_start) if next_ok else 0.0
    )
    uniqueness = len(set(nonempty_names)) / len(nonempty_names) if nonempty_names else 0.0
    style = _style_ratio(window, sheet)
    keyword = _ratio([any(k in n for k in KEYWORDS) for n in nonempty_names]) \
        if nonempty_names else 0.0
    avg_len = (sum(len(n) for n in nonempty_names) / len(nonempty_names)
               if nonempty_names else 99)
    length = max(0.0, 1.0 - min(avg_len, 30) / 30)
    context = 0.5 * (1.0 if _looks_like_title(sheet, region, prev_row) else 0.0) + 0.5 * (
        1.0 if next_ok else 0.0
    )

    features = {
        "fill": fill, "type_contrast": type_contrast, "uniqueness": uniqueness,
        "style": style, "keyword": keyword, "length": length, "context": context,
    }
    penalties: list[str] = []
    if prev_row >= region.start_row and not _looks_like_title(sheet, region, prev_row):
        penalties.append("data_above")
    if len(nonempty_names) != len(set(nonempty_names)):
        penalties.append("duplicate_names")
    header_cells = [cell for row in window for cell in row]
    if _ratio([not cell.text.strip() for cell in header_cells]) > 0.3:
        penalties.append("empty_headers")
    if avg_len > 25:
        penalties.append("too_verbose")

    score = sum(WEIGHTS[k] * features[k] for k in WEIGHTS) - sum(
        PENALTIES[p] for p in penalties
    )
    return HeaderCandidate(
        start_row=start_row, depth=depth,
        confidence=max(0.0, min(1.0, score)),
        features=features, penalties=tuple(penalties),
    )


def generate_candidates(sheet: SheetGrid, region: Region) -> list[HeaderCandidate]:
    candidates: list[HeaderCandidate] = []
    last_start = min(region.end_row - 1, region.start_row + _MAX_SCAN_ROWS - 1)
    for start in range(region.start_row, last_start + 1):
        row = [sheet.cell(start, c) for c in range(region.start_col, region.end_col + 1)]
        texts = [cell.text.strip() for cell in row if cell.text.strip()]
        if not texts:
            continue
        if any(len(t) > _MAX_CELL_CHARS for t in texts):
            continue
        numeric = _ratio([_is_number(t) for t in texts])
        has_keyword = any(any(k in t for k in KEYWORDS) for t in texts)
        if numeric > 0.7 and not has_keyword:
            continue
        for depth in range(1, _MAX_HEADER_DEPTH + 1):
            if start + depth > region.end_row:
                break
            # 表头行必须连续且都像表头；一旦扩展行开始像数据，更深候选全部作废，
            # 否则“表头+首行数据”会凭更长的列名拿到更高分。
            if depth > 1 and not _is_header_like_row(sheet, region, start + depth - 1):
                break
            candidates.append(score_candidate(sheet, region, start, depth))
    return sorted(candidates, key=lambda c: c.confidence, reverse=True)


def detect_header(sheet: SheetGrid, region: Region, settings: Settings) -> HeaderDecision:
    candidates = generate_candidates(sheet, region)
    if not candidates:
        return HeaderDecision(None, False, "no_candidates", [])
    top = candidates[0]
    second = candidates[1].confidence if len(candidates) > 1 else 0.0
    margin = top.confidence - second
    if top.confidence < settings.header_accept:
        return HeaderDecision(top, False, "low_confidence", candidates[:3])
    if margin < settings.header_margin:
        return HeaderDecision(top, False, "margin_too_small", candidates[:3])
    return HeaderDecision(top, True, "accepted", candidates[:3])
