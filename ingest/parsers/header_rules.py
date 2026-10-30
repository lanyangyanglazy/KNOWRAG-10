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
    # 政务/监管表格高频表头词：缺了它们，"类别/事项/内容"这类无数字表头
    # 会拿不到任何关键词信号，在 PDF（无样式）上被误判为低置信度。
    "类别", "事项", "内容", "编码", "层级", "形式", "范围", "对象", "依据", "标准",
)
WEIGHTS = {
    "fill": 0.12, "type_contrast": 0.28, "uniqueness": 0.14, "style": 0.14,
    "keyword": 0.10, "length": 0.08, "context": 0.14,
}
PENALTIES = {
    # 列名重复（如三列都叫"金额"）是强负信号：它几乎必然意味着选错了行，
    # 因此惩罚要足以把分数压到阈值以下，而不是只扣一点点。
    "data_above": 0.20, "duplicate_names": 0.25,
    "empty_headers": 0.30, "too_verbose": 0.20,
    # 深度 >1 时，被并入表头的行如果出现长文本，大概率是首行数据被误吞
    # （典型：PDF 表格第二行是"事项描述"这类长句），必须显著压低其分数。
    "extension_looks_like_data": 0.10,
}
# 深度扩展行的单元格长度上限：表头单元格通常很短，数据描述往往超过该值。
_MAX_EXTENSION_CELL_CHARS = 25
_NUMBER_RE = re.compile(r"^[+-]?[\d,]+(\.\d+)?%?$")
_MAX_SCAN_ROWS = 20
_MAX_HEADER_DEPTH = 3
_MAX_CELL_CHARS = 40
# 扩展表头深度时，下一行数字占比超过该值即视为数据行，不再吞进表头；
# 但命中 KEYWORDS 的行（如“部门 | 2024 | 2025”这类年份/月份标签）不受此限
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
    """扩展表头深度时用：整行非空且不像数据行，才可能仍是表头。

    数字占比高的行通常是数据行；但年份/月份等数字型表头标签合法，
    因此命中 KEYWORDS 的行仍按表头处理（例如 ``部门 | 2024 | 2025``）。
    """
    texts = [sheet.cell(row, c).text.strip()
             for c in range(region.start_col, region.end_col + 1)]
    nonempty = [t for t in texts if t]
    if not nonempty:
        return False
    if any(any(k in t for k in KEYWORDS) for t in nonempty):
        return True
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
    # 只看"被扩展进来的行"：首行本身就是长文本的情况由 too_verbose 负责。
    if depth > 1 and any(
            len(_norm(cell.text)) > _MAX_EXTENSION_CELL_CHARS
            for row in window[1:] for cell in row):
        penalties.append("extension_looks_like_data")
    # 首列是"序号/编号/项目号"这类行键时，下一行首格是纯数字几乎必然是数据行，
    # 不是第二层表头。中文表格里这条信号非常稳定（Excel/PDF 都适用）。
    if depth > 1:
        first_header = _norm(window[0][0].text)
        if (any(key in first_header for key in ("序号", "编号", "项目号", "项号"))
                and any(_is_number(row[0].text) for row in window[1:])):
            penalties.append("extension_looks_like_data")

    # PDF 网格没有样式信息，style 恒为 0.5：它不提供区分度，却占着 14% 权重，
    # 会把所有候选的分数整体压到阈值附近。无样式时按比例分摊给其余特征，
    # 保证权重和仍为 1，且不同格式的分数可比。
    weights = dict(WEIGHTS)
    if not sheet.styles_available:
        style_weight = weights.pop("style")
        rest = sum(weights.values())
        for key in weights:
            weights[key] += style_weight * weights[key] / rest
    # 位置先验：表头必然靠近表格顶部。没有这一项时，深层的"章节小标题行"
    # 会拿到接近的分数，触发 margin_too_small 把正确表头否掉。
    position_penalty = min(0.09, 0.015 * max(0, start_row - region.start_row))
    score = sum(weights[k] * features[k] for k in weights) - sum(
        PENALTIES[p] for p in penalties) - position_penalty
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
    # margin 用来判断"表头起始行是否唯一"。同一 start_row 的 depth=1/2/3
    # 是同一个假设的不同深度，不能用它来否掉 top——否则会出现
    # "正确表头 0.803、它自己的两行版本 0.684，差 0.119 就拒绝"的荒谬结果。
    # 深度选择由分数本身完成：更深的候选只有在明显更优时才会排到第一。
    second = max(
        (candidate.confidence for candidate in candidates[1:]
         if candidate.start_row != top.start_row),
        default=0.0,
    )
    margin = top.confidence - second
    if top.confidence < settings.header_accept:
        return HeaderDecision(top, False, "low_confidence", candidates[:3])
    if margin < settings.header_margin:
        return HeaderDecision(top, False, "margin_too_small", candidates[:3])
    return HeaderDecision(top, True, "accepted", candidates[:3])
