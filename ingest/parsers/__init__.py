from __future__ import annotations

from ingest.parsers.base import Parser


def get_parser(kind: str, **kwargs) -> Parser:
    if kind in ("xlsx", "xlsm"):
        from ingest.parsers.xlsx_parser import XlsxParser
        return XlsxParser(**kwargs)
    if kind == "docx":
        from ingest.parsers.docx_parser import DocxParser
        return DocxParser(**kwargs)
    if kind == "doc":
        from ingest.parsers.docx_parser import DocParser
        return DocParser(**kwargs)
    if kind == "pdf":
        from ingest.parsers.pdf_parser import PdfParser
        return PdfParser(**kwargs)
    if kind == "markdown":
        from ingest.parsers.markdown_parser import MarkdownParser
        return MarkdownParser(**kwargs)
    raise KeyError(f"未注册的解析器类型: {kind}")
