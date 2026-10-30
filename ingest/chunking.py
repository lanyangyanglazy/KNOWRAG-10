from __future__ import annotations

import hashlib
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from ingest.config import Settings, get_settings
from ingest.ir import DocModel, Table
from ingest.profiles import ChunkProfile, profile_from_settings, resolve_profile

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


def _row_runs(numbers: list[int]) -> str:
    """把升序绝对行号压成紧凑连续区间，如 `22-31、100-109`。"""
    spans: list[tuple[int, int]] = []
    for number in numbers:
        if spans and number == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], number)
        else:
            spans.append((number, number))
    return "、".join(f"{low}-{high}" if low != high else str(low)
                    for low, high in spans)


def _split_body(text: str, limit: int, overlap: int, *,
                min_chars: int = MIN_CHUNK_CHARS) -> list[str]:
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
        if (merged and len(piece) < min_chars
                and len(merged[-1]) + 1 + len(piece) <= limit):
            merged[-1] = f"{merged[-1]}\n{piece}"
        else:
            merged.append(piece)
    return merged


def _column_label(column) -> str:
    return f"{column.name}（{column.unit}）" if column.unit else column.name


class _Builder:
    def __init__(self, ir: DocModel, *, doc_id: str, version_no: int, title: str,
                 dept: str, classification: int | None, source_filename: str,
                 settings: Settings, profile: ChunkProfile,
                 origin_id: str | None = None) -> None:
        self.ir = ir
        # psycopg 返回的 UUID 不是 JSON 可序列化类型；metadata 要落 jsonb，统一转字符串
        self.doc_id = str(doc_id)
        self.origin_id = origin_id
        self.version_no = version_no
        self.title = title
        self.dept = dept
        self.classification = classification
        self.source_filename = source_filename
        self.settings = settings
        self.profile = profile
        self.drafts: list[ChunkDraft] = []

    def add(self, chunk_type: str, content: str, metadata: dict) -> None:
        ordinal = len(self.drafts)
        meta = {
            # chunk_id 必须保持 UUID 形态：回源路由按 UUID@v#n 解析定位
            "chunk_id": f"{self.doc_id}@v{self.version_no}#{ordinal}",
            # doc_id 用于评测金标匹配：导入语料取源系统编号，上传文档退回 UUID
            "doc_id": self.origin_id or self.doc_id,
            "chunk_type": chunk_type,
            # 切片参数留痕：换 profile 后能解释"为什么召回变了"
            "chunk_profile": self.profile.profile_id,
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
        prefix = _prefix(self.title, path) if self.profile.title_prefix else ""
        chunk_size = self.profile.chunk_size
        if chunk_size < len(prefix) + 2:
            prefix = prefix[:max(0, chunk_size - 2)]
        limit = max(1, chunk_size - len(prefix) - 1) if prefix else chunk_size
        for piece in _split_body(body, limit, self.profile.chunk_overlap,
                                 min_chars=self.profile.min_chunk_chars):
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

    def emit_table(self, table: Table, path: list[str], block_id: str,
                   lead: str = "") -> None:
        prefix = _prefix(self.title, path)
        pairs = self._data_pairs(table)
        self.add("table_summary", self._summary(table, prefix, pairs, lead),
                 self._table_meta(table, path, block_id))
        if not pairs:
            return
        selected = self._select_pairs(pairs)
        sampled = ({row for row, _ in selected}
                   != {row for row, _ in pairs})
        for group in self._column_groups(table):
            for window in self._windows(selected):
                runs = _row_runs([row for row, _ in window])
                metadata = self._table_meta(table, path, block_id)
                metadata.update({
                    "row_range": runs,
                    "col_group": group["label"],
                })
                if sampled:
                    metadata["sampled"] = True
                if any(len(row[index]) > CELL_MAX
                       for _, row in window for index in group["indexes"]
                       if index < len(row)):
                    metadata["truncated"] = True
                self.add("table_rows",
                         self._window_content(table, prefix, group, window, runs),
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
        excluded = set(table.footer_rows) | set(table.repeated_header_rows)
        return [(start + offset, row) for offset, row in enumerate(table.rows)
                if start + offset not in excluded]

    def _select_pairs(self, pairs: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
        if len(pairs) <= self.profile.max_rows_per_table:
            return pairs
        if len(pairs) <= SAMPLE_HEAD + SAMPLE_TAIL:
            return pairs
        head = pairs[:SAMPLE_HEAD]
        tail = pairs[-SAMPLE_TAIL:]
        middle = pairs[SAMPLE_HEAD:len(pairs) - SAMPLE_TAIL]
        step = max(1, len(middle) // SAMPLE_MIDDLE)
        chosen = (
            {row for row, _ in head}
            | {row for row, _ in middle[::step][:SAMPLE_MIDDLE]}
            | {row for row, _ in tail})
        return [pair for pair in pairs if pair[0] in chosen]

    def _windows(self, pairs: list[tuple[int, list[str]]]):
        size = max(1, self.profile.table_rows_per_chunk)
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
                 pairs: list[tuple[int, list[str]]], lead: str = "") -> str:
        sheet = table.sheet or "工作表"
        lines = [prefix]
        if lead.strip():
            lines.append(lead.strip())
        lines.append(
            f"文档《{self.title}》工作表「{sheet}」区域 {table.range}，"
            f"共 {len(pairs)} 行 × {len(table.columns)} 列。")
        lines.append("列：" + " | ".join(
            _column_label(column) for column in table.columns))
        footer = table.footer_values()
        if footer:
            lines.append(f"合计：{' | '.join(footer[0])}（第 {table.footer_rows[0]} 行）")
        return "\n".join(lines)

    def _window_content(self, table: Table, prefix: str, group: dict,
                        window: list[tuple[int, list[str]]],
                        runs: str) -> str:
        indexes = group["indexes"]
        labels = [_column_label(table.columns[index]) for index in indexes]
        lines = [
            prefix,
            "列：" + " | ".join(labels),
            f"第 {runs} 行：",
        ]
        for _, row in window:
            cells = [row[index][:CELL_MAX] if index < len(row) else ""
                     for index in indexes]
            lines.append(" | ".join(cells))
        return "\n".join(lines)


def chunk_document(ir: DocModel, *, doc_id: str, version_no: int, title: str,
                   dept: str, classification: int | None,
                   source_filename: str,
                   settings: Settings | None = None,
                   profile: ChunkProfile | str | None = None,
                   origin_id: str | None = None) -> list[ChunkDraft]:
    settings = settings or get_settings()
    # 显式 profile 优先；否则由 settings 推导（settings 的默认值已按 profile 填好，
    # 只有显式环境变量覆盖时才会出现 ``xxx+env`` 后缀）。
    chunk_profile = (resolve_profile(profile) if profile is not None
                     else profile_from_settings(settings))
    builder = _Builder(ir, doc_id=doc_id, version_no=version_no, title=title,
                       dept=dept, classification=classification,
                       source_filename=source_filename, settings=settings,
                       profile=chunk_profile, origin_id=origin_id)
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
        if len(text) < builder.profile.merge_chars:
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
            builder.emit_table(block.table, block.path or section_path,
                               block.block_id, carry)
            carry = ""
    flush()
    if carry.strip():
        builder.emit_text(carry, section_path)
    return builder.drafts
