"""PDF 解析编排：文本层提取、页眉页脚剔除、表格回源信息与扫描件标记。

每个文本/表格块都带 1-based 页码与 bbox，检索结果可据此回到 PDF 原位置。
表格走与 XLSX/DOCX 相同的表头链路（设计 §8.5）：模板 > 规则 > LLM。模板
按候选逐行匹配，未命中再按区域顶部 40 行 × 深度 1..3 兜底扫描；规则接受
候选仅在模板全部落空后生效。LLM 每份文档最多 3 次，且只在
``header_llm_enabled``（HEADER_LLM=always）时调用。

P0 只读文本层，不做 OCR、不联网：文本极少却含图片的 PDF 记 ``scanned_pdf``
警告后继续提取，剩余文本仍可入库。
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path

import pdfplumber
import pymupdf

from ingest.config import Settings, get_settings
from ingest.errors import TooLarge
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning
from ingest.parsers.base import ParseError
from ingest.parsers.header_llm import try_llm_header
from ingest.parsers.header_rules import (
    HeaderCandidate,
    compose_header_names,
    detect_header,
)
from ingest.parsers.regions import Region
from ingest.parsers.table_build import build_table
from ingest.parsers.templates import build_signature, match_template, template_columns
from ingest.parsers.xlsx_reader import Cell, SheetGrid
from ingest.sniff import sha256_file

_MAX_LLM_HEADER_CALLS = 3
# 与 DOCX 编排一致：模板兜底扫描限定在区域顶部 40 行 × 深度 1..3。
_MAX_TEMPLATE_SCAN_ROWS = 40
_MAX_TEMPLATE_DEPTH = 3
# 页眉页脚判定：文本块出现在每页首/尾且覆盖至少 60% 页面即视为版式噪声。
_REPEAT_RATIO = 0.6
# 文本块与表格区域重叠超过一半时，该块视为表格内容，不再单独产文本块。
_TABLE_OVERLAP = 0.5
_BOLD_FLAG = 16


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _blocks_for_page(page) -> list[dict]:
    """抽取一页的文本块：文本、bbox、字号与加粗标记（图片块跳过）。"""
    data = page.get_text("dict")
    result: list[dict] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        bold = False
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            lines.append("".join(span.get("text", "") for span in spans))
            for span in spans:
                sizes.append(float(span.get("size", 0)))
                bold = bold or bool(span.get("flags", 0) & _BOLD_FLAG)
        text = "\n".join(lines).strip()
        if not text:
            continue
        result.append({
            "text": text,
            "bbox": [float(v) for v in block["bbox"]],
            "size": max(sizes or [0.0]),
            "bold": bold,
        })
    return result


def _overlaps(a: list[float], b: list[float]) -> float:
    """a 被 b 覆盖的面积比例；用于把表格内的文本块从正文里摘掉。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    area = (ax1 - ax0) * (ay1 - ay0)
    return (ix * iy / area) if area else 0.0


def _drop_repeated_furniture(page_blocks: list[list[dict]],
                             page_count: int) -> None:
    """删除每页首/尾重复出现（≥60% 页面）的页眉页脚文本块。

    只在页首与页尾两类位置统计，正文中偶然相同的句子不会被误删；2 页文档
    同样适用（两页都出现即 100%）。
    """
    if page_count < 2:
        return
    first: Counter[str] = Counter()
    last: Counter[str] = Counter()
    for blocks in page_blocks:
        if not blocks:
            continue
        first[_norm(blocks[0]["text"])] += 1
        last[_norm(blocks[-1]["text"])] += 1
    repeated = {
        text for counter in (first, last)
        for text, count in counter.items()
        if text and count / page_count >= _REPEAT_RATIO
    }
    if not repeated:
        return
    for blocks in page_blocks:
        blocks[:] = [b for b in blocks if _norm(b["text"]) not in repeated]


