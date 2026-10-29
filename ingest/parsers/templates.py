"""版式模板：列名归一化、签名构建、模板匹配与配置提取。

模板由复核台人工确认后保存，命中即用；编排层在规则打分之前调用匹配
（设计 §8.6）。签名里的 ``sheet_name`` 只供匹配时取表名，不参与持久化语义。
"""
from __future__ import annotations

import re
import unicodedata

from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.regions import Region

_UNIT_SUFFIX = re.compile(r"[（(][^）)]{1,8}[）)]\s*$")
_TRAILING_MARK = re.compile(r"[\s*＊]+$")
_JACCARD_THRESHOLD = 0.90


def normalize_header_name(text: str) -> str:
    """NFKC、英文小写、去单位括号与末尾标记、去空白，用于模板列名比较。

    “金额(元)*”末尾标记在单位括号之后，因此先剥末尾标记再剥单位，循环到稳定；
    只剥一层处理不了“金额（万元）(元)”这类连续后缀。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    while True:
        stripped = _TRAILING_MARK.sub("", text)
        stripped = _UNIT_SUFFIX.sub("", stripped)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\s+", "", text)


def build_signature(sheet_name: str | None, region: Region,
                    candidate: HeaderCandidate, names: list[str]) -> dict:
    return {
        "ncols": len(names),
        "header_depth": candidate.depth,
        "relative_header_row": candidate.start_row - region.start_row,
        "columns_norm": [normalize_header_name(n) for n in names],
        "sheet_pattern": f"^{re.escape(sheet_name)}$" if sheet_name else None,
        # 匹配时直接读签名里的表名，调用方不必再传一次
        "sheet_name": sheet_name,
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _sheet_pattern_matches(pattern: str, sheet_name: str) -> bool:
    try:
        return re.search(pattern, sheet_name) is not None
    except re.error:
        # 模板正则由人工维护；坏模式只让该模板不命中，不影响整份文件
        return False


def match_template(signature: dict, templates: list[dict]) -> dict | None:
    for template in templates:
        sig = template.get("signature") or {}
        if sig.get("ncols") != signature.get("ncols"):
            continue
        pattern = sig.get("sheet_pattern")
        if pattern and not _sheet_pattern_matches(
                pattern, signature.get("sheet_name") or ""):
            continue
        if _jaccard(set(sig.get("columns_norm") or []),
                    set(signature.get("columns_norm") or [])) >= _JACCARD_THRESHOLD:
            return template
    return None


def template_header_rows(template: dict, region: Region) -> list[int]:
    sig = template["signature"]
    start = region.start_row + int(sig["relative_header_row"])
    return [start + i for i in range(int(sig["header_depth"]))]


def template_columns(template: dict) -> list[dict]:
    return list((template.get("config") or {}).get("columns") or [])
