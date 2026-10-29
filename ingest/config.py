from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    db_url: str
    upload_dir: Path
    storage_backend: str = "fs"
    max_file_mb: int = 50
    max_pdf_pages: int = 2000
    max_rows_per_table: int = 5000
    max_cells_per_sheet: int = 200_000
    max_cells_per_workbook: int = 2_000_000
    include_hidden_sheets: bool = False
    chunk_size: int = 400
    chunk_overlap: int = 60
    table_rows_per_chunk: int = 10
    header_accept: float = 0.75
    header_margin: float = 0.12
    header_llm: str = "auto"
    embed_dim: int = 1024
    embed_model: str = "bge-m3"
    ollama_base_url: str = "http://127.0.0.1:11434"
    embed_qps: int = 30
    dense_top: int = 50
    sparse_top: int = 50
    fusion_top: int = 5
    workers: int = 2
    queue_max: int = 200

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def header_llm_enabled(self) -> bool:
        # auto 需要评测门禁；P0 只有显式 always 才调用 LLM（见计划差异 4）
        return self.header_llm == "always"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        db_url=os.getenv("INGEST_DB_URL", ""),
        upload_dir=Path(os.getenv("INGEST_UPLOAD_DIR", str(BASE_DIR / "data" / "uploads"))),
        storage_backend=os.getenv("INGEST_STORAGE", "fs"),
        max_file_mb=_int("INGEST_MAX_FILE_MB", 50),
        max_pdf_pages=_int("INGEST_MAX_PDF_PAGES", 2000),
        max_rows_per_table=_int("INGEST_MAX_ROWS_PER_TABLE", 5000),
        max_cells_per_sheet=_int("INGEST_MAX_CELLS_PER_SHEET", 200_000),
        max_cells_per_workbook=_int("INGEST_MAX_CELLS_PER_WORKBOOK", 2_000_000),
        include_hidden_sheets=os.getenv("INGEST_INCLUDE_HIDDEN_SHEETS", "0") == "1",
        chunk_size=_int("INGEST_CHUNK_SIZE", 400),
        chunk_overlap=_int("INGEST_CHUNK_OVERLAP", 60),
        table_rows_per_chunk=_int("INGEST_TABLE_ROWS_PER_CHUNK", 10),
        header_accept=_float("HEADER_ACCEPT", 0.75),
        header_margin=_float("HEADER_MARGIN", 0.12),
        header_llm=os.getenv("HEADER_LLM", "auto"),
        embed_dim=_int("EMBED_DIM", 1024),
        embed_model=os.getenv("INGEST_EMBED_MODEL", "bge-m3"),
        ollama_base_url=os.getenv("INGEST_OLLAMA_BASE_URL",
                                  "http://127.0.0.1:11434"),
        embed_qps=_int("EMBED_QPS", 30),
        dense_top=_int("DENSE_TOP", 50),
        sparse_top=_int("SPARSE_TOP", 50),
        fusion_top=_int("FUSION_TOP", 5),
        workers=_int("INGEST_WORKERS", 2),
        queue_max=_int("INGEST_QUEUE_MAX", 200),
    )