def _grid_from_extracted(name: str, extracted: list[list] | None) -> SheetGrid | None:
    """pdfplumber 提取结果 → 与 XLSX 读取层同构的网格；不可用返回 None。

    ``extract()`` 的单元格可能是 None，行可能长短不一；这里统一成空串并补齐
    列宽，整表为空或不足两行（没有表头+数据）时直接丢弃。
    """
    if not extracted or len(extracted) < 2:
        return None
    rows = [["" if value is None else str(value) for value in row]
            for row in extracted]
    if not any(cell.strip() for row in rows for cell in row):
        return None
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    return SheetGrid(
        name=name, hidden=False,
        rows=[[Cell(text=value) for value in row] for row in rows],
        merges=[], styles_available=False,
    )


def _usable_templates(templates: list[dict], ncols: int) -> list[dict]:
    """只保留列数与区域宽度一致的模板；坏模板不能进入 build_table。"""
    return [t for t in templates if len(template_columns(t)) == ncols]


def _template_at(grid: SheetGrid, region: Region, start_row: int, depth: int,
                 templates: list[dict]) -> dict | None:
    candidate = HeaderCandidate(
        start_row=start_row, depth=depth, confidence=0.0,
        features={}, penalties=(), source="template")
    names = compose_header_names(grid, region, start_row, depth)
    signature = build_signature(None, region, candidate, names)
    return match_template(signature, templates)


