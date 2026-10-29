from __future__ import annotations

import hashlib
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from ingest.config import Settings, get_settings
from ingest.ir import DocModel, Table

try:
    import jieba

    _JIEBA = True
except ImportError:  # pragma: no cover
    _JIEBA = False

# 与 main.CHINESE_SEPARATORS 保持一致，测试会校验防漂移
CHINESE_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]

MIN_CHUNK_CHARS = 20
MERGE_CHARS = 40
PREFIX_MAX = 80
CELL_MAX = 200
WIDE_TABLE_COLS = 12
MAX_COL_GROUP = 10
SAMPLE_HEAD = 20
SAMPLE_TAIL = 20
SAMPLE_MIDDLE = 40


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    chunk_type: str
    content: str
    content_tokens: str
    metadata: dict
    chunk_hash: str


def tokenize(text: str) -> list[str]:
    if _JIEBA:
        tokens = list(jieba.cut(text))
    else:  # pragma: no cover
        tokens = [ch for ch in text if not ch.isspace()]
    return [token.strip().lower() for token in tokens if token.strip()]


def _hash(chunk_type: str, content: str) -> str:
    return hashlib.sha256(f"{chunk_type}\x00{content}".encode("utf-8")).hexdigest()


def _prefix(title: str, path: list[str]) -> str:
    parts = [f"《{title}》", *[part for part in path if part.strip()]]
    return " > ".join(parts)[:PREFIX_MAX]


def _split_body(text: str, limit: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    splitter = RecursiveCharacterTextSplitter(
        separators=CHINESE_SEPARATORS,
        chunk_size=limit,
        chunk_overlap=min(overlap, max(0, limit // 4)),
        length_function=len,
    )
    pieces = [piece.strip() for piece in splitter.split_text(text) if piece.strip()]
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) < MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]}\n{piece}"
        else:
            merged.append(piece)
    return merged


def _column_label(column) -> str:
    return f"{column.name}（{column.unit}）" if column.unit else column.name


