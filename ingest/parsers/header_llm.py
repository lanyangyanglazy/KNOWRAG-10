"""LLM 表头兜底：构造有界输入、严格校验 JSON 契约、产出候选。

只经注入的 llm 对象调用（离线私有化，导入期不联网），每次调用最多 invoke 一次；
每个工作簿 ≤3 次的限额由编排层（Task 9/13）持有。表头行必须能规范化为连续块，
否则抛 ValueError，由调用方落到人工复核，不放宽 IR 的连续性约束。
"""
from __future__ import annotations

import json
import math
import re

from openpyxl.utils.cell import range_boundaries

from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import SheetGrid

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_MAX_SCAN_ROWS = 20
_MAX_SCAN_COLS = 30
_MAX_CELL_CHARS = 40
_MAX_HEADER_DEPTH = 3
_MAX_MERGES = 50

PROMPT = """你是表格结构识别助手。下面是 Excel 区域左上角最多 20 行 × 30 列的 TSV，
单元格已截断到 40 字符；merged 行列出合并区间。请只输出 JSON：
{"header_rows":[3,4],"data_start":5,"end_row":57,
 "columns":["部门","Q1预算(万元)"],"footer_rows":[56],
 "confidence":0.8,"reason":"..."}
规则：header_rows 用区域内的绝对行号，必须从表头首行起连续、最多 3 行；
data_start 必须大于最后一个表头行；columns 数量等于区域列数，列名非空且不重复；
footer_rows 必须位于数据区（data_start..end_row）内且不与表头重叠。
不要输出解释或 Markdown。"""


def _region_merges(sheet: SheetGrid, region: Region) -> list[str]:
    """只把与当前区域相交的合并区间发给模型，控制提示词体积。"""
    kept: list[str] = []
    for ref in sheet.merges:
        try:
            min_col, min_row, max_col, max_row = range_boundaries(ref)
        except ValueError:
            continue
        if max_row < region.start_row or min_row > region.end_row:
            continue
        if max_col < region.start_col or min_col > region.end_col:
            continue
        kept.append(ref)
        if len(kept) >= _MAX_MERGES:
            break
    return kept


def build_llm_input(sheet: SheetGrid, region: Region) -> str:
    lines = [
        f"range: rows {region.start_row}-{region.end_row}, "
        f"cols {region.start_col}-{region.end_col}"
    ]
    merges = _region_merges(sheet, region)
    if merges:
        lines.append("merged: " + ", ".join(merges))
    last_row = min(region.end_row, region.start_row + _MAX_SCAN_ROWS - 1)
    last_col = min(region.end_col, region.start_col + _MAX_SCAN_COLS - 1)
    for r in range(region.start_row, last_row + 1):
        cells = []
        for c in range(region.start_col, last_col + 1):
            text = sheet.cell(r, c).text
            text = text.replace("\t", " ").replace("\r", " ") \
                .replace("\n", " ")[:_MAX_CELL_CHARS]
            cells.append(text)
        lines.append("\t".join(cells))
    return "\n".join(lines)


def _extract_json(raw: str) -> dict:
    if not isinstance(raw, str):
        raise ValueError("LLM 输出不是文本")
    match = _JSON_BLOCK.search(raw)
    if not match:
        raise ValueError("未找到 JSON")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 解析失败") from exc
    if not isinstance(payload, dict):
        raise ValueError("LLM 输出必须是 JSON 对象")
    return payload


def _is_int(value) -> bool:
    # bool 是 int 子类，JSON 里的 true/false 不能当行号
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_header_rows(value, region: Region) -> list[int]:
    if not isinstance(value, list) or not value or not all(_is_int(v) for v in value):
        raise ValueError("header_rows 非法")
    if len(value) > _MAX_HEADER_DEPTH:
        raise ValueError(f"header_rows 最多 {_MAX_HEADER_DEPTH} 行")
    if value != list(range(value[0], value[0] + len(value))):
        raise ValueError("header_rows 必须是连续递增的行号")
    scan_end = min(region.start_row + _MAX_SCAN_ROWS - 1, region.end_row)
    if not all(region.start_row <= v <= scan_end for v in value):
        raise ValueError("header_rows 越界")
    return value


def parse_llm_output(raw: str, region: Region, ncols: int) -> dict:
    payload = _extract_json(raw)
    header_rows = _validate_header_rows(payload.get("header_rows"), region)

    data_start = payload.get("data_start")
    if not _is_int(data_start) or data_start <= header_rows[-1]:
        raise ValueError("data_start 必须大于最后一个表头行")
    end_row = payload.get("end_row")
    if not _is_int(end_row) or end_row < data_start or end_row > region.end_row:
        raise ValueError("end_row 非法")

    columns = payload.get("columns")
    if not isinstance(columns, list) or len(columns) != ncols:
        raise ValueError("columns 数量必须等于区域列数")
    if not all(isinstance(c, str) and c.strip() for c in columns):
        raise ValueError("columns 不能为空")
    if len({c.strip() for c in columns}) != len(columns):
        raise ValueError("columns 不能重复")

    footer_rows = payload.get("footer_rows", [])
    if footer_rows is None:  # null 与省略等价；其他非列表值一律拒绝
        footer_rows = []
    if not isinstance(footer_rows, list) or not all(_is_int(v) for v in footer_rows):
        raise ValueError("footer_rows 非法")
    if set(footer_rows) & set(header_rows):
        raise ValueError("footer_rows 不能与表头重叠")
    # 合计行属于表尾数据行（IR 的 data_rows() 按 footer_rows 剔除），不能越过数据区
    if any(v < data_start or v > end_row for v in footer_rows):
        raise ValueError("footer_rows 必须位于数据区内")

    confidence = payload.get("confidence", 0.8)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence 非法")
    confidence = float(confidence)
    if not math.isfinite(confidence):
        raise ValueError("confidence 非法")

    return {
        "header_rows": header_rows,
        "data_start": data_start,
        "end_row": end_row,
        "columns": columns,
        "footer_rows": footer_rows,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(payload.get("reason", "")),
    }


def _content_text(content) -> str:
    """兼容 LangChain 消息 content 为字符串或内容块列表两种形态。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def try_llm_header(sheet: SheetGrid, region: Region,
                   llm) -> tuple[HeaderCandidate | None, str]:
    payload = build_llm_input(sheet, region)
    try:
        response = llm.invoke([("system", PROMPT), ("human", payload)])
    except Exception as exc:  # 模型故障不应拖垮整份文件，保留原因供审计
        return None, f"llm_error: {exc}"
    raw = _content_text(getattr(response, "content", ""))
    try:
        parsed = parse_llm_output(raw, region, ncols=region.ncols)
    except ValueError:
        return None, raw
    candidate = HeaderCandidate(
        start_row=parsed["header_rows"][0],
        depth=len(parsed["header_rows"]),
        confidence=parsed["confidence"],
        features={"llm": 1.0},
        penalties=(),
        source="llm",
    )
    return candidate, raw