class PdfParser:
    name = "pdf"
    version = "0.1.0"

    def __init__(self, templates: list[dict] | None = None, llm=None,
                 settings: Settings | None = None):
        self.templates = templates or []
        self.llm = llm
        self.settings = settings or get_settings()

    def supports(self, sniff) -> bool:
        return sniff.kind == "pdf"

    def _resolve_header(self, grid: SheetGrid, region: Region,
                        decision) -> tuple[HeaderCandidate | None, list[dict] | None]:
        # 模板优先（设计 §8.5）：命中即用，不能等规则“拒绝”了才回头看模板，
        # 否则规则一旦接受别的行，模板就永远不生效。列数不符的模板直接跳过。
        templates = _usable_templates(self.templates, region.ncols)
        for cand in decision.candidates:
            template = _template_at(
                grid, region, cand.start_row, cand.depth, templates)
            if template:
                return (
                    HeaderCandidate(
                        start_row=cand.start_row, depth=cand.depth,
                        confidence=0.95, features=dict(cand.features),
                        penalties=(), source="template"),
                    template_columns(template),
                )

        # 候选为空（纯数字表头）或被 top 3 挤掉时，按区域顶部 40 行 ×
        # 深度 1..3 兜底扫描模板，先命中者胜。
        if templates:
            last_row = min(region.end_row - 1,
                           region.start_row + _MAX_TEMPLATE_SCAN_ROWS - 1)
            for start_row in range(region.start_row, last_row + 1):
                max_depth = min(_MAX_TEMPLATE_DEPTH,
                                region.end_row - start_row)
                for depth in range(1, max_depth + 1):
                    template = _template_at(
                        grid, region, start_row, depth, templates)
                    if template:
                        return (
                            HeaderCandidate(
                                start_row=start_row, depth=depth,
                                confidence=0.95, features={}, penalties=(),
                                source="template"),
                            template_columns(template),
                        )

        if decision.accepted:
            return decision.candidate, None
        return None, None

    def _collect(self, path: Path) -> tuple[list[list[dict]], list[list[dict]],
                                            int, int, bool]:
        """逐页收集文本块与 pdfplumber 表格；返回 (文本, 表格, 页数, 字符数, 有图片)。"""
        page_blocks: list[list[dict]] = []
        page_tables: list[list[dict]] = []
        total_chars = 0
        has_image = False
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise ParseError("PDF 已加密")
            if doc.page_count > self.settings.max_pdf_pages:
                raise TooLarge(f"PDF 超过 {self.settings.max_pdf_pages} 页")
            with pdfplumber.open(path) as plumber:
                for index, page in enumerate(doc):
                    plumber_page = plumber.pages[index]
                    table_bboxes: list[list[float]] = []
                    tables: list[dict] = []
                    for found in plumber_page.find_tables():
                        grid = _grid_from_extracted(
                            f"page{index + 1}", found.extract())
                        if grid is None:
                            continue
                        bbox = [float(v) for v in found.bbox]
                        table_bboxes.append(bbox)
                        tables.append({"grid": grid, "bbox": bbox})
                    page_tables.append(tables)
                    blocks = [
                        b for b in _blocks_for_page(page)
                        if not any(_overlaps(b["bbox"], tb) > _TABLE_OVERLAP
                                   for tb in table_bboxes)
                    ]
                    page_blocks.append(blocks)
                    total_chars += sum(len(b["text"]) for b in blocks)
                    has_image = has_image or bool(page.get_images())
            page_count = doc.page_count
        return page_blocks, page_tables, page_count, total_chars, has_image

    def parse(self, path: Path) -> DocModel:
        page_blocks, page_tables, page_count, total_chars, has_image = \
            self._collect(path)

        warnings: list[Warning] = []
        if total_chars < 50 and has_image:
            warnings.append(Warning(
                code="scanned_pdf", detail="疑似扫描件，P0 不做 OCR"))
        _drop_repeated_furniture(page_blocks, page_count)

        all_sizes = [b["size"] for blocks in page_blocks for b in blocks
                     if b["size"] > 0]
        body_size = statistics.median(all_sizes) if all_sizes else 10.0

        blocks_out: list[Block] = []
        path_stack: list[str] = []
        table_no = 0
        llm_calls = 0
        for page_no, (text_blocks, tables) in enumerate(
                zip(page_blocks, page_tables), start=1):
            for item in text_blocks:
                text = item["text"]
                level = None
                if item["size"] >= body_size * 1.25 and len(text) <= 40:
                    level = 1
                elif item["bold"] and len(text) <= 60:
                    level = 2
                if level:
                    path_stack = path_stack[: level - 1] + [text]
                    blocks_out.append(Block(
                        block_id=f"b{len(blocks_out) + 1}", type="heading",
                        level=level, text=text, path=list(path_stack),
                        page=page_no, bbox=item["bbox"]))
                else:
                    blocks_out.append(Block(
                        block_id=f"b{len(blocks_out) + 1}", type="paragraph",
                        text=text, path=list(path_stack), page=page_no,
                        bbox=item["bbox"]))

            for table_info in tables:
                table_no += 1
                table_key = f"t{table_no}"
                grid: SheetGrid = table_info["grid"]
                region = Region(1, grid.nrows, 1, grid.ncols, 1.0)
                decision = detect_header(grid, region, self.settings)
                header, forced_columns = self._resolve_header(
                    grid, region, decision)

                if header is None and self.settings.header_llm_enabled \
                        and self.llm is not None \
                        and llm_calls < _MAX_LLM_HEADER_CALLS:
                    header, raw = try_llm_header(grid, region, self.llm)
                    llm_calls += 1
                    if header is None:
                        warnings.append(Warning(
                            code="ambiguous_header", detail=raw[:500]))
                elif header is None and decision.candidate is not None:
                    warnings.append(Warning(
                        code="ambiguous_header",
                        detail=f"置信度 {decision.candidate.confidence:.2f}"))

                try:
                    table, table_warnings = build_table(
                        grid, region, header, table_key=table_key,
                        settings=self.settings,
                        forced_columns=forced_columns)
                except Exception as exc:  # 单表失败不拖垮整份文档
                    warnings.append(Warning(
                        code="parse_partial",
                        detail=f"table {table_key}: {exc}"))
                    continue
                warnings.extend(
                    Warning(code=w.code, detail=w.detail) for w in table_warnings)
                table = table.model_copy(update={
                    "page": page_no, "bbox": table_info["bbox"]})
                blocks_out.append(Block(
                    block_id=f"b{len(blocks_out) + 1}", type="table",
                    table=table, path=list(path_stack), page=page_no,
                    bbox=table_info["bbox"]))

        if not blocks_out:
            warnings.append(Warning(
                code="empty_document", detail="PDF 中没有可入库文本"))
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(
                title=path.stem,
                source_filename=path.name,
                sha256=sha256_file(path),
            ),
            blocks=blocks_out,
            warnings=warnings,
        )
