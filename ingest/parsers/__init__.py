from __future__ import annotations

from ingest.parsers.base import Parser


def get_parser(kind: str, **kwargs) -> Parser:
    if kind in ("xlsx", "xlsm"):
        from ingest.parsers.xlsx_parser import XlsxParser
        return XlsxParser(**kwargs)
    raise KeyError(f"未注册的解析器类型: {kind}")
