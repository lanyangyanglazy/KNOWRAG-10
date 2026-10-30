"""解析 / 清洗质量体检：把一批文件跑成可比较的数字。

关注四类问题，它们都会以"检索效果差"的形式出现，而不是报错：

1. **取字**：有没有文字、字符是否像乱码、扫描件是否被识别出来；
2. **结构**：标题层级、表格数量与行列、表单型是否被误判；
3. **清洗**：PDF 页眉页脚剔除前后字符量变化、零宽字符/控制字符残留；
4. **切片**：在指定 profile 下的切片数、长度分布、类型分布。

用法：

    uv run python -m ingest.quality_audit data/public_samples
    uv run python -m ingest.quality_audit data/public_samples --profile p600
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ingest.chunking import chunk_document
from ingest.errors import IngestError
from ingest.parsers import get_parser
from ingest.profiles import resolve_profile
from ingest.sniff import sniff_file

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".md"}


@dataclass
class FileAudit:
    file: str
    size_bytes: int
    sha256: str
    kind: str | None = None
    supported: bool = True
    error: str | None = None
    parser: str | None = None
    title: str | None = None

    blocks: int = 0
    blocks_by_type: dict = field(default_factory=dict)
    headings: int = 0
    tables: int = 0
    table_rows: int = 0
    table_cols: int = 0
    chars: int = 0
    cjk_ratio: float | None = None
    zero_width: int = 0
    control_chars: int = 0
    warning_counts: dict = field(default_factory=dict)

    pdf_pages: int | None = None
    pdf_raw_chars: int | None = None
    furniture_removed_ratio: float | None = None
    repeated_header_ratio: float | None = None

    chunks: int = 0
    chunk_types: dict = field(default_factory=dict)
    chunk_min: int = 0
    chunk_median: int = 0
    chunk_max: int = 0

    verdict: str = ""
    notes: list[str] = field(default_factory=list)


def _text_of(block) -> str:
    if block.table is not None:
        return "".join(cell for row in block.table.rows for cell in row)
    return block.text or ""


def _pdf_raw_metrics(path: Path) -> tuple[int, int, float | None]:
    """返回 (页数, 原始字符数, 重复页眉页脚比例)，用于衡量清洗动作。"""
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover
        return 0, 0, None

    with fitz.open(path) as doc:
        pages = doc.page_count
        raw = 0
        firsts: list[str] = []
        lasts: list[str] = []
        for page in doc:
            text = page.get_text() or ""
            raw += len(text)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                firsts.append(lines[0])
                lasts.append(lines[-1])
    repeated = 0
    for lines in (firsts, lasts):
        if not lines:
            continue
        top = max(set(lines), key=lines.count)
        if lines.count(top) >= max(2, int(len(lines) * 0.6)):
            repeated += 1
    ratio = repeated / 2 if pages else None
    return pages, raw, ratio


def audit_file(path: Path, profile) -> FileAudit:
    data = path.read_bytes()
    record = FileAudit(
        file=path.name, size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest())

    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        record.supported = False
        record.error = f"白名单外格式：{path.suffix}"
        record.verdict = "REJECTED"
        record.notes.append("应由上传层拒识（unsupported_type），不应静默入库")
        return record

    try:
        sniff = sniff_file(path, path.name)
        record.kind = sniff.kind
        parser = get_parser(sniff.kind)
        model = parser.parse(path)
    except IngestError as exc:
        record.error = f"{type(exc).__name__}: {exc}"
        record.verdict = "REJECTED"
        return record
    except Exception as exc:  # noqa: BLE001 - 体检要覆盖未知异常
        record.error = f"{type(exc).__name__}: {exc}"
        record.verdict = "FAILED"
        return record

    record.parser = f"{model.parser.name}@{model.parser.version}"
    record.title = model.meta.title
    counts: dict[str, int] = {}
    for warning in model.warnings:
        counts[warning.code] = counts.get(warning.code, 0) + 1
    record.warning_counts = counts

    by_type: dict[str, int] = {}
    for block in model.blocks:
        by_type[block.type] = by_type.get(block.type, 0) + 1
        if block.type == "heading":
            record.headings += 1
        if block.table is not None:
            record.tables += 1
            record.table_rows += len(block.table.rows)
            record.table_cols = max(record.table_cols, len(block.table.columns))
    record.blocks = len(model.blocks)
    record.blocks_by_type = by_type

    text = "\n".join(_text_of(block) for block in model.blocks)
    record.chars = len(text)
    if text:
        record.cjk_ratio = round(len(CJK_RE.findall(text)) / max(1, len(text)), 3)
    record.zero_width = len(ZERO_WIDTH_RE.findall(text))
    record.control_chars = len(CONTROL_RE.findall(text))

    if sniff.kind == "pdf":
        pages, raw_chars, repeated = _pdf_raw_metrics(path)
        record.pdf_pages = pages
        record.pdf_raw_chars = raw_chars
        record.repeated_header_ratio = repeated
        if raw_chars:
            record.furniture_removed_ratio = round(
                1 - record.chars / raw_chars, 3)

    drafts = chunk_document(
        model, doc_id=record.sha256[:32], version_no=1,
        title=model.meta.title, dept="测试", classification=0,
        source_filename=path.name, profile=profile,
    )
    record.chunks = len(drafts)
    types: dict[str, int] = {}
    for draft in drafts:
        types[draft.chunk_type] = types.get(draft.chunk_type, 0) + 1
    record.chunk_types = types
    if drafts:
        lengths = sorted(len(draft.content) for draft in drafts)
        record.chunk_min = lengths[0]
        record.chunk_median = int(statistics.median(lengths))
        record.chunk_max = lengths[-1]

    record.verdict = _verdict(record)
    record.notes = _notes(record)
    return record


def _warning_label(record: FileAudit) -> str:
    return ", ".join(f"{code}×{count}" if count > 1 else code
                     for code, count in record.warning_counts.items()) or "—"


def _verdict(record: FileAudit) -> str:
    codes = record.warning_counts
    if "scanned_pdf" in codes:
        return "NEEDS_OCR"
    if "empty_document" in codes or record.chars < 50:
        return "EMPTY"
    if record.kind == "pdf" and (record.cjk_ratio or 0) < 0.30 and record.chars > 50:
        return "GARBLED"
    if "parse_partial" in codes or "table_no_header" in codes:
        return "WARN"
    return "OK"


def _notes(record: FileAudit) -> list[str]:
    notes: list[str] = []
    if record.kind == "pdf" and record.repeated_header_ratio:
        notes.append("检测到重复页眉/页脚，解析器已按版式噪声剔除")
    if record.kind == "pdf" and record.pdf_pages and record.chars:
        notes.append(f"平均每页 {record.chars / record.pdf_pages:.0f} 字")
    if record.zero_width or record.control_chars:
        notes.append("存在零宽/控制字符，清洗层应做规范化")
    if record.kind == "xlsx" and record.tables:
        notes.append(f"表格 {record.tables} 个，最大 {record.table_rows} 数据行 × {record.table_cols} 列")
    if record.chunk_max >= record.chunk_median * 3 and record.chunks > 2:
        notes.append("切片长度离散度大，建议检查是否被表格/长段落拉长")
    return notes


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(records: list[FileAudit], profile_id: str) -> str:
    lines = [
        "# 解析 / 清洗质量体检",
        "",
        f"- 切片 profile：{profile_id}",
        f"- 文件数：{len(records)}",
        "",
        "| 文件 | 格式 | 判定 | 字符 | 标题 | 表格 | 切片 | 切片长度 min/中位/max | 警告 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        if r.error and not r.supported:
            lines.append(f"| {r.file} | {Path(r.file).suffix} | {r.verdict} | - | - | - | - | - | {r.error} |")
            continue
        if r.error:
            lines.append(f"| {r.file} | {r.kind or '?'} | {r.verdict} | - | - | - | - | - | {r.error} |")
            continue
        lines.append(
            f"| {r.file} | {r.kind} | {r.verdict} | {r.chars} | {r.headings} "
            f"| {r.tables} | {r.chunks} | {r.chunk_min}/{r.chunk_median}/{r.chunk_max} "
            f"| {_warning_label(r)} |"
        )

    lines += ["", "## 明细", ""]
    for r in records:
        lines.append(f"### {r.file}")
        lines.append("")
        if r.error:
            lines.append(f"- 结果：{r.verdict}（{r.error}）")
            lines.append("")
            continue
        lines.append(f"- 解析器：{r.parser}")
        lines.append(f"- 块：{r.blocks}（{r.blocks_by_type}）")
        lines.append(f"- 字符：{r.chars}，中文占比 {_pct(r.cjk_ratio)}，"
                     f"零宽 {r.zero_width}，控制字符 {r.control_chars}")
        if r.kind == "pdf":
            lines.append(f"- 页数：{r.pdf_pages}，原始字符 {r.pdf_raw_chars}，"
                         f"清洗剔除比例 {_pct(r.furniture_removed_ratio)}，"
                         f"重复页眉页脚 {_pct(r.repeated_header_ratio)}")
        lines.append(f"- 切片：{r.chunks}（{r.chunk_types}），"
                     f"长度 {r.chunk_min}/{r.chunk_median}/{r.chunk_max}")
        for note in r.notes:
            lines.append(f"- 备注：{note}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(description="解析 / 清洗质量体检")
    parser.add_argument("path", nargs="?", default="data/public_samples")
    parser.add_argument("--profile", default="p400")
    parser.add_argument("--out", default=None, help="报告输出目录")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if root.is_file():
        files = [root]
    else:
        files = sorted(p for p in root.iterdir() if p.is_file()
                       and p.name != "manifest.json")
    if not files:
        print(f"未在 {root} 找到文件")
        return 1

    profile = resolve_profile(args.profile)
    records = [audit_file(path, profile) for path in files]

    out_dir = Path(args.out) if args.out else root / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"quality-{profile.profile_id}.json"
    md_path = out_dir / f"quality-{profile.profile_id}.md"
    json_path.write_text(
        json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
        encoding="utf-8")
    markdown = render_markdown(records, profile.profile_id)
    md_path.write_text(markdown, encoding="utf-8")

    print(markdown)
    counts: dict[str, int] = {}
    for record in records:
        counts[record.verdict] = counts.get(record.verdict, 0) + 1
    print(f"\n汇总：{counts}")
    print(f"报告：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
