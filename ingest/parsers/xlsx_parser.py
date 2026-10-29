"""XLSX/XLSM 解析编排：读网格 → 切区域 → 表头（模板 > 规则 > LLM）→ Table。

表头兜底顺序按设计 §8.5：先模板（人工确认过的版式比启发式分数更可信，
命中即用）、再规则接受、最后 LLM。LLM 每个工作簿最多 3 次，且只在
``header_llm_enabled``（HEADER_LLM=always）时调用。
"""
from __future__ import annotations

from pathlib import Path

from ingest.config import Settings, get_settings
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning
from ingest.parsers.header_llm import try_llm_header
from ingest.parsers.header_rules import (
    HeaderCandidate, compose_header_names, detect_header,
)
from ingest.parsers.regions import Region, split_regions
from ingest.parsers.table_build import build_table
from ingest.parsers.templates import (
    build_signature, match_template, template_columns,
)
from ingest.parsers.xlsx_reader import SheetGrid, read_workbook
from ingest.sniff import sha256_file

_MAX_LLM_HEADER_CALLS = 3
# 模板兜底扫描窗口：规则候选只保留 top 3，纯数字表头（如 2024|2025）甚至
# 不产生候选，模板命中不能受候选生成限制。
_MAX_TEMPLATE_SCAN_ROWS = 40
_MAX_TEMPLATE_DEPTH = 3


def _usable_templates(templates: list[dict], ncols: int) -> list[dict]:
    """只保留列数与区域宽度一致的模板；坏模板不能进入 build_table。"""
    return [t for t in templates if len(template_columns(t)) == ncols]


def _template_at(sheet: SheetGrid, region: Region, start_row: int, depth: int,
                 templates: list[dict]) -> dict | None:
    candidate = HeaderCandidate(start_row, depth, 0.0, {}, (), "template")
    names = compose_header_names(sheet, region, start_row, depth)
    signature = build_signature(sheet.name, region, candidate, names)
    return match_template(signature, templates)


class XlsxParser:
    name = "xlsx"
    version = "0.1.0"

    def __init__(self, templates: list[dict] | None = None, llm=None,
                 settings: Settings | None = None):
        self.templates = templates or []
        self.llm = llm
        self.settings = settings or get_settings()

    def supports(self, sniff) -> bool:
        return sniff.kind in ("xlsx", "xlsm")

    def parse(self, path: Path) -> DocModel:
        workbook = read_workbook(path, self.settings)
        warnings: list[Warning] = [
            Warning(code=w["code"], detail=w.get("detail", ""))
            for w in workbook.warnings
        ]
        blocks: list[Block] = []
        table_no = 0
        llm_calls = 0

        for sheet in workbook.sheets:
            if sheet.hidden and not self.settings.include_hidden_sheets:
                warnings.append(Warning(
                    code="hidden_sheet_skipped", detail=sheet.name))
                continue
            regions = split_regions(sheet)
            if len(regions) > 1:
                warnings.append(Warning(
                    code="multiple_tables", detail=sheet.name))
            for region in regions:
                table_no += 1
                decision = detect_header(sheet, region, self.settings)
                header = None
                forced_columns = None

                # 模板优先（设计 §8.5）：人工确认过的版式比启发式分数更可信，
                # 命中即用；不能等规则“拒绝”了才回头看模板，否则规则一旦接受
                # 别的行，模板就永远不生效。列数不符的模板直接跳过。
                templates = _usable_templates(self.templates, region.ncols)
                for cand in decision.candidates:
                    template = _template_at(
                        sheet, region, cand.start_row, cand.depth, templates)
                    if template:
                        header = HeaderCandidate(
                            cand.start_row, cand.depth, 0.95,
                            dict(cand.features), (), "template")
                        forced_columns = template_columns(template)
                        break

                # 候选为空（纯数字表头）或被 top 3 挤掉时，按区域顶部 40 行 ×
                # 深度 1..3 兜底扫描模板，先命中者胜。
                if header is None and templates:
                    last_row = min(region.end_row - 1,
                                   region.start_row + _MAX_TEMPLATE_SCAN_ROWS - 1)
                    for start_row in range(region.start_row, last_row + 1):
                        max_depth = min(_MAX_TEMPLATE_DEPTH,
                                        region.end_row - start_row)
                        for depth in range(1, max_depth + 1):
                            template = _template_at(
                                sheet, region, start_row, depth, templates)
                            if template:
                                header = HeaderCandidate(
                                    start_row, depth, 0.95, {}, (), "template")
                                forced_columns = template_columns(template)
                                break
                        if header is not None:
                            break

                if header is None and decision.accepted:
                    header = decision.candidate

                if header is None and self.settings.header_llm_enabled \
                        and self.llm is not None \
                        and llm_calls < _MAX_LLM_HEADER_CALLS:
                    header, raw = try_llm_header(sheet, region, self.llm)
                    llm_calls += 1
                    if header is None:
                        warnings.append(Warning(
                            code="ambiguous_header", detail=raw[:500]))
                elif header is None and decision.candidate is not None:
                    warnings.append(Warning(
                        code="ambiguous_header",
                        detail=f"置信度 {decision.candidate.confidence:.2f}"))

                table_key = f"t{table_no}"
                try:
                    table, table_warnings = build_table(
                        sheet, region, header, table_key=table_key,
                        settings=self.settings, forced_columns=forced_columns)
                except Exception as exc:  # 单表失败不拖垮整份文件
                    warnings.append(Warning(
                        code="parse_partial",
                        detail=f"{sheet.name} {table_key}: {exc}"))
                    continue
                for warning in table_warnings:
                    warnings.append(Warning(
                        code=warning.code, detail=warning.detail))
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="table",
                    table=table, path=[],
                ))

        if not blocks:
            warnings.append(Warning(
                code="empty_document", detail="未找到可入库内容"))
        sha = sha256_file(path)
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(title=path.stem, source_filename=path.name, sha256=sha),
            blocks=blocks,
            warnings=warnings,
        )