class _Builder:
    def __init__(self, ir: DocModel, *, doc_id: str, version_no: int, title: str,
                 dept: str, classification: int | None, source_filename: str,
                 settings: Settings) -> None:
        self.ir = ir
        self.doc_id = doc_id
        self.version_no = version_no
        self.title = title
        self.dept = dept
        self.classification = classification
        self.source_filename = source_filename
        self.settings = settings
        self.drafts: list[ChunkDraft] = []

    def add(self, chunk_type: str, content: str, metadata: dict) -> None:
        ordinal = len(self.drafts)
        meta = {
            "chunk_id": f"{self.doc_id}@v{self.version_no}#{ordinal}",
            "chunk_type": chunk_type,
            "title": self.title,
            "dept": self.dept,
            "classification": self.classification,
            "source": self.source_filename,
            "parser": f"{self.ir.parser.name}@{self.ir.parser.version}",
            **metadata,
        }
        tokens = " ".join(tokenize(content))
        self.drafts.append(ChunkDraft(
            ordinal=ordinal, chunk_type=chunk_type, content=content,
            content_tokens=tokens, metadata=meta,
            chunk_hash=_hash(chunk_type, content),
        ))

    def emit_text(self, body: str, path: list[str]) -> None:
        prefix = _prefix(self.title, path)
        limit = max(60, self.settings.chunk_size - len(prefix) - 1 - MIN_CHUNK_CHARS)
        for piece in _split_body(body, limit, self.settings.chunk_overlap):
            self.add("text", f"{prefix}\n{piece}", {
                "heading_path": list(path),
                "block_id": None,
                "sheet": None,
                "table_key": None,
                "cell_range": None,
                "row_range": None,
                "col_group": None,
                "page": None,
                "bbox": None,
            })

    def emit_table(self, table: Table, path: list[str], block_id: str) -> None:
        prefix = _prefix(self.title, path)
        pairs = self._data_pairs(table)
        self.add("table_summary", self._summary(table, prefix, pairs),
                 self._table_meta(table, path, block_id))
        if not pairs:
            return
        selected = self._select_pairs(pairs)
        sampled = len(selected) < len(pairs)
        for group in self._column_groups(table):
            for window in self._windows(selected):
                metadata = self._table_meta(table, path, block_id)
                metadata.update({
                    "row_range": f"{window[0][0]}-{window[-1][0]}",
                    "col_group": group["label"],
                })
                if sampled:
                    metadata["sampled"] = True
                if any(len(row[index]) > CELL_MAX
                       for _, row in window for index in group["indexes"]
                       if index < len(row)):
                    metadata["truncated"] = True
                self.add("table_rows",
                         self._window_content(table, prefix, group, window),
                         metadata)

    def _table_meta(self, table: Table, path: list[str], block_id: str) -> dict:
        return {
            "heading_path": list(path),
            "block_id": block_id,
            "sheet": table.sheet,
            "table_key": table.table_key,
            "cell_range": table.range,
            "row_range": None,
            "col_group": None,
            "page": table.page,
            "bbox": table.bbox,
        }

    def _data_pairs(self, table: Table) -> list[tuple[int, list[str]]]:
        start = table.data_start_row
        footer = set(table.footer_rows)
        return [(start + offset, row) for offset, row in enumerate(table.rows)
                if start + offset not in footer]

    def _select_pairs(self, pairs: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
        if len(pairs) <= self.settings.max_rows_per_table:
            return pairs
        head = pairs[:SAMPLE_HEAD]
        tail = pairs[-SAMPLE_TAIL:]
        middle = pairs[SAMPLE_HEAD:len(pairs) - SAMPLE_TAIL]
        if not middle:
            return head + tail
        step = max(1, len(middle) // SAMPLE_MIDDLE)
        return head + middle[::step][:SAMPLE_MIDDLE] + tail

    def _windows(self, pairs: list[tuple[int, list[str]]]):
        size = max(1, self.settings.table_rows_per_chunk)
        return [pairs[start:start + size] for start in range(0, len(pairs), size)]

    def _column_groups(self, table: Table) -> list[dict]:
        ncols = len(table.columns)
        if ncols <= WIDE_TABLE_COLS:
            return [{"indexes": list(range(ncols)), "label": None}]
        groups: list[dict] = []
        start = 1
        while start < ncols:
            end = min(ncols, start + MAX_COL_GROUP - 1)
            indexes = [0, *range(start, end)]
            label = (f"1-{end}" if start == 1
                     else f"1,{start + 1}-{end}" if start + 1 != end
                     else f"1,{start + 1}")
            groups.append({"indexes": indexes, "label": label})
            start = end
        return groups

    def _summary(self, table: Table, prefix: str,
                 pairs: list[tuple[int, list[str]]]) -> str:
        sheet = table.sheet or "工作表"
        lines = [
            prefix,
            f"文档《{self.title}》工作表「{sheet}」区域 {table.range}，"
            f"共 {len(pairs)} 行 × {len(table.columns)} 列。",
            "列：" + " | ".join(_column_label(column) for column in table.columns),
        ]
        footer = table.footer_values()
        if footer:
            lines.append(f"合计：{' | '.join(footer[0])}（第 {table.footer_rows[0]} 行）")
        return "\n".join(lines)

    def _window_content(self, table: Table, prefix: str, group: dict,
                        window: list[tuple[int, list[str]]]) -> str:
        indexes = group["indexes"]
        labels = [_column_label(table.columns[index]) for index in indexes]
        lines = [
            prefix,
            "列：" + " | ".join(labels),
            f"第 {window[0][0]}–{window[-1][0]} 行：",
        ]
        for _, row in window:
            cells = [row[index][:CELL_MAX] if index < len(row) else ""
                     for index in indexes]
            lines.append(" | ".join(cells))
        return "\n".join(lines)


def chunk_document(ir: DocModel, *, doc_id: str, version_no: int, title: str,
                   dept: str, classification: int | None,
                   source_filename: str,
                   settings: Settings | None = None) -> list[ChunkDraft]:
    settings = settings or get_settings()
    builder = _Builder(ir, doc_id=doc_id, version_no=version_no, title=title,
                       dept=dept, classification=classification,
                       source_filename=source_filename, settings=settings)
    stack: list[tuple[int, str]] = []
    section_path: list[str] = []
    buffer: list[str] = []
    carry = ""

    def flush() -> None:
        nonlocal carry
        text = "\n\n".join(part for part in buffer if part.strip()).strip()
        buffer.clear()
        if not text:
            return
        if carry:
            text = f"{carry}\n\n{text}".strip()
            carry = ""
        if len(text) < MERGE_CHARS:
            carry = text
            return
        builder.emit_text(text, section_path)

    for block in ir.blocks:
        if block.type == "heading":
            if not block.text.strip():
                continue
            flush()
            level = block.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text.strip()))
            section_path = [text for _, text in stack]
        elif block.type == "paragraph":
            if block.text.strip():
                buffer.append(block.text)
        elif block.type == "table" and block.table is not None:
            flush()
            if carry:
                builder.emit_text(carry, section_path)
                carry = ""
            builder.emit_table(block.table, block.path or section_path,
                               block.block_id)
    flush()
    if carry and len(carry) >= MIN_CHUNK_CHARS:
        builder.emit_text(carry, section_path)
    return builder.drafts
