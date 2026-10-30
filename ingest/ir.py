from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_A1_RANGE = re.compile(r"^[A-Z]+(\d+):[A-Z]+(\d+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParserInfo(_Strict):
    name: str
    version: str


class Warning(_Strict):
    code: str
    block_id: str | None = None
    detail: str = ""


class Column(_Strict):
    key: str
    name: str
    dtype: Literal["string", "number", "date", "bool"] = "string"
    unit: str | None = None
    source_names: list[str] = Field(default_factory=list)

    @field_validator("key", "name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("列 key/name 不能为空")
        return value


class Table(_Strict):
    table_key: str
    sheet: str | None = None
    range: str
    header_rows: list[int] = Field(default_factory=list)
    header_values: list[list[str]] = Field(default_factory=list)
    # 表头之前的原始行（标题行等）。复核台把表头起始行往前改时，靠它还原被“降级”成正文的行；
    # 没有它就只能在 full_grid 里丢掉 range 顶部，改表头的语义就错了。
    pre_rows: list[list[str]] = Field(default_factory=list)
    columns: list[Column]
    rows: list[list[str]]
    footer_rows: list[int] = Field(default_factory=list)
    # 正文中重复出现的表头行（绝对行号）。这些行仍保留在 rows 里，保证
    # rows[i] ↔ data_start_row + i 的绝对行号映射不被破坏；data_rows()
    # 在数据视图里剔除它们，footer/full_grid 因此仍与原始表格对齐。
    repeated_header_rows: list[int] = Field(default_factory=list)
    merge_ranges: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    source: Literal["heuristic", "llm", "template", "human"]
    flags: list[str] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    page: int | None = None
    bbox: list[float] | None = None

    @model_validator(mode="after")
    def _check(self):
        if not _A1_RANGE.match(self.range):
            raise ValueError(f"非法单元格区域: {self.range}")
        # full_grid() 的下标必须能按绝对行号映射（range 首行 + i），因此表头行必须连续；
        # 该检查同时覆盖递增与去重，否则 data_start_row 与 full_grid 会错位。
        if self.header_rows and self.header_rows != list(
            range(self.header_rows[0], self.header_rows[0] + len(self.header_rows))
        ):
            raise ValueError("header_rows 必须是从起始行开始的连续行号")
        if len(self.header_values) != len(self.header_rows):
            raise ValueError("header_values 行数必须等于 header_rows 行数")
        expected_pre = (
            self.header_rows[0] - self.range_start_row if self.header_rows else 0
        )
        if len(self.pre_rows) != expected_pre:
            raise ValueError(
                f"pre_rows 行数必须等于表头起始行之前的数据行数（期望 {expected_pre}）")
        if len({c.key for c in self.columns}) != len(self.columns):
            raise ValueError("columns.key 必须唯一")
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("rows 每行列数必须等于 columns 数")
        for row in self.header_values:
            if len(row) != len(self.columns):
                raise ValueError("header_values 每行列数必须等于 columns 数")
        for row in self.pre_rows:
            if len(row) != len(self.columns):
                raise ValueError("pre_rows 每行列数必须等于 columns 数")
        if set(self.header_rows) & set(self.footer_rows):
            raise ValueError("footer_rows 不能与 header_rows 重叠")
        if self.repeated_header_rows:
            if self.repeated_header_rows != sorted(set(self.repeated_header_rows)):
                raise ValueError("repeated_header_rows 必须递增且不重复")
            data_end = self.data_start_row + len(self.rows) - 1
            if (self.repeated_header_rows[0] < self.data_start_row
                    or self.repeated_header_rows[-1] > data_end):
                raise ValueError("repeated_header_rows 必须落在数据区内")
            if set(self.repeated_header_rows) & (
                    set(self.header_rows) | set(self.footer_rows)):
                raise ValueError("repeated_header_rows 不能与表头/表尾行重叠")
        return self

    @property
    def range_start_row(self) -> int:
        return int(_A1_RANGE.match(self.range).group(1))

    @property
    def data_start_row(self) -> int:
        return max(self.header_rows) + 1 if self.header_rows else self.range_start_row

    def data_rows(self) -> list[list[str]]:
        start = self.data_start_row
        excluded = set(self.footer_rows) | set(self.repeated_header_rows)
        return [row for i, row in enumerate(self.rows) if start + i not in excluded]

    def footer_values(self) -> list[list[str]]:
        start = self.data_start_row
        return [row for i, row in enumerate(self.rows) if start + i in self.footer_rows]

    def full_grid(self) -> list[list[str]]:
        """从 range 第一行起的完整单元格网格，供 rebuild_table 按绝对行号重切表头。"""
        return ([list(row) for row in self.pre_rows]
                + [list(row) for row in self.header_values]
                + [list(row) for row in self.rows])


class Block(_Strict):
    block_id: str
    type: Literal["heading", "paragraph", "table", "image"]
    level: int | None = None
    text: str = ""
    path: list[str] = Field(default_factory=list)
    table: Table | None = None
    page: int | None = None
    bbox: list[float] | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.type == "heading" and (self.level is None or not 1 <= self.level <= 6):
            raise ValueError("heading 必须有 1..6 的 level")
        if self.type == "table" and self.table is None:
            raise ValueError("table block 必须带 table")
        if self.type != "table" and self.table is not None:
            raise ValueError("非 table block 不能带 table")
        return self


class DocMeta(_Strict):
    title: str
    dept: str | None = None
    classification: None = None  # 解析器永不自动定密级
    effective_date: str | None = None
    source_filename: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not _SHA256.match(value):
            raise ValueError("sha256 必须是 64 位小写十六进制")
        return value


class DocModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    parser: ParserInfo
    meta: DocMeta
    blocks: list[Block] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        block_ids = [b.block_id for b in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block_id 必须唯一")
        keys = [b.table.table_key for b in self.blocks if b.table is not None]
        if len(keys) != len(set(keys)):
            raise ValueError("table_key 必须唯一")
        return self

    def tables(self) -> list[Table]:
        return [b.table for b in self.blocks if b.table is not None]

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, text: str) -> "DocModel":
        return cls.model_validate_json(text)
