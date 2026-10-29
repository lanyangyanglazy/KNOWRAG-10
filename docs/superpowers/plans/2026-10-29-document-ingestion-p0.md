# 文档摄取子系统 P0 实现计划（M1 + M2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有教学主线（`main.py` + FAISS）的前提下，交付一个可私有化部署的文档摄取与检索后端：上传 Word/Excel/PDF → 安全扫描 → 解析为 DocModel → Excel 表头识别/复核 → 切片/嵌入 → PostgreSQL + pgvector 权限感知检索 → 引用回源，并把现有 `data/gov_docs` 完整导入。

**Architecture:** 新增独立包 `ingest/`（解析、队列、流水线、切片、嵌入、检索）与 `app/routers/`（上传、复核、任务、回源 API）。PostgreSQL 16 + pgvector 作为唯一事实来源，`documents / doc_versions / chunks` 分层；异步 worker 用 `SELECT … FOR UPDATE SKIP LOCKED` 消费 `jobs`。现有 `app/rag_service.py` 增加 `RETRIEVAL_BACKEND=pg|faiss` 分支：设置 `INGEST_DB_URL` 时默认走 pg，未设置时保持 FAISS，现有 73 个测试不受影响。

**Tech Stack:** Python 3.11+（本机 3.14）、FastAPI、psycopg 3 + psycopg_pool、pgvector 0.8、python-docx、openpyxl、PyMuPDF、pdfplumber、filetype、jieba、Ollama（bge-m3 / qwen3:4b）、pytest、Docker Compose。

**Spec:** `docs/文档摄取子系统设计.md`（下称“设计文档”）

## Global Constraints

以下约束对每个任务隐式生效，任务内不再重复：

- **离线私有化单租户**：运行时零外联；模型、LibreOffice、OCR（P1）全部本机/内网。任何可选依赖不可在导入阶段发起网络请求。
- **权限 fail-closed**：`classification IS NULL` 一律不可检索；检索 SQL 必须前置过滤 `active / status='active' / deleted_at IS NULL / classification ≤ 用户上限 / (classification=0 OR dept=用户部门)`，禁止“先检索后删结果”。
- **未复核、未定级、已下线、失败、隔离的版本不可检索**：只有 `documents.status='active'` 且 `chunks.active=true` 的当前版本切片进入候选。
- **密级**沿用 `公开=0 / 内部=1 / 秘密=2`；秘密级必须两名不同且有秘密权限的用户先后确认，上传者可作其一。
- **距离度量**沿用 L2 与 `RAG_MAX_DISTANCE=0.78`；迁移后必须用新语料重新校准，未校准前不得对外宣称阈值有效。
- **对外切片 ID** 固定为 `{document_id}@v{version_no}#{ordinal}`；`ordinal` 在同一版本内按文档顺序确定性分配。
- **错误码**：400 `unsupported_type`、413 `too_large`、409 `duplicate_content`、422 `review_required`、423 `quarantined`、429 `queue_full`、503 `model_unavailable`（另有 400 `invalid_metadata`、409 `document_inactive` 作为补充）。
- **现有测试不回归**：`tests/test_main.py` + `tests/test_web.py` 共 73 个测试必须保持全绿；修改 `app/server.py`、`app/rag_service.py` 时只做加法或兼容性包装。
- **数据库测试**必须设置 `INGEST_TEST_DB_URL`；验收/CI 运行时设置 `INGEST_REQUIRE_DB=1`，此时 skip 数量必须为 0。
- **上限**：单文件 50 MB、单 PDF 2000 页由上传层返回 413；单元格/行数上限在解析层截断并写 `truncated` 警告（设计 §7.3），不返回 413。
- **时间**统一存 UTC（`timestamptz`），审计一行一个 JSON 对象；日志与文件名只做展示，落盘一律用 `sha256`。
- **Windows 开发机**：命令用 Git Bash 执行；LibreOffice 可能不在 PATH，相关测试用 `pytest.mark.skipif` 跳过，但不得让生产代码静默降级。
- **不引入 Alembic**：P0 用幂等的 `ingest/schema.sql` + `migrate()`；改表必须新增迁移语句，不允许在运行期 `DROP TABLE`。

## 与设计文档的显式差异（需要评审时知情）

这些差异是为了让设计可落地而补的，均不改变安全边界：

1. `documents` 增加 `effective_date date`。设计 §11 的确认请求包含该字段，但 §5.2 表结构漏列；否则该字段无处持久化。
2. `Table` 增加 `header_values: list[list[str]]`。复核台需要“改表头起始行”后重切，仅靠 `columns + rows` 无法还原原始表头单元格。
3. `jobs.max_attempts` 语义定为**首次执行 + 最多 max_attempts 次重试**（默认 3 次重试），这样退避 30 s / 2 min / 10 min 才有意义；`attempts > max_attempts` 才进入 `failed`。
4. `HEADER_LLM=auto` 在评测门禁通过前等同 `off`；只有显式 `always` 才调用 LLM。设计 §8.5 要求“不达标自动关闭”，P0 没有评测门禁，因此不能默认开启。
5. 现有语料导入走 `trusted_import=true`（设计 §14.1 “免解析复核”），直接进入嵌入与激活；该入口只存在于 CLI/内部任务，不经 HTTP 暴露。
6. `RETRIEVAL_BACKEND` 的解析顺序：显式参数 > 环境变量 > （设置了 `INGEST_DB_URL` 则 `pg`，否则 `faiss`）。这样现有测试无需注入数据库即可保持 FAISS 路径。
7. `purge` 完成后保留 `documents` 墓碑行（`status='deleting'`, `deleted_at` 非空），删除切片、版本与原件；审计仍可按 document_id 追溯。
8. `User` 增加 `roles: tuple[str, ...] = ()`，演示账号映射为 `uploader / reviewer / admin`；现有 5 参数构造保持兼容。

## 文件结构（先锁定边界，再拆任务）

```text
ingest/
  __init__.py
  config.py            # 环境变量 → Settings（lru_cache），所有上限/阈值唯一来源
  errors.py            # IngestError 及 400/409/413/422/423/429/503 子类
  db.py                # 连接池、register_vector、migrate()
  schema.sql           # 全部 DDL 与索引（幂等）
  ir.py                # DocModel / Table / Block（pydantic），校验与 JSON 往返
  sniff.py             # 魔数、大小、zip bomb、路径穿越、宏、加密
  storage.py           # 内容寻址 FileStore
  convert.py           # LibreOffice headless 转换/重算
  repository.py        # documents / doc_versions / review_items 的 SQL 封装
  queue.py             # jobs 入队/抢占/心跳/重试/回收
  worker.py            # 单任务执行、dead-letter 钩子、常驻循环
  pipeline.py          # parse / embed / activate / purge 处理器
  chunking.py          # 文本切片、表格摘要、行窗口、宽表列组
  embedding.py         # Embedder 协议、OllamaEmbedder、批量嵌入
  retrieval.py         # pgvector ANN + FTS + ACL + RRF + 邻片扩展 + 拒答
  activation.py        # activate / deactivate / purge 事务
  review_service.py    # 复核、双人确认、waive、模板保存
  import_gov_docs.py   # 现有 md 导入 CLI（trusted_import）
  parsers/
    __init__.py        # 解析器注册表 get_parser(kind)
    base.py            # Parser 协议、ParseError、SniffResult 复用
    xlsx_reader.py     # 两遍 openpyxl → SheetGrid/WorkbookGrid
    regions.py         # 区域切分
    header_rules.py    # 七特征打分、候选、接受条件、列名合成
    header_llm.py      # LLM 兜底、JSON 契约校验、调用限额
    templates.py       # 版式模板签名/匹配/应用
    table_build.py     # 规范化 Table（类型/单位/合计行/重复表头）
    docx_parser.py     # .docx + .doc（转换）
    pdf_parser.py      # 文本层、页眉页脚剔除、表格、扫描件标记
    markdown_parser.py # 现有 gov_docs 导入
app/
  audit.py             # write_audit（server 与 worker 共用）
  deps.py              # current_user / get_db / Cookie 序列化
  routers/
    __init__.py
    documents.py       # 上传、列表、详情、下线、删除
    review.py          # 复核台、confirm/approve/waive、preview
    jobs.py            # 任务状态、SSE
    templates.py       # 模板列表/创建
    source.py          # 引用回源
  static/
    index.html         # 增加导航与两个视图容器
    ingest.js          # 文档库 + 复核台 + 回源（无外部依赖）
    ingest.css
scripts/
  eval_retrieval.py    # Recall@5 / 引用准确率 / 拒答准确率
docs/eval/
  gov_questions.yaml   # 种子问题集
tests/ingest/
  conftest.py
  builders.py          # 测试用 docx/xlsx/pdf 生成器
  test_config.py test_schema.py test_ir.py test_sniff.py test_storage.py
  test_queue.py test_xlsx_reader.py test_regions.py test_header_rules.py
  test_header_llm.py test_templates.py test_table_build.py
  test_docx_parser.py test_pdf_parser.py test_markdown_parser.py
  test_pipeline.py test_documents_api.py test_review_api.py
  test_jobs_source_api.py test_chunking.py test_embedding.py
  test_activation.py test_retrieval.py test_rag_service_pg.py
  test_import_gov_docs.py test_eval_metrics.py test_end_to_end.py
```

## 任务索引

| # | 任务 | 里程碑 | 依赖 |
|---|---|---|---|
| 1 | 依赖、Compose、配置、DB 与 schema、审计抽取 | M1 | — |
| 2 | DocModel IR | M1 | 1 |
| 3 | 安全嗅探与内容寻址存储 | M1 | 1 |
| 4 | 任务队列与 worker 循环 | M1 | 1 |
| 5 | XLSX 网格读取 | M1 | 2,3 |
| 6 | 区域切分 | M1 | 5 |
| 7 | 表头规则打分与列名合成 | M1 | 5,6 |
| 8 | 版式模板与 LLM 兜底 | M1 | 7 |
| 9 | 表格规范化 | M1 | 7 |
| 10 | DOCX 解析器 | M1 | 2,3,9 |
| 11 | PDF 解析器 | M1 | 2,3,9 |
| 12 | Markdown 解析器 | M1 | 2 |
| 13 | 解析流水线（parse job） | M1 | 4,9,10,11,12 |
| 14 | 上传与文档 API | M1 | 3,4,13 |
| 15 | 复核 API（含秘密级双人） | M1 | 13,14 |
| 16 | 任务查询、健康检查、引用回源 | M1 | 14 |
| 17 | 文档库与复核台前端 | M1 | 14,15,16 |
| 18 | 切片（文本/表格摘要/行窗口） | M2 | 2 |
| 19 | 嵌入 job 与 chunks 写入 | M2 | 13,18 |
| 20 | 激活、下线、清理 | M2 | 19 |
| 21 | pgvector 混合检索与拒答 | M2 | 18,19 |
| 22 | RagService 后端切换与现有语料导入 | M2 | 20,21 |
| 23 | 评测基准与全链路验收 | M2 | 22 |

---

### Task 1: 依赖、Compose、配置、DB、schema 与审计抽取

**Files:**
- Modify: `pyproject.toml`, `.env.example`, `app/server.py`
- Create: `docker-compose.yml`, `infra/init/01-test-db.sql`, `ingest/__init__.py`, `ingest/config.py`, `ingest/errors.py`, `ingest/db.py`, `ingest/schema.sql`, `app/audit.py`, `tests/ingest/__init__.py`, `tests/ingest/conftest.py`, `tests/ingest/test_config.py`, `tests/ingest/test_schema.py`, `tests/test_audit.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `ingest.config.Settings`（字段见下）、`ingest.config.get_settings() -> Settings`（`@lru_cache`，测试可 `get_settings.cache_clear()`）。
  - `ingest.errors.IngestError`、`UnsupportedType`、`TooLarge`、`DuplicateContent`、`ReviewRequired`、`Quarantined`、`QueueFull`、`ModelUnavailable`、`ParseError`。
  - `ingest.db.get_pool()`, `ingest.db.connection()`, `ingest.db.migrate(conn)`, `ingest.db.reset_pool_for_tests()`。
  - `app.audit.write_audit(record: dict, audit_file: Path | None = None) -> None`。

- [ ] **Step 1: 写失败测试**

`tests/ingest/test_config.py`：

```python
from ingest.config import get_settings


def test_defaults_match_design(monkeypatch):
    for key in ("INGEST_DB_URL", "INGEST_MAX_FILE_MB", "EMBED_DIM", "DENSE_TOP",
                "HEADER_ACCEPT", "HEADER_LLM", "INGEST_QUEUE_MAX"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.db_url == ""
    assert s.max_file_mb == 50
    assert s.max_pdf_pages == 2000
    assert s.max_rows_per_table == 5000
    assert s.max_cells_per_sheet == 200_000
    assert s.chunk_size == 400 and s.chunk_overlap == 60
    assert s.embed_dim == 1024
    assert s.dense_top == 50 and s.sparse_top == 50 and s.fusion_top == 5
    assert s.header_accept == 0.75 and s.header_margin == 0.12
    assert s.header_llm == "auto"
    assert s.header_llm_enabled is False  # auto 在门禁前等同 off
    assert s.queue_max == 200


def test_env_override(monkeypatch):
    monkeypatch.setenv("INGEST_MAX_FILE_MB", "8")
    monkeypatch.setenv("HEADER_LLM", "always")
    get_settings.cache_clear()
    s = get_settings()
    assert s.max_file_mb == 8
    assert s.header_llm_enabled is True
```

`tests/ingest/conftest.py`：

```python
import os

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from ingest.db import migrate

TEST_DB_URL = os.getenv("INGEST_TEST_DB_URL", "")
TRUNCATE_SQL = (
    "TRUNCATE review_approvals, review_items, chunks, doc_tables, doc_versions, "
    "documents, jobs, layout_templates RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session")
def db_url():
    if not TEST_DB_URL:
        if os.getenv("INGEST_REQUIRE_DB"):
            pytest.fail("INGEST_REQUIRE_DB=1 但未设置 INGEST_TEST_DB_URL")
        pytest.skip("未设置 INGEST_TEST_DB_URL；先 docker compose up -d postgres")
    return TEST_DB_URL


@pytest.fixture(scope="session")
def migrated(db_url):
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        migrate(conn)
    return db_url


@pytest.fixture()
def conn(migrated):
    with psycopg.connect(migrated, row_factory=dict_row) as conn:
        register_vector(conn)
        conn.execute(TRUNCATE_SQL)
        conn.commit()
        yield conn
        conn.rollback()
```

`tests/ingest/test_schema.py`：

```python
from ingest.db import migrate


def test_migrate_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    names = {row["tablename"] for row in rows}
    assert {
        "documents", "doc_versions", "chunks", "doc_tables", "jobs",
        "review_items", "review_approvals", "layout_templates",
    } <= names


def test_migrate_is_idempotent(conn):
    migrate(conn)
    migrate(conn)
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
```

`tests/test_audit.py`：

```python
import json

from app import audit


def test_write_audit_shared_module(tmp_path):
    target = tmp_path / "audit.jsonl"
    audit.write_audit({"action": "upload", "user": "zhang"}, audit_file=target)
    record = json.loads(target.read_text(encoding="utf-8").strip())
    assert record["action"] == "upload"
    assert record["ts"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_config.py tests/ingest/test_schema.py tests/test_audit.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'ingest'` / `No module named 'app.audit'`。

- [ ] **Step 3: 写实现**

`pyproject.toml` 的 `dependencies` 追加（保持既有项不动）：

```toml
    "filetype>=1.2",
    "openpyxl>=3.1.5",
    "pgvector>=0.3.6",
    "psycopg[binary,pool]>=3.2",
    "pydantic>=2.9",
    "pymupdf>=1.26",
    "pdfplumber>=0.11.4",
    "python-docx>=1.1.2",
    "python-multipart>=0.0.20",
```

`docker-compose.yml`：

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    container_name: rag-postgres
    environment:
      POSTGRES_USER: rag
      POSTGRES_PASSWORD: rag
      POSTGRES_DB: rag
    ports:
      - "5433:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./infra/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U rag -d rag"]
      interval: 5s
      timeout: 3s
      retries: 20

volumes:
  pgdata:
```

`infra/init/01-test-db.sql`：

```sql
CREATE DATABASE rag_test;
```

`.env.example` 追加：

```dotenv
# ===== 文档摄取（P0） =====
# 设置后 app/rag_service.py 默认走 pg；不设置则保持 FAISS
INGEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test
INGEST_UPLOAD_DIR=data/uploads
INGEST_STORAGE=fs
INGEST_MAX_FILE_MB=50
INGEST_QUEUE_MAX=200
INGEST_WORKERS=2
# 本机 LibreOffice 路径（Windows 示例）；不设置则查 PATH
# SOFFICE_BIN=C:\Program Files\LibreOffice\program\soffice.exe
```

`ingest/config.py`：

```python
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
        embed_qps=_int("EMBED_QPS", 30),
        dense_top=_int("DENSE_TOP", 50),
        sparse_top=_int("SPARSE_TOP", 50),
        fusion_top=_int("FUSION_TOP", 5),
        workers=_int("INGEST_WORKERS", 2),
        queue_max=_int("INGEST_QUEUE_MAX", 200),
    )
```

`ingest/errors.py`：

```python
from __future__ import annotations


class IngestError(Exception):
    code = "ingest_error"
    http_status = 500

    def __init__(self, message: str = "", **detail):
        super().__init__(message or self.code)
        self.detail = detail


class UnsupportedType(IngestError):
    code, http_status = "unsupported_type", 400


class TooLarge(IngestError):
    code, http_status = "too_large", 413


class DuplicateContent(IngestError):
    code, http_status = "duplicate_content", 409


class ReviewRequired(IngestError):
    code, http_status = "review_required", 422


class Quarantined(IngestError):
    code, http_status = "quarantined", 423


class QueueFull(IngestError):
    code, http_status = "queue_full", 429


class ModelUnavailable(IngestError):
    code, http_status = "model_unavailable", 503


class ParseError(Exception):
    """解析器内部失败；由 worker 转成 failed + parse_warning。"""
```

`ingest/schema.sql`（完整 DDL；`documents.effective_date` 与 `header_values` 为计划差异 1、2）：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title               text NOT NULL,
  dept                text NOT NULL,
  classification      smallint CHECK (classification IN (0,1,2)),
  effective_date      date,
  status              text NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','active','inactive','deleting')),
  current_version_id  uuid,
  created_by          text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  deleted_at          timestamptz
);

CREATE TABLE IF NOT EXISTS doc_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id     uuid NOT NULL REFERENCES documents(id),
  version_no      int NOT NULL,
  sha256          char(64) NOT NULL,
  filename        text NOT NULL,
  mime            text NOT NULL,
  size_bytes      bigint NOT NULL,
  storage_path    text NOT NULL,
  parser          text NOT NULL,
  parser_version  text NOT NULL,
  status          text NOT NULL CHECK (status IN
                  ('uploaded','scanning','parsing','needs_review','embedding',
                   'ready','failed','quarantined')),
  ir              jsonb,
  warnings        jsonb NOT NULL DEFAULT '[]',
  error           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  activated_at    timestamptz,
  UNIQUE (document_id, version_no),
  UNIQUE (document_id, sha256)
);

CREATE TABLE IF NOT EXISTS chunks (
  id              bigserial PRIMARY KEY,
  document_id     uuid NOT NULL REFERENCES documents(id),
  version_id      uuid NOT NULL REFERENCES doc_versions(id),
  ordinal         int NOT NULL,
  chunk_type      text NOT NULL CHECK (chunk_type IN
                  ('text','table_summary','table_rows')),
  content         text NOT NULL,
  content_tokens  text NOT NULL,
  tsv             tsvector,
  embedding       vector(1024),
  embed_model     text NOT NULL,
  dept            text NOT NULL,
  classification  smallint,
  metadata        jsonb NOT NULL DEFAULT '{}',
  chunk_hash      char(64) NOT NULL,
  active          boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (version_id, ordinal)
);

CREATE TABLE IF NOT EXISTS doc_tables (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_id    uuid NOT NULL REFERENCES doc_versions(id),
  table_key     text NOT NULL,
  sheet         text,
  cell_range    text,
  header_rows   int[] NOT NULL,
  columns       jsonb NOT NULL,
  row_count     int NOT NULL,
  footer_rows   int[] NOT NULL DEFAULT '{}',
  confidence    real NOT NULL,
  source        text NOT NULL,
  storage_ref   text,
  UNIQUE (version_id, table_key)
);

CREATE TABLE IF NOT EXISTS jobs (
  id            bigserial PRIMARY KEY,
  kind          text NOT NULL,
  payload       jsonb NOT NULL,
  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed')),
  priority      int NOT NULL DEFAULT 100,
  attempts      int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 3,
  run_after     timestamptz NOT NULL DEFAULT now(),
  locked_by     text,
  locked_at     timestamptz,
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

CREATE TABLE IF NOT EXISTS review_items (
  id            bigserial PRIMARY KEY,
  version_id    uuid NOT NULL REFERENCES doc_versions(id),
  kind          text NOT NULL CHECK (kind IN
                ('structure','classification','metadata','parse_warning')),
  status        text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','resolved','waived')),
  payload       jsonb NOT NULL,
  resolved_by   text,
  resolved_at   timestamptz,
  note          text
);

CREATE TABLE IF NOT EXISTS review_approvals (
  id              bigserial PRIMARY KEY,
  review_item_id  bigint NOT NULL REFERENCES review_items(id),
  user_id         text NOT NULL,
  decision        text NOT NULL CHECK (decision IN ('approve','reject')),
  note            text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_item_id, user_id)
);

CREATE TABLE IF NOT EXISTS layout_templates (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  scope_dept    text,
  signature     jsonb NOT NULL,
  config        jsonb NOT NULL,
  hits          int NOT NULL DEFAULT 0,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_vec_l0 ON chunks USING hnsw (embedding vector_l2_ops)
  WHERE active AND classification = 0;
CREATE INDEX IF NOT EXISTS chunks_vec_l1 ON chunks USING hnsw (embedding vector_l2_ops)
  WHERE active AND classification = 1;
CREATE INDEX IF NOT EXISTS chunks_vec_l2 ON chunks USING hnsw (embedding vector_l2_ops)
  WHERE active AND classification = 2;
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv) WHERE active;
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (document_id, active);
CREATE INDEX IF NOT EXISTS chunks_dept ON chunks (dept, classification) WHERE active;
CREATE INDEX IF NOT EXISTS chunks_hash ON chunks (version_id, chunk_hash);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (priority, run_after) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS review_open ON review_items (version_id) WHERE status = 'open';
```

`ingest/db.py`：

```python
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ingest.config import get_settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_pool: ConnectionPool | None = None


def _configure(conn: Connection) -> None:
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = get_settings().db_url
        if not url:
            raise RuntimeError("INGEST_DB_URL 未配置")
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            configure=_configure,
        )
    return _pool


def reset_pool_for_tests() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
    _pool = None


@contextmanager
def connection() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        yield conn


def migrate(conn: Connection) -> None:
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()
```

`app/audit.py`（从 `app/server.py` 原样搬移，保留函数签名）：

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
AUDIT_FILE = BASE_DIR / "logs" / "audit.jsonl"


def write_audit(record: dict, audit_file: Path | None = None) -> None:
    """追加一行 JSON 审计日志；等保留存/防篡改属于 P1。"""
    audit_file = Path(audit_file or AUDIT_FILE)
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now().isoformat(timespec="seconds"), **record}
    with audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
```

`app/server.py` 中删除本地 `write_audit` 与 `json`/`datetime` 的相关实现，改为兼容包装（**不要**改调用点，保证现有测试仍能 patch `server.AUDIT_FILE`）：

```python
from app import audit as _audit

AUDIT_FILE = _audit.AUDIT_FILE


def write_audit(record: dict, audit_file: Path | None = None) -> None:
    _audit.write_audit(record, audit_file or AUDIT_FILE)
```

- [ ] **Step 4: 启动数据库并验证测试通过**

```bash
uv sync
docker compose up -d postgres
docker compose ps
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_config.py tests/ingest/test_schema.py tests/test_audit.py -v
uv run pytest tests/test_main.py tests/test_web.py -q
```

Expected: 新增测试 PASS；现有 73 个测试仍全绿（`test_write_audit_appends_jsonl` 通过兼容包装）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock docker-compose.yml infra/init/01-test-db.sql .env.example \
  ingest/__init__.py ingest/config.py ingest/errors.py ingest/db.py ingest/schema.sql \
  app/audit.py app/server.py tests/ingest/__init__.py tests/ingest/conftest.py \
  tests/ingest/test_config.py tests/ingest/test_schema.py tests/test_audit.py
git commit -m "feat(ingest): 初始化配置、数据库 schema、审计模块与测试基座"
```

---

### Task 2: DocModel IR

**Files:**
- Create: `ingest/ir.py`, `tests/ingest/test_ir.py`
- Modify: `pyproject.toml`（已加 pydantic，无需再改）

**Interfaces:**
- Consumes: `ingest.errors.ParseError`。
- Produces:
  - `ingest.ir.Column`、`Table`、`Block`、`DocMeta`、`ParserInfo`、`Warning`、`DocModel`。
  - `DocModel.to_json() -> str`、`DocModel.from_json(text: str) -> DocModel`、`DocModel.tables() -> list[Table]`。
  - `Table.data_start_row`、`Table.data_rows()`、`Table.footer_values()`、`Table.full_grid()`、`table.range_start_row`。

- [ ] **Step 1: 写失败测试**

```python
import pytest
from pydantic import ValidationError

from ingest.ir import Block, Column, DocMeta, DocModel, ParserInfo, Table, Warning


def _table(**over):
    data = {
        "table_key": "t1",
        "sheet": "2025预算",
        "range": "A3:H57",
        "header_rows": [3, 4],
        "header_values": [["", "Q1", "Q2"], ["部门", "预算", "预算"]],
        "columns": [
            {"key": "c1", "name": "部门", "dtype": "string", "source_names": ["部门"]},
            {"key": "c2", "name": "Q1/预算", "dtype": "number", "unit": "万元"},
        ],
        "rows": [["综合处", "120"], ["人事处", "98"], ["合计", "218"]],
        "footer_rows": [56],
        "merge_ranges": ["B3:C3"],
        "confidence": 0.91,
        "source": "heuristic",
    }
    data.update(over)
    return data


def _model(**over) -> DocModel:
    data = {
        "schema_version": 1,
        "parser": {"name": "xlsx", "version": "0.1.0"},
        "meta": {"title": "2025年部门预算表", "source_filename": "2025预算.xlsx",
                 "sha256": "a" * 64},
        "blocks": [
            {"block_id": "b1", "type": "heading", "level": 1, "text": "二、预算",
             "path": ["二、预算"]},
            {"block_id": "b2", "type": "table", "table": _table()},
        ],
        "warnings": [{"code": "merged_cells", "block_id": "b2", "detail": "B3:C3"}],
    }
    data.update(over)
    return DocModel.model_validate(data)


def test_round_trip_json():
    model = _model()
    assert DocModel.from_json(model.to_json()) == model


def test_table_helpers():
    table = _model().tables()[0]
    assert table.range_start_row == 3
    assert table.data_start_row == 5
    assert table.data_rows() == [["综合处", "120"], ["人事处", "98"]]
    assert table.footer_values() == [["合计", "218"]]
    assert table.full_grid()[0] == ["", "Q1", "Q2"]


def test_rejects_unsorted_header_rows():
    with pytest.raises(ValidationError):
        _model(blocks=[{"block_id": "b1", "type": "table",
                        "table": _table(header_rows=[4, 3])}])


def test_rejects_row_width_mismatch():
    with pytest.raises(ValidationError):
        _model(blocks=[{"block_id": "b1", "type": "table",
                        "table": _table(rows=[["只有一列"]])}])


def test_rejects_known_classification_in_ir():
    with pytest.raises(ValidationError):
        _model(meta={"title": "x", "source_filename": "x.xlsx",
                     "sha256": "a" * 64, "classification": 2})


def test_rejects_duplicate_block_ids():
    with pytest.raises(ValidationError):
        _model(blocks=[
            {"block_id": "b1", "type": "paragraph", "text": "a", "path": []},
            {"block_id": "b1", "type": "paragraph", "text": "b", "path": []},
        ])
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_ir.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'ingest.ir'`。

- [ ] **Step 3: 写实现**

`ingest/ir.py`：

```python
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
    columns: list[Column]
    rows: list[list[str]]
    footer_rows: list[int] = Field(default_factory=list)
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
        if self.header_rows != sorted(set(self.header_rows)):
            raise ValueError("header_rows 必须严格递增且不重复")
        if len(self.header_values) != len(self.header_rows):
            raise ValueError("header_values 行数必须等于 header_rows 行数")
        if len({c.key for c in self.columns}) != len(self.columns):
            raise ValueError("columns.key 必须唯一")
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("rows 每行列数必须等于 columns 数")
        for row in self.header_values:
            if len(row) != len(self.columns):
                raise ValueError("header_values 每行列数必须等于 columns 数")
        if set(self.header_rows) & set(self.footer_rows):
            raise ValueError("footer_rows 不能与 header_rows 重叠")
        return self

    @property
    def range_start_row(self) -> int:
        return int(_A1_RANGE.match(self.range).group(1))

    @property
    def data_start_row(self) -> int:
        return max(self.header_rows) + 1 if self.header_rows else self.range_start_row

    def data_rows(self) -> list[list[str]]:
        start = self.data_start_row
        return [row for i, row in enumerate(self.rows) if start + i not in self.footer_rows]

    def footer_values(self) -> list[list[str]]:
        start = self.data_start_row
        return [row for i, row in enumerate(self.rows) if start + i in self.footer_rows]

    def full_grid(self) -> list[list[str]]:
        return [list(row) for row in self.header_values] + [list(row) for row in self.rows]


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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_ir.py -v
```

Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add ingest/ir.py tests/ingest/test_ir.py
git commit -m "feat(ingest): 定义 DocModel 统一中间表示与校验"
```

---

### Task 3: 安全嗅探与内容寻址存储

**Files:**
- Create: `ingest/sniff.py`, `ingest/storage.py`, `tests/ingest/builders.py`, `tests/ingest/test_sniff.py`, `tests/ingest/test_storage.py`

**Interfaces:**
- Consumes: `Settings`、`IngestError` 子类。
- Produces:
  - `ingest.sniff.SniffResult(kind, mime, size_bytes, sha256, filename, encrypted, macro, warnings)`。
  - `sniff_file(path: Path, filename: str, settings: Settings | None = None) -> SniffResult`。
  - `strip_macros(src: Path, dst: Path) -> bool`（返回是否剥离过宏）。
  - `ingest.storage.FileStore(root: Path)`，方法 `put_file(src: Path, sha256: str) -> str`、`resolve(rel: str) -> Path`、`delete(rel: str) -> None`。

- [ ] **Step 1: 写失败测试**

`tests/ingest/builders.py`（后续任务复用）：

```python
from pathlib import Path

from docx import Document
from openpyxl import Workbook


def build_xlsx(path: Path, rows: list[list], sheet: str = "Sheet1") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def build_docx(path: Path, paragraphs: list[tuple[str | None, str]],
               table: list[list[str]] | None = None) -> Path:
    doc = Document()
    for style, text in paragraphs:
        doc.add_paragraph(text, style=style)
    if table:
        t = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                t.cell(r, c).text = value
    doc.save(path)
    return path
```

`tests/ingest/test_sniff.py`：

```python
import zipfile
from dataclasses import replace

import fitz
import pytest

from ingest.config import get_settings
from ingest.errors import Quarantined, TooLarge, UnsupportedType
from ingest.sniff import sniff_file, strip_macros
from tests.ingest.builders import build_xlsx


def test_sniffs_xlsx(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [["部门", "金额"], ["综合处", 120]])
    result = sniff_file(path, "a.xlsx")
    assert result.kind == "xlsx"
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    assert result.macro is False


def test_extension_must_match_content(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [["x"]])
    with pytest.raises(UnsupportedType):
        sniff_file(path, "a.docx")


def test_rejects_unknown_and_oversize(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a document")
    with pytest.raises(UnsupportedType):
        sniff_file(junk, "junk.docx")

    path = build_xlsx(tmp_path / "a.xlsx", [["x"]])
    small = replace(get_settings(), max_file_mb=0)
    with pytest.raises(TooLarge):
        sniff_file(path, "a.xlsx", small)


def test_rejects_zip_bomb_ratio(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [["x"]])
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("xl/media/big.bin", b"\0" * 30_000_000)
    with pytest.raises(Quarantined):
        sniff_file(path, "a.xlsx")


def test_detects_and_strips_macros(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsm", [["x"]])
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("xl/vbaProject.bin", b"macro")
    assert sniff_file(path, "a.xlsm").macro is True

    clean = tmp_path / "clean.xlsm"
    assert strip_macros(path, clean) is True
    with zipfile.ZipFile(clean) as zf:
        assert "xl/vbaProject.bin" not in zf.namelist()


def test_encrypted_pdf_is_quarantined(tmp_path):
    path = tmp_path / "secret.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(Quarantined):
        sniff_file(path, "secret.pdf")
```

`tests/ingest/test_storage.py`：

```python
import hashlib

import pytest

from ingest.errors import Quarantined
from ingest.storage import FileStore


def test_put_is_content_addressed_and_idempotent(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello")
    sha = hashlib.sha256(b"hello").hexdigest()
    store = FileStore(tmp_path / "uploads")
    rel = store.put_file(src, sha)
    assert rel == f"{sha[:2]}/{sha}"
    src.unlink()
    assert store.resolve(rel).read_bytes() == b"hello"
    assert store.put_file(store.resolve(rel), sha) == rel


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../x"])
def test_resolve_rejects_traversal(tmp_path, bad):
    store = FileStore(tmp_path / "uploads")
    with pytest.raises(Quarantined):
        store.resolve(bad)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_sniff.py tests/ingest/test_storage.py -v
```

Expected: FAIL，`No module named 'ingest.sniff'`。

- [ ] **Step 3: 写实现**

`ingest/sniff.py`：

```python
from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import filetype
import fitz

from ingest.config import Settings, get_settings
from ingest.errors import Quarantined, TooLarge, UnsupportedType

OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_TOTAL_LIMIT = 500 * 1024 * 1024
ZIP_RATIO_LIMIT = 100
EXT_KIND = {".docx": "docx", ".doc": "doc", ".xlsx": "xlsx",
            ".xlsm": "xlsm", ".pdf": "pdf"}


@dataclass(frozen=True)
class SniffResult:
    kind: str
    mime: str
    size_bytes: int
    sha256: str
    filename: str
    encrypted: bool = False
    macro: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detect_kind(path: Path) -> tuple[str | None, str, bool]:
    """返回 (kind, mime, has_macro)。只读本地字节。"""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        has_macro = any(name.endswith("vbaProject.bin") for name in names)
        if "word/document.xml" in names:
            return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", has_macro
        if "xl/workbook.xml" in names:
            if has_macro:
                return "xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", True
            return "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", False
        return None, "application/zip", has_macro
    head = path.open("rb").read(8)
    if head == OLE_MAGIC:
        return "doc", "application/msword", False
    if head.startswith(b"%PDF"):
        return "pdf", "application/pdf", False
    guessed = filetype.guess(str(path))
    return None, (guessed.mime if guessed else "application/octet-stream"), False


def _check_zip_safety(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos)
        compressed = max(1, sum(i.compress_size for i in infos))
        if total > ZIP_TOTAL_LIMIT or total / compressed > ZIP_RATIO_LIMIT:
            raise Quarantined("zip 解压总量或压缩比超限")
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise Quarantined(f"压缩包内路径非法: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise Quarantined(f"压缩包内包含符号链接: {name}")


def sniff_file(path: Path, filename: str, settings: Settings | None = None) -> SniffResult:
    s = settings or get_settings()
    size = path.stat().st_size
    if size == 0:
        raise UnsupportedType("空文件")
    if size > s.max_file_bytes:
        raise TooLarge(f"文件超过 {s.max_file_mb} MB")
    ext = Path(filename).suffix.lower()
    if ext not in EXT_KIND:
        raise UnsupportedType(f"不支持的扩展名: {ext or '(无)'}")
    kind, mime, has_macro = _detect_kind(path)
    if kind is None:
        raise UnsupportedType("无法识别的文件内容")
    expected = EXT_KIND[ext]
    if kind != expected and not (expected == "xlsm" and kind == "xlsm"):
        if kind == "doc" and expected in ("docx", "xlsx", "xlsm"):
            raise Quarantined("文件已加密或为旧版 OLE 容器")
        raise UnsupportedType(f"扩展名 {ext} 与内容 {kind} 不一致")

    warnings: list[str] = []
    encrypted = False
    if kind in ("docx", "xlsx", "xlsm"):
        _check_zip_safety(path)
    if kind == "pdf":
        doc = fitz.open(path)
        try:
            if doc.needs_pass:
                encrypted = True
                raise Quarantined("PDF 已加密")
            if doc.page_count > s.max_pdf_pages:
                raise TooLarge(f"PDF 超过 {s.max_pdf_pages} 页")
        finally:
            doc.close()
    if has_macro:
        warnings.append("macro_stripped")
    return SniffResult(
        kind=kind, mime=mime, size_bytes=size, sha256=sha256_file(path),
        filename=filename, encrypted=encrypted, macro=has_macro,
        warnings=tuple(warnings),
    )


_REL_MACRO = re.compile(rb'<Relationship[^>]*vbaProject\.bin[^>]*/>')
_OVERRIDE_MACRO = re.compile(rb'<Override[^>]*vbaProject\.bin[^>]*/>')


def strip_macros(src: Path, dst: Path) -> bool:
    """复制 zip 并移除 vbaProject.bin 及其关系；返回是否剥离过。"""
    stripped = False
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename.endswith("vbaProject.bin"):
                stripped = True
                continue
            data = zin.read(info.filename)
            if info.filename in ("xl/_rels/workbook.xml.rels", "[Content_Types].xml"):
                data = _REL_MACRO.sub(b"", _OVERRIDE_MACRO.sub(b"", data))
            zout.writestr(info, data)
    return stripped
```

`ingest/storage.py`：

```python
from __future__ import annotations

import shutil
from pathlib import Path

from ingest.errors import Quarantined

BASE_DIR = Path(__file__).resolve().parents[1]


class FileStore:
    """内容寻址本地存储：uploads/{sha[:2]}/{sha}。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put_file(self, src: Path, sha256: str) -> str:
        rel = f"{sha256[:2]}/{sha256}"
        dest = self.resolve(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
        return rel

    def resolve(self, rel: str) -> Path:
        candidate = (self.root / rel).resolve()
        root = self.root.resolve()
        if root not in candidate.parents and candidate != root:
            raise Quarantined(f"非法存储路径: {rel}")
        return candidate

    def delete(self, rel: str) -> None:
        path = self.resolve(rel)
        if path.exists():
            path.unlink()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_sniff.py tests/ingest/test_storage.py -v
```

Expected: 9 passed（加密 PDF 与 zip bomb 用例不得 skip）。

- [ ] **Step 5: Commit**

```bash
git add ingest/sniff.py ingest/storage.py tests/ingest/builders.py \
  tests/ingest/test_sniff.py tests/ingest/test_storage.py
git commit -m "feat(ingest): 文件安全嗅探与内容寻址存储"
```

---

### Task 4: 任务队列与 worker 循环

**Files:**
- Create: `ingest/queue.py`, `ingest/worker.py`, `tests/ingest/test_queue.py`

**Interfaces:**
- Consumes: `ingest.db`。
- Produces:
  - `enqueue(conn, kind: str, payload: dict, *, priority: int = 100, max_attempts: int = 3, run_after=None) -> int`。
  - `claim(conn, worker_id: str) -> dict | None`、`complete(conn, job_id: int) -> None`、`fail(conn, job_id: int, error: str) -> str`（返回 `queued|failed`）、`heartbeat(conn, job_id: int, worker_id: str) -> None`、`reap_stale(conn, timeout_seconds: int = 600) -> int`、`queue_depth(conn) -> int`。
  - `ingest.worker.run_job(conn, job: dict, handlers: dict[str, Callable[[Connection, dict], None]], worker_id: str, dead_letter_hooks: dict[str, Callable] | None = None) -> bool`。
  - `ingest.worker.run_forever(handlers, *, worker_id: str, poll_seconds: float = 1.0) -> None`。

- [ ] **Step 1: 写失败测试**

```python
import time
from datetime import datetime, timezone

import pytest

from ingest.queue import (
    claim, complete, enqueue, fail, heartbeat, queue_depth, reap_stale,
)
from ingest.worker import run_job


def test_enqueue_and_claim_are_skip_locked(conn):
    enqueue(conn, "parse", {"version_id": "v1"}, priority=10)
    enqueue(conn, "parse", {"version_id": "v2"}, priority=20)
    conn.commit()

    first = claim(conn, "worker-a")
    assert first["payload"]["version_id"] == "v1"
    # conn 已提交，锁释放；另一个连接抢到第二条
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(conn.info.dsn, row_factory=dict_row) as other:
        second = claim(other, "worker-b")
    assert second["payload"]["version_id"] == "v2"
    assert second["attempts"] == 1


def test_fail_backoff_then_dead_letter(conn):
    job_id = enqueue(conn, "parse", {}, max_attempts=3)
    conn.commit()

    delays = []
    for _ in range(3):
        job = claim(conn, "w")
        status = fail(conn, job["id"], "boom")
        assert status == "queued"
        row = conn.execute("SELECT run_after FROM jobs WHERE id=%s", (job["id"],)).fetchone()
        delays.append((row["run_after"] - datetime.now(timezone.utc)).total_seconds())
        conn.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (job["id"],))
        conn.commit()
    assert delays[0] == pytest.approx(30, abs=5)
    assert delays[1] == pytest.approx(120, abs=5)
    assert delays[2] == pytest.approx(600, abs=5)

    job = claim(conn, "w")
    assert fail(conn, job["id"], "boom") == "failed"
    row = conn.execute("SELECT status, last_error, finished_at FROM jobs WHERE id=%s",
                       (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"] == "boom"
    assert row["finished_at"] is not None


def test_reap_stale_requeues(conn):
    enqueue(conn, "parse", {})
    conn.commit()
    job = claim(conn, "w")
    conn.execute("UPDATE jobs SET locked_at = now() - interval '20 minutes' WHERE id=%s",
                 (job["id"],))
    conn.commit()
    assert reap_stale(conn, timeout_seconds=600) == 1
    row = conn.execute("SELECT status, locked_by FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    assert row["status"] == "queued"
    assert row["locked_by"] is None


def test_run_job_marks_done_and_calls_dead_letter(conn):
    job_id = enqueue(conn, "parse", {"version_id": "v1"})
    conn.commit()
    job = claim(conn, "w")
    calls = []

    def ok(conn, payload):
        calls.append(payload["version_id"])

    assert run_job(conn, job, {"parse": ok}, "w") is True
    assert calls == ["v1"]
    assert conn.execute("SELECT status FROM jobs WHERE id=%s", (job_id,)).fetchone()["status"] == "done"

    job_id2 = enqueue(conn, "parse", {"version_id": "v2"}, max_attempts=0)
    conn.commit()
    job2 = claim(conn, "w")

    def boom(conn, payload):
        raise RuntimeError("bad")

    dead = []
    assert run_job(conn, job2, {"parse": boom}, "w",
                   {"parse": lambda c, j: dead.append(j["id"])}) is False
    assert dead == [job_id2]
    assert conn.execute("SELECT status FROM jobs WHERE id=%s", (job_id2,)).fetchone()["status"] == "failed"


def test_heartbeat_and_depth(conn):
    enqueue(conn, "parse", {})
    conn.commit()
    assert queue_depth(conn) == 1
    job = claim(conn, "w")
    conn.execute("UPDATE jobs SET locked_at = now() - interval '5 minutes' WHERE id=%s",
                 (job["id"],))
    conn.commit()
    heartbeat(conn, job["id"], "w")
    row = conn.execute("SELECT locked_at FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    assert (datetime.now(timezone.utc) - row["locked_at"]).total_seconds() < 10
    complete(conn, job["id"])
    assert queue_depth(conn) == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_queue.py -v
```

Expected: FAIL，`No module named 'ingest.queue'`。

- [ ] **Step 3: 写实现**

`ingest/queue.py`：

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg import Connection
from psycopg.types.json import Jsonb

BACKOFF_SECONDS = [30, 120, 600]


def enqueue(conn: Connection, kind: str, payload: dict, *, priority: int = 100,
            max_attempts: int = 3, run_after: datetime | None = None) -> int:
    row = conn.execute(
        """INSERT INTO jobs (kind, payload, priority, max_attempts, run_after)
           VALUES (%s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (kind, Jsonb(payload), priority, max_attempts, run_after),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def claim(conn: Connection, worker_id: str) -> dict | None:
    row = conn.execute(
        """UPDATE jobs SET status='running', locked_by=%s, locked_at=now(),
                           attempts=attempts+1
           WHERE id = (
             SELECT id FROM jobs
             WHERE status='queued' AND run_after <= now()
             ORDER BY priority, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
           RETURNING *""",
        (worker_id,),
    ).fetchone()
    conn.commit()
    return row


def complete(conn: Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', finished_at=now(), locked_by=NULL, locked_at=NULL "
        "WHERE id=%s",
        (job_id,),
    )
    conn.commit()


def fail(conn: Connection, job_id: int, error: str) -> str:
    # 语义：首次执行 + 最多 max_attempts 次重试（计划差异 3）
    row = conn.execute(
        "SELECT attempts, max_attempts FROM jobs WHERE id=%s FOR UPDATE", (job_id,)
    ).fetchone()
    if row["attempts"] > row["max_attempts"]:
        conn.execute(
            "UPDATE jobs SET status='failed', last_error=%s, finished_at=now(), "
            "locked_by=NULL, locked_at=NULL WHERE id=%s",
            (error, job_id),
        )
        conn.commit()
        return "failed"
    delay = BACKOFF_SECONDS[min(row["attempts"] - 1, len(BACKOFF_SECONDS) - 1)]
    conn.execute(
        "UPDATE jobs SET status='queued', last_error=%s, locked_by=NULL, locked_at=NULL, "
        "run_after=now() + make_interval(secs => %s) WHERE id=%s",
        (error, delay, job_id),
    )
    conn.commit()
    return "queued"


def heartbeat(conn: Connection, job_id: int, worker_id: str) -> None:
    conn.execute(
        "UPDATE jobs SET locked_at=now() WHERE id=%s AND locked_by=%s AND status='running'",
        (job_id, worker_id),
    )
    conn.commit()


def reap_stale(conn: Connection, timeout_seconds: int = 600) -> int:
    rows = conn.execute(
        """UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL
           WHERE status='running'
             AND locked_at < now() - make_interval(secs => %s)
           RETURNING id""",
        (timeout_seconds,),
    ).fetchall()
    conn.commit()
    return len(rows)


def queue_depth(conn: Connection) -> int:
    return int(conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')"
    ).fetchone()["n"])
```

`ingest/worker.py`：

```python
from __future__ import annotations

import threading
import time
from typing import Callable

from psycopg import Connection

from ingest.db import connection
from ingest.queue import claim, complete, fail, heartbeat, reap_stale

Handler = Callable[[Connection, dict], None]
Hook = Callable[[Connection, dict], None]


def run_job(conn: Connection, job: dict, handlers: dict[str, Handler],
            worker_id: str, dead_letter_hooks: dict[str, Hook] | None = None) -> bool:
    handler = handlers.get(job["kind"])
    if handler is None:
        fail(conn, job["id"], f"unknown job kind: {job['kind']}")
        return False
    try:
        handler(conn, job["payload"])
    except Exception as exc:  # noqa: BLE001 - 失败必须落库并重试
        conn.rollback()
        status = fail(conn, job["id"], f"{type(exc).__name__}: {exc}")
        if status == "failed" and dead_letter_hooks and job["kind"] in dead_letter_hooks:
            dead_letter_hooks[job["kind"]](conn, job)
        return False
    complete(conn, job["id"])
    return True


def _start_heartbeat(job_id: int, worker_id: str) -> threading.Event:
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(30):
            try:
                with connection() as hb:
                    heartbeat(hb, job_id, worker_id)
            except Exception:  # noqa: BLE001 - 心跳失败交给回收机制
                pass

    threading.Thread(target=beat, daemon=True).start()
    return stop


def run_forever(handlers: dict[str, Handler], *, worker_id: str,
                poll_seconds: float = 1.0,
                dead_letter_hooks: dict[str, Hook] | None = None) -> None:
    while True:
        with connection() as conn:
            reap_stale(conn)
            job = claim(conn, worker_id)
        if job is None:
            time.sleep(poll_seconds)
            continue
        stop = _start_heartbeat(job["id"], worker_id)
        try:
            with connection() as conn:
                run_job(conn, job, handlers, worker_id, dead_letter_hooks)
        finally:
            stop.set()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_queue.py -v
```

Expected: 5 passed。

- [ ] **Step 5: Commit**

```bash
git add ingest/queue.py ingest/worker.py tests/ingest/test_queue.py
git commit -m "feat(ingest): 基于 SKIP LOCKED 的任务队列与 worker 执行器"
```

---

### Task 5: XLSX 网格读取

**Files:**
- Create: `ingest/convert.py`, `ingest/parsers/__init__.py`, `ingest/parsers/base.py`, `ingest/parsers/xlsx_reader.py`, `tests/ingest/test_xlsx_reader.py`, `tests/ingest/test_convert.py`
- Modify: `tests/ingest/builders.py`

**Interfaces:**
- Consumes: `Settings`、`SniffResult`。
- Produces:
  - `Cell(text, value, is_numeric, bold, has_fill, hidden, formula)`、`SheetGrid(name, hidden, rows, merges, styles_available)`、`WorkbookGrid(sheets, warnings)`。
  - `SheetGrid.cell(row, col) -> Cell`（1-based，越界返回空 `Cell`）、`.nrows`、`.ncols`。
  - `read_workbook(path: Path, settings: Settings | None = None) -> WorkbookGrid`。
  - `ingest.convert.find_soffice() -> Path | None`、`convert_document(src, target_ext, outdir=None, timeout=120) -> Path`、`ConversionError`。
  - `ingest.parsers.base.Parser` 协议、`ingest.parsers.get_parser(kind, **kwargs)`。

- [ ] **Step 1: 写失败测试**

先扩展 `tests/ingest/builders.py`：

```python
from openpyxl.styles import Font


def build_xlsx(path: Path, rows: list[list], sheet: str = "Sheet1",
               bold_rows: tuple[int, ...] = ()) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r, row in enumerate(rows, start=1):
        ws.append(row)
        if r in bold_rows:
            for cell in ws[r]:
                cell.font = Font(bold=True)
    wb.save(path)
    return path
```

`tests/ingest/test_xlsx_reader.py`：

```python
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

from ingest.config import get_settings
from ingest.parsers.xlsx_reader import read_workbook


def test_reads_display_values_styles_and_flags(tmp_path):
    path = tmp_path / "a.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "预算"
    ws.append(["部门", "占比", "日期", "金额"])
    ws.append(["综合处", 0.15, datetime(2026, 1, 1), 1234.5])
    ws["B2"].number_format = "0%"
    ws["C2"].number_format = "yyyy-mm-dd"
    ws["D2"].number_format = "#,##0.00"
    ws["A1"].font = ws["A1"].font.copy(bold=True)
    ws["A2"].fill = ws["A2"].fill.copy(fill_type="solid")
    ws["E5"] = "=B2+1"
    ws.row_dimensions[2].hidden = True
    ws.column_dimensions["B"].hidden = True
    wb.save(path)

    grid = read_workbook(path)
    sheet = grid.sheets[0]
    assert sheet.name == "预算"
    assert sheet.cell(2, 2).text == "15%"
    assert sheet.cell(2, 3).text == "2026-01-01"
    assert sheet.cell(2, 4).text == "1,234.50"
    assert sheet.cell(1, 1).bold is True
    assert sheet.cell(2, 1).has_fill is True
    assert sheet.cell(2, 2).is_numeric is True
    assert sheet.cell(2, 1).hidden is True
    assert sheet.cell(5, 5).formula == "=B2+1"
    assert any(w["code"] == "formula_not_cached" for w in grid.warnings)


def test_expands_merged_cells(tmp_path):
    path = tmp_path / "merged.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "预算"
    ws.merge_cells("A1:B1")
    ws["A2"] = "部门"
    ws["B2"] = "Q1"
    ws["A3"] = "综合处"
    ws["B3"] = 120
    wb.save(path)

    sheet = read_workbook(path).sheets[0]
    assert sheet.merges == ["A1:B1"]
    assert sheet.cell(1, 2).text == "预算"


def test_hidden_sheet_is_kept_with_flag(tmp_path):
    path = tmp_path / "hidden.xlsx"
    wb = Workbook()
    wb.active.title = "可见"
    wb.active["A1"] = "x"
    hidden = wb.create_sheet("隐藏")
    hidden["A1"] = "y"
    hidden.sheet_state = "hidden"
    wb.save(path)

    sheets = read_workbook(path).sheets
    assert [(s.name, s.hidden) for s in sheets] == [("可见", False), ("隐藏", True)]


def test_truncates_at_cell_limit(tmp_path):
    path = tmp_path / "big.xlsx"
    wb = Workbook()
    for r in range(1, 6):
        for c in range(1, 6):
            wb.active.cell(r, c, f"{r}-{c}")
    wb.save(path)

    settings = replace(get_settings(), max_cells_per_sheet=10)
    grid = read_workbook(path, settings)
    assert grid.sheets[0].nrows * grid.sheets[0].ncols <= 10
    assert any(w["code"] == "truncated" for w in grid.warnings)
```

`tests/ingest/test_convert.py`：

```python
from pathlib import Path
from unittest.mock import patch

import pytest

from ingest import convert


def test_find_soffice_from_env(tmp_path, monkeypatch):
    fake = tmp_path / "soffice"
    fake.write_text("")
    monkeypatch.setenv("SOFFICE_BIN", str(fake))
    assert convert.find_soffice() == fake


def test_missing_soffice_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SOFFICE_BIN", raising=False)
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.exists", return_value=False):
        assert convert.find_soffice() is None
    with pytest.raises(convert.ConversionError):
        convert.convert_document(tmp_path / "a.doc", "docx")


@pytest.mark.skipif(convert.find_soffice() is None, reason="未安装 LibreOffice")
def test_real_conversion(tmp_path):
    from docx import Document

    src = tmp_path / "a.docx"
    Document().save(src)
    out = convert.convert_document(src, "pdf", outdir=tmp_path)
    assert out.exists() and out.stat().st_size > 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_xlsx_reader.py tests/ingest/test_convert.py -v
```

Expected: FAIL，`No module named 'ingest.parsers'`。

- [ ] **Step 3: 写实现**

`ingest/convert.py`：

```python
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ConversionError(Exception):
    pass


def find_soffice() -> Path | None:
    env = os.getenv("SOFFICE_BIN")
    if env and Path(env).exists():
        return Path(env)
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
        Path("/usr/bin/soffice"),
        Path("/opt/libreoffice/program/soffice"),
    ):
        if candidate.exists():
            return candidate
    return None


def convert_document(src: Path, target_ext: str, outdir: Path | None = None,
                     timeout: int = 120) -> Path:
    soffice = find_soffice()
    if soffice is None:
        raise ConversionError("未找到 LibreOffice，请设置 SOFFICE_BIN 或安装后重试")
    outdir = Path(outdir or tempfile.mkdtemp(prefix="rag-convert-"))
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [str(soffice), "--headless", "--convert-to", target_ext,
             "--outdir", str(outdir), str(src)],
            check=True, timeout=timeout, capture_output=True,
        )
    except subprocess.SubprocessError as exc:
        raise ConversionError(f"LibreOffice 转换失败: {exc}") from exc
    out = outdir / f"{Path(src).stem}.{target_ext}"
    if not out.exists():
        raise ConversionError(f"转换未生成文件: {out}")
    return out
```

`ingest/parsers/base.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ingest.ir import DocModel
from ingest.sniff import SniffResult


class ParseError(Exception):
    """单个文件解析失败；流水线会重试并在耗尽后转人工。"""


class Parser(Protocol):
    name: str
    version: str

    def supports(self, sniff: SniffResult) -> bool: ...
    def parse(self, path: Path) -> DocModel: ...
```

`ingest/parsers/__init__.py`（懒加载，后续任务补分支）：

```python
from __future__ import annotations

from ingest.parsers.base import Parser


def get_parser(kind: str, **kwargs) -> Parser:
    if kind in ("xlsx", "xlsm"):
        from ingest.parsers.xlsx_parser import XlsxParser
        return XlsxParser(**kwargs)
    raise KeyError(f"未注册的解析器类型: {kind}")
```

`ingest/parsers/xlsx_reader.py`：

```python
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ingest.config import Settings, get_settings


@dataclass(frozen=True)
class Cell:
    text: str = ""
    value: object = None
    is_numeric: bool = False
    bold: bool = False
    has_fill: bool = False
    hidden: bool = False
    formula: str | None = None


@dataclass
class SheetGrid:
    name: str
    hidden: bool
    rows: list[list[Cell]]
    merges: list[str]
    styles_available: bool = True

    @property
    def nrows(self) -> int:
        return len(self.rows)

    @property
    def ncols(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    def cell(self, row: int, col: int) -> Cell:
        if 1 <= row <= self.nrows and 1 <= col <= self.ncols:
            return self.rows[row - 1][col - 1]
        return Cell()


@dataclass
class WorkbookGrid:
    sheets: list[SheetGrid]
    warnings: list[dict]


def _display(cell) -> str:
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        if value.time() == time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        fmt = cell.number_format or ""
        if "%" in fmt:
            return f"{value * 100:g}%"
        if "#,##" in fmt:
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return str(value)
    return str(value)


def _is_numeric_value(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_workbook(path: Path, settings: Settings | None = None) -> WorkbookGrid:
    s = settings or get_settings()
    wb_values = load_workbook(path, data_only=True)
    wb_formulas = load_workbook(path, data_only=False)
    warnings: list[dict] = []
    sheets: list[SheetGrid] = []
    remaining_cells = s.max_cells_per_workbook

    for ws in wb_values.worksheets:
        ws_formula = wb_formulas[ws.title]
        max_row = ws.max_row or 0
        max_col = ws.max_column or 0
        if max_row == 0 or max_col == 0:
            continue
        cells = max_row * max_col
        limit = min(s.max_cells_per_sheet, max(0, remaining_cells))
        if cells > limit:
            max_col = max(1, min(max_col, limit // max(1, max_row)))
            max_row = max(1, min(max_row, limit // max(1, max_col)))
            warnings.append({"code": "truncated",
                              "detail": f"{ws.title} 超过单元格上限，已截断"})
        remaining_cells -= max_row * max_col
        if remaining_cells <= 0:
            warnings.append({"code": "truncated",
                             "detail": "工作簿超过单元格上限，后续工作表已跳过"})
            break

        hidden_cols = {
            get_column_letter(c) for c in range(1, max_col + 1)
            if ws.column_dimensions[get_column_letter(c)].hidden
        }
        rows: list[list[Cell]] = []
        for r in range(1, max_row + 1):
            row_cells: list[Cell] = []
            row_hidden = bool(ws.row_dimensions[r].hidden)
            for c in range(1, max_col + 1):
                value_cell = ws.cell(r, c)
                formula_cell = ws_formula.cell(r, c)
                formula = (
                    formula_cell.value
                    if isinstance(formula_cell.value, str) and formula_cell.value.startswith("=")
                    else None
                )
                if formula and value_cell.value is None:
                    if len([w for w in warnings if w["code"] == "formula_not_cached"]) < 20:
                        warnings.append({
                            "code": "formula_not_cached",
                            "detail": f"{ws.title}!{value_cell.coordinate}",
                        })
                row_cells.append(Cell(
                    text=_display(value_cell),
                    value=value_cell.value,
                    is_numeric=_is_numeric_value(value_cell.value),
                    bold=bool(value_cell.font.bold),
                    has_fill=bool(value_cell.fill.fill_type == "solid"),
                    hidden=row_hidden or get_column_letter(c) in hidden_cols,
                    formula=formula,
                ))
            rows.append(row_cells)

        for merge in list(ws.merged_cells.ranges):
            top = rows[merge.min_row - 1][merge.min_col - 1]
            for r in range(merge.min_row, merge.max_row + 1):
                for c in range(merge.min_col, merge.max_col + 1):
                    rows[r - 1][c - 1] = replace(top, formula=None)

        sheets.append(SheetGrid(
            name=ws.title,
            hidden=ws.sheet_state != "visible",
            rows=rows,
            merges=[str(m) for m in ws.merged_cells.ranges],
            styles_available=True,
        ))

    return WorkbookGrid(sheets=sheets, warnings=warnings)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_xlsx_reader.py tests/ingest/test_convert.py -v
```

Expected: 6 passed（`test_real_conversion` 在未装 LibreOffice 时 skip，其余必须通过）。

- [ ] **Step 5: Commit**

```bash
git add ingest/convert.py ingest/parsers/__init__.py ingest/parsers/base.py \
  ingest/parsers/xlsx_reader.py tests/ingest/builders.py \
  tests/ingest/test_xlsx_reader.py tests/ingest/test_convert.py
git commit -m "feat(ingest): Excel 网格读取（合并单元格/样式/隐藏/公式/上限）"
```

---

### Task 6: 区域切分

**Files:**
- Create: `ingest/parsers/regions.py`, `tests/ingest/test_regions.py`

**Interfaces:**
- Consumes: `SheetGrid`。
- Produces: `Region(start_row, end_row, start_col, end_col, density)`，属性 `nrows/ncols`，`split_regions(sheet: SheetGrid, *, min_density: float = 0.10) -> list[Region]`（行号/列号均 1-based，闭区间）。

- [ ] **Step 1: 写失败测试**

```python
from ingest.parsers.regions import Region, split_regions
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows: list[list[str]]) -> SheetGrid:
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v)) if v not in (None, "") else Cell() for v in row]
              for row in padded],
        merges=[],
    )


def test_single_region():
    regions = split_regions(grid([
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["人事处", "98", "101"],
    ]))
    assert regions == [Region(1, 3, 1, 3, regions[0].density)]
    assert regions[0].density == 1.0


def test_blank_row_splits_two_tables():
    regions = split_regions(grid([
        ["部门", "金额"],
        ["综合处", "120"],
        ["", ""],
        ["部门", "人数"],
        ["综合处", "3"],
    ]))
    assert [(r.start_row, r.end_row) for r in regions] == [(1, 2), (4, 5)]


def test_two_blank_columns_split_side_by_side():
    regions = split_regions(grid([
        ["部门", "金额", "", "", "姓名", "人数"],
        ["综合处", "120", "", "", "张伟", "3"],
        ["人事处", "98", "", "", "李娜", "2"],
    ]))
    assert [(r.start_col, r.end_col) for r in regions] == [(1, 2), (5, 6)]


def test_single_blank_column_does_not_split():
    regions = split_regions(grid([
        ["部门", "", "金额"],
        ["综合处", "", "120"],
        ["人事处", "", "98"],
    ]))
    assert len(regions) == 1


def test_sparse_region_is_dropped():
    regions = split_regions(grid([
        ["x", "", "", "", ""],
        ["", "", "", "", "y"],
    ]))
    assert regions == []


def test_region_properties():
    region = Region(2, 5, 3, 7, 0.5)
    assert region.nrows == 4
    assert region.ncols == 5
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_regions.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.regions'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/regions.py`：

```python
from __future__ import annotations

from dataclasses import dataclass

from ingest.parsers.xlsx_reader import SheetGrid


@dataclass(frozen=True)
class Region:
    start_row: int
    end_row: int
    start_col: int
    end_col: int
    density: float

    @property
    def nrows(self) -> int:
        return self.end_row - self.start_row + 1

    @property
    def ncols(self) -> int:
        return self.end_col - self.start_col + 1


def _bands(indices: list[int], max_blank: int) -> list[tuple[int, int]]:
    if not indices:
        return []
    bands = []
    start = prev = indices[0]
    for idx in indices[1:]:
        if idx - prev - 1 > max_blank:
            bands.append((start, prev))
            start = idx
        prev = idx
    bands.append((start, prev))
    return bands


def split_regions(sheet: SheetGrid, *, min_density: float = 0.10) -> list[Region]:
    rows = [r for r in range(1, sheet.nrows + 1)
            if any(sheet.cell(r, c).text.strip() for c in range(1, sheet.ncols + 1))]
    cols = [c for c in range(1, sheet.ncols + 1)
            if any(sheet.cell(r, c).text.strip() for r in range(1, sheet.nrows + 1))]
    if not rows or not cols:
        return []

    row_bands = _bands(rows, max_blank=1)      # 1 个全空行即切分
    col_bands = _bands(cols, max_blank=2)      # 连续 2 个全空列才切分

    regions: list[Region] = []
    for r0, r1 in row_bands:
        for c0, c1 in col_bands:
            total = (r1 - r0 + 1) * (c1 - c0 + 1)
            filled = sum(
                1 for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)
                if sheet.cell(r, c).text.strip()
            )
            density = filled / total if total else 0.0
            if (r1 - r0 + 1) < 2 or (c1 - c0 + 1) < 2 or density < min_density:
                continue
            regions.append(Region(r0, r1, c0, c1, density))
    return sorted(regions, key=lambda r: (r.start_row, r.start_col))
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_regions.py -v
```

Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/regions.py tests/ingest/test_regions.py
git commit -m "feat(ingest): Excel 区域切分（空行/双空列/密度过滤）"
```

---

### Task 7: 表头规则打分与列名合成

**Files:**
- Create: `ingest/parsers/header_rules.py`, `tests/ingest/test_header_rules.py`

**Interfaces:**
- Consumes: `SheetGrid`、`Region`、`Settings`。
- Produces:
  - `HeaderCandidate(start_row, depth, confidence, features, penalties, source="heuristic")`，`to_dict()`。
  - `HeaderDecision(candidate, accepted, reason, candidates)`。
  - `compose_header_names(sheet, region, start_row, depth) -> list[str]`。
  - `score_candidate(sheet, region, start_row, depth) -> HeaderCandidate`。
  - `generate_candidates(sheet, region) -> list[HeaderCandidate]`。
  - `detect_header(sheet, region, settings) -> HeaderDecision`。

- [ ] **Step 1: 写失败测试**

```python
import pytest

from ingest import config
from ingest.parsers import header_rules as hr
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows: list[list], bold_rows: tuple[int, ...] = ()) -> SheetGrid:
    width = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v)) if v not in (None, "") else Cell(
                  bold=(r + 1) in bold_rows) for v in row]
              for r, row in enumerate(padded)],
        merges=[],
    )


def detect(rows, bold_rows=()):
    sheet = grid(rows, bold_rows)
    region = Region(1, len(rows), 1, sheet.ncols, 1.0)
    return hr.detect_header(sheet, region, config.get_settings())


def test_single_row_header_is_accepted():
    decision = detect([
        ["序号", "部门", "金额"],
        ["1", "综合处", "120"],
        ["2", "人事处", "98"],
        ["3", "财务处", "150"],
    ], bold_rows=(1,))
    assert decision.accepted is True
    assert decision.candidate.start_row == 1
    assert decision.candidate.depth == 1
    assert decision.candidate.confidence >= 0.75


def test_double_header_with_merged_parent():
    decision = detect([
        ["", "预算", "预算"],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["人事处", "98", "101"],
    ], bold_rows=(1, 2))
    assert decision.accepted is True
    assert decision.candidate.depth == 2
    assert hr.compose_header_names(
        grid([["", "预算", "预算"], ["部门", "Q1", "Q2"]]),
        Region(1, 2, 1, 3, 1.0), 1, 2,
    ) == ["部门", "预算/Q1", "预算/Q2"]


def test_numeric_only_block_has_no_candidate():
    decision = detect([
        ["1", "120", "130"],
        ["2", "98", "101"],
        ["3", "150", "160"],
    ])
    assert decision.accepted is False
    assert decision.candidate is None
    assert decision.reason == "no_candidates"


def test_duplicate_header_is_penalized():
    decision = detect([
        ["金额", "金额", "金额"],
        ["120", "130", "140"],
        ["98", "101", "105"],
    ], bold_rows=(1,))
    assert decision.accepted is False


def test_margin_requires_lead(monkeypatch):
    candidates = [
        hr.HeaderCandidate(1, 1, 0.90, {}, ()),
        hr.HeaderCandidate(1, 2, 0.85, {}, ()),
    ]
    monkeypatch.setattr(hr, "generate_candidates", lambda *a, **k: candidates)
    sheet = grid([["部门", "金额"], ["综合处", "120"]])
    decision = hr.detect_header(sheet, Region(1, 2, 1, 2, 1.0), config.get_settings())
    assert decision.accepted is False
    assert decision.reason == "margin_too_small"


def test_header_above_data_is_penalized():
    sheet = grid([
        ["1", "120", "130"],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
    ], bold_rows=(2,))
    scored = hr.score_candidate(sheet, Region(1, 3, 1, 3, 1.0), 2, 1)
    assert "data_above" in scored.penalties
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_header_rules.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.header_rules'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/header_rules.py`：

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest.config import Settings
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import Cell, SheetGrid

KEYWORDS = (
    "序号", "编号", "名称", "项目", "部门", "单位", "金额", "数量", "单价",
    "合计", "预算", "实际", "支出", "收入", "比例", "日期", "时间", "备注",
    "说明", "类型", "状态", "人数", "季度", "月份", "年度",
)
WEIGHTS = {
    "fill": 0.12, "type_contrast": 0.28, "uniqueness": 0.14, "style": 0.14,
    "keyword": 0.10, "length": 0.08, "context": 0.14,
}
_NUMBER_RE = re.compile(r"^[+-]?[\d,]+(\.\d+)?%?$")


def _is_number(text: str) -> bool:
    return bool(_NUMBER_RE.match(text.replace("￥", "").replace("¥", "").strip()))


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True)
class HeaderCandidate:
    start_row: int
    depth: int
    confidence: float
    features: dict[str, float] = field(default_factory=dict)
    penalties: tuple[str, ...] = ()
    source: str = "heuristic"

    def to_dict(self) -> dict:
        return {
            "start_row": self.start_row, "depth": self.depth,
            "confidence": round(self.confidence, 4),
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "penalties": list(self.penalties), "source": self.source,
        }


@dataclass
class HeaderDecision:
    candidate: HeaderCandidate | None
    accepted: bool
    reason: str
    candidates: list[HeaderCandidate]


def _window(sheet: SheetGrid, region: Region, start_row: int, depth: int) -> list[list[Cell]]:
    return [
        [sheet.cell(r, c) for c in range(region.start_col, region.end_col + 1)]
        for r in range(start_row, start_row + depth)
    ]


def compose_header_names(sheet: SheetGrid, region: Region, start_row: int,
                         depth: int) -> list[str]:
    names: list[str] = []
    for c in range(region.start_col, region.end_col + 1):
        parts: list[str] = []
        for r in range(start_row, start_row + depth):
            text = _norm(sheet.cell(r, c).text)
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        names.append("/".join(parts))
    return names


def _ratio(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def _header_text_ratio(window: list[list[Cell]]) -> float:
    cells = [cell for row in window for cell in row if cell.text.strip()]
    return _ratio([not cell.is_numeric and not _is_number(cell.text) for cell in cells])


def _below_numeric_ratio(sheet: SheetGrid, region: Region, data_start: int) -> float:
    ratios = []
    for c in range(region.start_col, region.end_col + 1):
        values = [sheet.cell(r, c).text for r in range(data_start, min(region.end_row, data_start + 19) + 1)]
        nonempty = [v for v in values if v.strip()]
        if nonempty:
            ratios.append(_ratio([_is_number(v) or "-" in v or "/" in v for v in nonempty]))
    return sum(ratios) / len(ratios) if ratios else 0.0


def _style_ratio(window: list[list[Cell]], sheet: SheetGrid) -> float:
    if not sheet.styles_available:
        return 0.5
    cells = [cell for row in window for cell in row if cell.text.strip()]
    return _ratio([cell.bold or cell.has_fill for cell in cells])


def _looks_like_title(sheet: SheetGrid, region: Region, row: int) -> bool:
    if row < region.start_row:
        return True
    texts = [sheet.cell(row, c).text for c in range(region.start_col, region.end_col + 1)]
    nonempty = [t for t in texts if t.strip()]
    if not nonempty:
        return True
    numeric = _ratio([_is_number(t) for t in nonempty])
    avg_len = sum(len(t) for t in nonempty) / len(nonempty)
    return numeric < 0.2 and avg_len > 12


def score_candidate(sheet: SheetGrid, region: Region, start_row: int,
                    depth: int) -> HeaderCandidate:
    window = _window(sheet, region, start_row, depth)
    names = compose_header_names(sheet, region, start_row, depth)
    nonempty_names = [n for n in names if n]
    data_start = start_row + depth
    prev_row = start_row - 1
    next_ok = data_start <= region.end_row

    fill = _ratio([bool(cell.text.strip()) for row in window for cell in row])
    type_contrast = 0.5 * _header_text_ratio(window) + 0.5 * (
        _below_numeric_ratio(sheet, region, data_start) if next_ok else 0.0
    )
    uniqueness = len(set(nonempty_names)) / len(nonempty_names) if nonempty_names else 0.0
    style = _style_ratio(window, sheet)
    keyword = _ratio([any(k in n for k in KEYWORDS) for n in nonempty_names]) if nonempty_names else 0.0
    avg_len = sum(len(n) for n in nonempty_names) / len(nonempty_names) if nonempty_names else 99
    length = max(0.0, 1.0 - min(avg_len, 30) / 30)
    context = 0.5 * (1.0 if _looks_like_title(sheet, region, prev_row) else 0.0) + 0.5 * (
        1.0 if next_ok else 0.0
    )

    features = {
        "fill": fill, "type_contrast": type_contrast, "uniqueness": uniqueness,
        "style": style, "keyword": keyword, "length": length, "context": context,
    }
    penalties: list[str] = []
    if prev_row >= region.start_row and not _looks_like_title(sheet, region, prev_row):
        penalties.append("data_above")
    if len(nonempty_names) != len(set(nonempty_names)):
        penalties.append("duplicate_names")
    header_cells = [cell for row in window for cell in row]
    if _ratio([not cell.text.strip() for cell in header_cells]) > 0.3:
        penalties.append("empty_headers")
    if avg_len > 25:
        penalties.append("too_verbose")

    penalty = {
        "data_above": 0.20, "duplicate_names": 0.20,
        "empty_headers": 0.30, "too_verbose": 0.20,
    }
    score = sum(WEIGHTS[k] * features[k] for k in WEIGHTS) - sum(penalty[p] for p in penalties)
    return HeaderCandidate(
        start_row=start_row, depth=depth,
        confidence=max(0.0, min(1.0, score)),
        features=features, penalties=tuple(penalties),
    )


def generate_candidates(sheet: SheetGrid, region: Region) -> list[HeaderCandidate]:
    candidates: list[HeaderCandidate] = []
    last_start = min(region.end_row - 1, region.start_row + 19)
    for start in range(region.start_row, last_start + 1):
        row = [sheet.cell(start, c) for c in range(region.start_col, region.end_col + 1)]
        texts = [cell.text.strip() for cell in row if cell.text.strip()]
        if not texts:
            continue
        if any(len(t) > 40 for t in texts):
            continue
        numeric = _ratio([_is_number(t) for t in texts])
        has_keyword = any(any(k in t for k in KEYWORDS) for t in texts)
        if numeric > 0.7 and not has_keyword:
            continue
        for depth in (1, 2, 3):
            if start + depth - 1 > region.end_row or start + depth > region.end_row:
                break
            candidates.append(score_candidate(sheet, region, start, depth))
    return sorted(candidates, key=lambda c: c.confidence, reverse=True)


def detect_header(sheet: SheetGrid, region: Region, settings: Settings) -> HeaderDecision:
    candidates = generate_candidates(sheet, region)
    if not candidates:
        return HeaderDecision(None, False, "no_candidates", [])
    top = candidates[0]
    second = candidates[1].confidence if len(candidates) > 1 else 0.0
    margin = top.confidence - second
    if top.confidence < settings.header_accept:
        return HeaderDecision(top, False, "low_confidence", candidates[:3])
    if margin < settings.header_margin:
        return HeaderDecision(top, False, "margin_too_small", candidates[:3])
    return HeaderDecision(top, True, "accepted", candidates[:3])
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_header_rules.py -v
```

Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/header_rules.py tests/ingest/test_header_rules.py
git commit -m "feat(ingest): 表头七特征打分、双层表头与接受条件"
```

---

### Task 8: 版式模板与 LLM 兜底

**Files:**
- Create: `ingest/parsers/templates.py`, `ingest/parsers/header_llm.py`, `tests/ingest/test_templates.py`, `tests/ingest/test_header_llm.py`

**Interfaces:**
- Consumes: `SheetGrid`、`Region`、`HeaderCandidate`、`compose_header_names`。
- Produces:
  - `normalize_header_name(text: str) -> str`。
  - `build_signature(sheet_name: str | None, region: Region, candidate: HeaderCandidate, names: list[str]) -> dict`。
  - `match_template(signature: dict, templates: list[dict]) -> dict | None`（template 行含 `id/name/scope_dept/signature/config`）。
  - `template_header_rows(template: dict, region: Region) -> list[int]`、`template_columns(template: dict) -> list[dict]`。
  - `parse_llm_output(raw: str, region: Region, ncols: int) -> dict`（失败抛 `ValueError`）。
  - `build_llm_input(sheet, region) -> str`、`try_llm_header(sheet, region, llm) -> tuple[HeaderCandidate | None, str]`。

- [ ] **Step 1: 写失败测试**

`tests/ingest/test_templates.py`：

```python
from ingest.parsers.regions import Region
from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.templates import (
    build_signature, match_template, normalize_header_name,
    template_columns, template_header_rows,
)


def test_normalize_header_name():
    assert normalize_header_name(" Q1预算（万元） ") == "q1预算"
    assert normalize_header_name("金额(元)*") == "金额"
    assert normalize_header_name("部　门") == "部门"


def test_signature_and_match():
    region = Region(1, 10, 1, 3, 1.0)
    cand = HeaderCandidate(3, 2, 0.9, {}, ())
    sig = build_signature("2025预算", region, cand,
                          ["部门", "Q1预算（万元）", "Q2预算（万元）"])
    assert sig["ncols"] == 3
    assert sig["relative_header_row"] == 2
    assert sig["header_depth"] == 2
    assert sig["sheet_pattern"] == "^2025预算$"

    template = {"id": "t", "name": "预算模板", "scope_dept": "财务处",
                "signature": sig,
                "config": {"columns": [{"key": "c1", "name": "部门"}]}}
    assert match_template(sig, [template]) is template

    other = build_signature("2025预算", region, cand, ["部门", "Q1", "Q2"])
    assert match_template(other, [template]) is None


def test_sheet_pattern_blocks_mismatch():
    region = Region(1, 5, 1, 2, 1.0)
    cand = HeaderCandidate(1, 1, 0.9, {}, ())
    sig = build_signature("2024预算", region, cand, ["部门", "金额"])
    template = {
        "id": "t", "name": "x", "scope_dept": None,
        "signature": {**sig, "sheet_pattern": "^2025预算$"},
        "config": {"columns": []},
    }
    assert match_template(sig, [template]) is None


def test_template_rows_and_columns():
    region = Region(5, 20, 1, 3, 1.0)
    template = {
        "signature": {"relative_header_row": 2, "header_depth": 2},
        "config": {"columns": [{"key": "c1", "name": "部门"}]},
    }
    assert template_header_rows(template, region) == [7, 8]
    assert template_columns(template) == [{"key": "c1", "name": "部门"}]
```

`tests/ingest/test_header_llm.py`：

```python
import json

import pytest

from ingest.parsers.header_llm import build_llm_input, parse_llm_output, try_llm_header
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows):
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v)) if v not in (None, "") else Cell() for v in row]
              for row in rows],
        merges=["B1:C1"],
    )


def test_build_llm_input_limits_size():
    sheet = grid([["部门", "Q1", "Q2"], ["综合处", "120", "130"]])
    payload = build_llm_input(sheet, Region(1, 2, 1, 3, 1.0))
    assert "部门\tQ1\tQ2" in payload
    assert "merged: B1:C1" in payload


def test_parse_valid_output():
    region = Region(3, 57, 1, 2, 1.0)
    raw = json.dumps({
        "header_rows": [3, 4], "data_start": 5, "end_row": 57,
        "columns": ["部门", "Q1预算"], "footer_rows": [56],
        "confidence": 0.8, "reason": "ok",
    })
    result = parse_llm_output(raw, region, ncols=2)
    assert result["header_rows"] == [3, 4]
    assert result["columns"] == ["部门", "Q1预算"]


@pytest.mark.parametrize("payload", [
    {"header_rows": [4, 3], "data_start": 5, "end_row": 57, "columns": ["a", "b"]},
    {"header_rows": [3], "data_start": 3, "end_row": 57, "columns": ["a", "b"]},
    {"header_rows": [3], "data_start": 4, "end_row": 57, "columns": ["a", "a"]},
    {"header_rows": [3], "data_start": 4, "end_row": 57, "columns": ["a"]},
    {"header_rows": [3], "data_start": 4, "end_row": 57, "columns": ["a", "b"],
     "footer_rows": [3]},
    {"header_rows": [99], "data_start": 100, "end_row": 57, "columns": ["a", "b"]},
])
def test_parse_invalid_output_raises(payload):
    region = Region(3, 57, 1, 2, 1.0)
    with pytest.raises(ValueError):
        parse_llm_output(json.dumps(payload), region, ncols=2)


class _FakeLLM:
    def __init__(self, content):
        self.content = content

    def invoke(self, messages):
        class _Msg:
            pass
        msg = _Msg()
        msg.content = self.content
        return msg


def test_try_llm_header_parses_fenced_json():
    sheet = grid([["部门", "Q1"], ["综合处", "120"]])
    region = Region(1, 2, 1, 2, 1.0)
    llm = _FakeLLM("```json\n" + json.dumps({
        "header_rows": [1], "data_start": 2, "end_row": 2,
        "columns": ["部门", "Q1"], "footer_rows": [], "confidence": 0.8,
    }) + "\n```")
    candidate, raw = try_llm_header(sheet, region, llm)
    assert candidate is not None
    assert candidate.source == "llm"
    assert candidate.confidence == 0.8
    assert "部门" in raw
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_templates.py tests/ingest/test_header_llm.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.templates'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/templates.py`：

```python
from __future__ import annotations

import re
import unicodedata

from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.regions import Region

_UNIT_SUFFIX = re.compile(r"[（(][^）)]{1,8}[）)]\s*$")
_TRAILING_MARK = re.compile(r"[\s*＊]+$")


def normalize_header_name(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _UNIT_SUFFIX.sub("", text)
    text = _TRAILING_MARK.sub("", text)
    return re.sub(r"\s+", "", text)


def build_signature(sheet_name: str | None, region: Region,
                    candidate: HeaderCandidate, names: list[str]) -> dict:
    return {
        "ncols": len(names),
        "header_depth": candidate.depth,
        "relative_header_row": candidate.start_row - region.start_row,
        "columns_norm": [normalize_header_name(n) for n in names],
        "sheet_pattern": f"^{re.escape(sheet_name)}$" if sheet_name else None,
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def match_template(signature: dict, templates: list[dict]) -> dict | None:
    for template in templates:
        sig = template["signature"]
        if sig.get("ncols") != signature.get("ncols"):
            continue
        pattern = sig.get("sheet_pattern")
        if pattern and not re.search(pattern, signature.get("sheet_name") or
                                     signature.get("_sheet_name") or ""):
            # signature 本身不带 sheet 名，调用方在匹配前注入 _sheet_name
            if not re.search(pattern, signature.get("sheet_name", "")):
                continue
        if _jaccard(set(sig.get("columns_norm", [])),
                    set(signature.get("columns_norm", []))) >= 0.90:
            return template
    return None


def template_header_rows(template: dict, region: Region) -> list[int]:
    sig = template["signature"]
    start = region.start_row + int(sig["relative_header_row"])
    return [start + i for i in range(int(sig["header_depth"]))]


def template_columns(template: dict) -> list[dict]:
    return list(template.get("config", {}).get("columns", []))
```

> 说明：`match_template` 需要 sheet 名做 `sheet_pattern` 匹配。调用方在 `build_signature` 后把 `signature["sheet_name"] = sheet.name` 注入；测试里的 signature 直接来自 `build_signature`，因此 `sheet_pattern` 匹配会失败。为保持测试与实现一致，`build_signature` 直接写入 `"sheet_name": sheet_name`，`match_template` 读该字段即可。上面代码的兼容分支保留为防御，但主路径是 `sheet_name`。

修正后的关键实现（以这段为准）：

```python
def build_signature(sheet_name: str | None, region: Region,
                    candidate: HeaderCandidate, names: list[str]) -> dict:
    return {
        "ncols": len(names),
        "header_depth": candidate.depth,
        "relative_header_row": candidate.start_row - region.start_row,
        "columns_norm": [normalize_header_name(n) for n in names],
        "sheet_pattern": f"^{re.escape(sheet_name)}$" if sheet_name else None,
        "sheet_name": sheet_name,
    }


def match_template(signature: dict, templates: list[dict]) -> dict | None:
    for template in templates:
        sig = template["signature"]
        if sig.get("ncols") != signature.get("ncols"):
            continue
        pattern = sig.get("sheet_pattern")
        if pattern and not re.search(pattern, signature.get("sheet_name") or ""):
            continue
        if _jaccard(set(sig.get("columns_norm", [])),
                    set(signature.get("columns_norm", []))) >= 0.90:
            return template
    return None
```

`ingest/parsers/header_llm.py`：

```python
from __future__ import annotations

import json
import re

from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import SheetGrid

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

PROMPT = """你是表格结构识别助手。下面是 Excel 区域左上角最多 20 行 × 30 列的 TSV，
单元格已截断到 40 字符；merged 行列出合并区间。请只输出 JSON：
{"header_rows":[3,4],"data_start":5,"end_row":57,
 "columns":["部门","Q1预算(万元)"],"footer_rows":[56],
 "confidence":0.8,"reason":"..."}
规则：header_rows 用区域内的绝对行号且递增；columns 数量等于列数；列名非空且不重复；
footer_rows 不与表头/数据重叠。不要输出解释或 Markdown。"""


def build_llm_input(sheet: SheetGrid, region: Region) -> str:
    lines = [f"range: rows {region.start_row}-{region.end_row}, cols {region.start_col}-{region.end_col}"]
    if sheet.merges:
        lines.append("merged: " + ", ".join(sheet.merges))
    last_row = min(region.end_row, region.start_row + 19)
    last_col = min(region.end_col, region.start_col + 29)
    for r in range(region.start_row, last_row + 1):
        cells = []
        for c in range(region.start_col, last_col + 1):
            text = sheet.cell(r, c).text.replace("\t", " ").replace("\n", " ")[:40]
            cells.append(text)
        lines.append("\t".join(cells))
    return "\n".join(lines)


def parse_llm_output(raw: str, region: Region, ncols: int) -> dict:
    match = _JSON_BLOCK.search(raw)
    if not match:
        raise ValueError("未找到 JSON")
    payload = json.loads(match.group(0))
    header_rows = payload.get("header_rows")
    data_start = payload.get("data_start")
    end_row = payload.get("end_row")
    columns = payload.get("columns")
    footer_rows = payload.get("footer_rows") or []
    if not isinstance(header_rows, list) or not header_rows or not all(
            isinstance(v, int) for v in header_rows):
        raise ValueError("header_rows 非法")
    if header_rows != sorted(set(header_rows)):
        raise ValueError("header_rows 必须递增且不重复")
    if not all(region.start_row <= v <= region.start_row + 19 for v in header_rows):
        raise ValueError("header_rows 越界")
    if not isinstance(data_start, int) or data_start <= max(header_rows):
        raise ValueError("data_start 必须大于表头")
    if not isinstance(end_row, int) or end_row < data_start or end_row > region.end_row:
        raise ValueError("end_row 非法")
    if not isinstance(columns, list) or len(columns) != ncols:
        raise ValueError("columns 数量必须等于列数")
    if not all(isinstance(c, str) and c.strip() for c in columns):
        raise ValueError("columns 不能为空")
    if len(set(columns)) != len(columns):
        raise ValueError("columns 不能重复")
    if not isinstance(footer_rows, list) or not all(isinstance(v, int) for v in footer_rows):
        raise ValueError("footer_rows 非法")
    if set(footer_rows) & set(header_rows) or any(
            data_start <= v <= end_row for v in footer_rows):
        raise ValueError("footer_rows 与表头/数据区重叠")
    confidence = float(payload.get("confidence", 0.8))
    return {
        "header_rows": header_rows, "data_start": data_start, "end_row": end_row,
        "columns": columns, "footer_rows": footer_rows,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(payload.get("reason", "")),
    }


def try_llm_header(sheet: SheetGrid, region: Region, llm) -> tuple[HeaderCandidate | None, str]:
    payload = build_llm_input(sheet, region)
    response = llm.invoke([("system", PROMPT), ("human", payload)])
    raw = response.content or ""
    try:
        parsed = parse_llm_output(raw, region, ncols=region.ncols)
    except (ValueError, json.JSONDecodeError):
        return None, raw
    candidate = HeaderCandidate(
        start_row=parsed["header_rows"][0],
        depth=len(parsed["header_rows"]),
        confidence=parsed["confidence"],
        features={"llm": 1.0},
        penalties=(),
        source="llm",
    )
    return candidate, raw
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_templates.py tests/ingest/test_header_llm.py -v
```

Expected: 9 passed（参数化非法用例共 6 条）。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/templates.py ingest/parsers/header_llm.py \
  tests/ingest/test_templates.py tests/ingest/test_header_llm.py
git commit -m "feat(ingest): 版式模板匹配与表头 LLM 兜底"
```

---

### Task 9: 表格规范化与 XLSX 解析编排

**Files:**
- Create: `ingest/parsers/table_build.py`, `ingest/parsers/xlsx_parser.py`, `tests/ingest/test_table_build.py`, `tests/ingest/test_xlsx_parser.py`
- Modify: `ingest/parsers/__init__.py`

**Interfaces:**
- Consumes: `SheetGrid`、`Region`、`HeaderDecision`、模板函数、`DocModel`。
- Produces:
  - `build_table(sheet, region, header: HeaderCandidate | None, *, table_key: str, settings, forced_columns: list[dict] | None = None) -> tuple[Table, list[Warning]]`。
  - `rebuild_table(table: Table, new_header_rows: list[int], columns: list[dict], *, settings) -> tuple[Table, list[Warning]]`（复核台改表头用）。
  - `XlsxParser(templates=None, llm=None, settings=None)`，`name="xlsx"`、`version="0.1.0"`、`parse(path) -> DocModel`。
  - `get_parser("xlsx" | "xlsm", **kwargs)`。

- [ ] **Step 1: 写失败测试**

`tests/ingest/test_table_build.py`：

```python
from ingest import config
from ingest.parsers.header_rules import HeaderCandidate
from ingest.parsers.regions import Region
from ingest.parsers.table_build import build_table, rebuild_table
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def grid(rows):
    return SheetGrid(
        name="S", hidden=False,
        rows=[[Cell(text=str(v)) if v not in (None, "") else Cell() for v in row]
              for row in rows],
        merges=[],
    )


def test_builds_columns_types_units_and_footer():
    sheet = grid([
        ["部门", "金额（万元）", "日期"],
        ["综合处", "1,200", "2026-01-01"],
        ["人事处", "980", "2026-01-02"],
        ["合计", "2,180", ""],
    ])
    region = Region(1, 4, 1, 3, 1.0)
    header = HeaderCandidate(1, 1, 0.9, {}, ())
    table, warnings = build_table(sheet, region, header, table_key="t1",
                                  settings=config.get_settings())
    assert [c.name for c in table.columns] == ["部门", "金额", "日期"]
    assert table.columns[1].unit == "万元"
    assert [c.dtype for c in table.columns] == ["string", "number", "date"]
    assert table.footer_rows == [4]
    assert table.data_rows() == [["综合处", "1,200", "2026-01-01"],
                                 ["人事处", "980", "2026-01-02"]]
    assert table.range == "A1:C4"
    assert table.header_values == [["部门", "金额（万元）", "日期"]]
    assert any(w.code == "merged_cells" for w in warnings) is False


def test_repeated_header_is_dropped():
    sheet = grid([
        ["部门", "金额"],
        ["综合处", "120"],
        ["部门", "金额"],
        ["人事处", "98"],
    ])
    table, warnings = build_table(
        sheet, Region(1, 4, 1, 2, 1.0), HeaderCandidate(1, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    assert table.data_rows() == [["综合处", "120"], ["人事处", "98"]]
    assert any(w.code == "repeated_header" for w in warnings)


def test_no_header_falls_back_to_column_names():
    sheet = grid([["1", "120"], ["2", "98"]])
    table, warnings = build_table(
        sheet, Region(1, 2, 1, 2, 1.0), None,
        table_key="t1", settings=config.get_settings())
    assert [c.name for c in table.columns] == ["列1", "列2"]
    assert table.header_rows == []
    assert table.confidence == 0.0
    assert any(w.code == "table_no_header" for w in warnings)


def test_forced_template_columns_override_names():
    sheet = grid([["部门", "Q1"], ["综合处", "120"]])
    table, _ = build_table(
        sheet, Region(1, 2, 1, 2, 1.0), HeaderCandidate(1, 1, 0.95, {}, (), "template"),
        table_key="t1", settings=config.get_settings(),
        forced_columns=[{"key": "c1", "name": "部门", "dtype": "string"},
                        {"key": "c2", "name": "一季度", "dtype": "number", "unit": "万元"}])
    assert [c.name for c in table.columns] == ["部门", "一季度"]
    assert table.source == "template"


def test_rebuild_with_new_header_rows():
    sheet = grid([
        ["2025年预算", "", ""],
        ["部门", "Q1", "Q2"],
        ["综合处", "120", "130"],
        ["合计", "120", "130"],
    ])
    table, _ = build_table(
        sheet, Region(1, 4, 1, 3, 1.0), HeaderCandidate(2, 1, 0.9, {}, ()),
        table_key="t1", settings=config.get_settings())
    columns = [{"key": "c1", "name": "部门"}, {"key": "c2", "name": "Q1"},
               {"key": "c3", "name": "Q2"}]
    rebuilt, _ = rebuild_table(table, [2, 3], columns, settings=config.get_settings())
    assert rebuilt.header_rows == [2, 3]
    assert rebuilt.header_values == [["部门", "Q1", "Q2"], ["综合处", "120", "130"]]
    assert rebuilt.source == "human"
```

`tests/ingest/test_xlsx_parser.py`：

```python
from pathlib import Path

from ingest.parsers.xlsx_parser import XlsxParser
from tests.ingest.builders import build_xlsx


def test_single_table_auto_accepted(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [
        ["序号", "部门", "金额"],
        [1, "综合处", 120],
        [2, "人事处", 98],
        [3, "财务处", 150],
    ], bold_rows=(1,))
    model = XlsxParser().parse(path)
    table = model.tables()[0]
    assert table.confidence >= 0.75
    assert table.source == "heuristic"
    assert [c.name for c in table.columns] == ["序号", "部门", "金额"]
    assert model.parser.name == "xlsx"


def test_multiple_regions_produce_two_tables(tmp_path):
    path = build_xlsx(tmp_path / "two.xlsx", [
        ["部门", "金额"],
        ["综合处", 120],
        ["", ""],
        ["姓名", "人数"],
        ["张伟", 3],
    ], bold_rows=(1, 4))
    model = XlsxParser().parse(path)
    assert len(model.tables()) == 2
    assert any(w.code == "multiple_tables" for w in model.warnings)


def test_no_header_is_flagged(tmp_path):
    path = build_xlsx(tmp_path / "raw.xlsx", [["1", "120"], ["2", "98"]])
    model = XlsxParser().parse(path)
    assert any(w.code == "table_no_header" for w in model.warnings)


def test_hidden_sheet_skipped(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "可见"
    wb.active.append(["部门", "金额"])
    wb.active.append(["综合处", 120])
    hidden = wb.create_sheet("隐藏")
    hidden.append(["部门", "金额"])
    hidden.append(["综合处", 1])
    hidden.sheet_state = "hidden"
    path = tmp_path / "hidden.xlsx"
    wb.save(path)
    model = XlsxParser().parse(path)
    assert len(model.tables()) == 1
    assert any(w.code == "hidden_sheet_skipped" for w in model.warnings)


def test_template_hit_sets_confidence(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [
        ["部门", "Q1预算（万元）", "Q2预算（万元）"],
        ["综合处", 120, 130],
        ["人事处", 98, 101],
    ], bold_rows=(1,))
    template = {
        "id": "t1", "name": "预算模板", "scope_dept": None,
        "signature": {
            "ncols": 3, "header_depth": 1, "relative_header_row": 0,
            "columns_norm": ["部门", "q1预算", "q2预算"],
            "sheet_pattern": None, "sheet_name": "Sheet1",
        },
        "config": {"columns": [
            {"key": "c1", "name": "部门", "dtype": "string"},
            {"key": "c2", "name": "Q1预算", "dtype": "number", "unit": "万元"},
            {"key": "c3", "name": "Q2预算", "dtype": "number", "unit": "万元"},
        ]},
    }
    model = XlsxParser(templates=[template]).parse(path)
    table = model.tables()[0]
    assert table.source == "template"
    assert table.confidence == 0.95
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_table_build.py tests/ingest/test_xlsx_parser.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.table_build'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/table_build.py`：

```python
from __future__ import annotations

import re
from dataclasses import replace

from openpyxl.utils import get_column_letter

from ingest.config import Settings
from ingest.ir import Column, Table, Warning
from ingest.parsers.header_rules import HeaderCandidate, compose_header_names
from ingest.parsers.regions import Region
from ingest.parsers.xlsx_reader import SheetGrid

FOOTER_RE = re.compile(r"^(合\s*计|总\s*计|小\s*计|累计|备\s*注|注[:：]|说\s*明)")
UNIT_RE = re.compile(r"[（(]([^）)]{1,8})[）)]\s*$")
NUMBER_RE = re.compile(r"^[+-]?[\d,]+(\.\d+)?%?$")
DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def _is_number(text: str) -> bool:
    return bool(NUMBER_RE.match(text.replace("￥", "").replace("¥", "").strip()))


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _clean_name(text: str, index: int) -> tuple[str, str | None]:
    unit_match = UNIT_RE.search(text)
    unit = unit_match.group(1) if unit_match else None
    name = UNIT_RE.sub("", text).strip() or f"列{index}"
    return name, unit


def _infer_dtype(values: list[str]) -> str:
    values = [v for v in values if v.strip()]
    if not values:
        return "string"
    numeric = sum(1 for v in values if _is_number(v)) / len(values)
    if numeric >= 0.8:
        return "number"
    dates = sum(1 for v in values if DATE_RE.match(v)) / len(values)
    if dates >= 0.8:
        return "date"
    if all(v in {"是", "否", "true", "false", "True", "False"} for v in values):
        return "bool"
    return "string"


def _detect_footer_rows(sheet: SheetGrid, region: Region,
                        start_row: int) -> list[int]:
    footers: list[int] = []
    for r in range(start_row, region.end_row + 1):
        texts = [sheet.cell(r, c).text.strip()
                 for c in range(region.start_col, region.end_col + 1)]
        nonempty = [t for t in texts if t]
        first = next((t for t in texts[:3] if t), "")
        if FOOTER_RE.match(_norm(first)):
            footers.append(r)
            continue
        if len(nonempty) >= 2 and all(_is_number(t) for t in nonempty) \
                and any(not t for t in texts):
            footers.append(r)
    return footers


def build_table(sheet: SheetGrid, region: Region, header: HeaderCandidate | None, *,
                table_key: str, settings: Settings,
                forced_columns: list[dict] | None = None) -> tuple[Table, list[Warning]]:
    warnings: list[Warning] = []
    ncols = region.ncols
    header_rows = (
        [header.start_row + i for i in range(header.depth)] if header else []
    )
    header_values = [
        [sheet.cell(r, c).text for c in range(region.start_col, region.end_col + 1)]
        for r in header_rows
    ]
    names = (compose_header_names(sheet, region, header.start_row, header.depth)
             if header else [f"列{i}" for i in range(1, ncols + 1)])

    data_start = max(header_rows) + 1 if header_rows else region.start_row
    raw_rows: list[list[str]] = []
    for r in range(data_start, region.end_row + 1):
        raw_rows.append([
            sheet.cell(r, c).text for c in range(region.start_col, region.end_col + 1)
        ])

    header_signature = {_norm(v) for v in (header_values[-1] if header_values else []) if _norm(v)}
    kept_rows: list[list[str]] = []
    repeated = False
    for offset, row in enumerate(raw_rows):
        if header_signature and {_norm(v) for v in row if _norm(v)} == header_signature:
            repeated = True
            continue
        kept_rows.append(row)
    if repeated:
        warnings.append(Warning("repeated_header", None, "正文中出现重复表头，已丢弃"))

    footer_rows = [r for r in _detect_footer_rows(sheet, region, data_start)]
    # 重复表头被丢弃后，footer 的绝对行号仍以原始区域为准
    columns: list[Column] = []
    if forced_columns:
        for i, cfg in enumerate(forced_columns, start=1):
            columns.append(Column(
                key=cfg.get("key", f"c{i}"), name=cfg.get("name", f"列{i}"),
                dtype=cfg.get("dtype", "string"), unit=cfg.get("unit"),
                source_names=cfg.get("source_names", []),
            ))
    else:
        data_columns = list(zip(*[row for row in kept_rows
                                  if not _is_number(row[0] if row else "")]))
        for i, name in enumerate(names):
            clean, unit = _clean_name(name, i + 1)
            values = [row[i] for row in kept_rows if i < len(row)]
            columns.append(Column(
                key=f"c{i + 1}", name=clean, dtype=_infer_dtype(values),
                unit=unit, source_names=[p for p in name.split("/") if p],
            ))
        if not columns:
            columns = [Column(key=f"c{i}", name=f"列{i}") for i in range(1, ncols + 1)]

    if len({c.name for c in columns}) != len(columns):
        seen: dict[str, int] = {}
        for i, col in enumerate(columns):
            count = seen.get(col.name, 0) + 1
            seen[col.name] = count
            if count > 1:
                columns[i] = col.model_copy(update={"name": f"{col.name}_{count}"})
        warnings.append(Warning("ambiguous_header", None, "列名重复，已加后缀区分"))

    if header is None:
        warnings.append(Warning("table_no_header", None, "未识别到表头，使用列1..列N"))
    if sheet.merges:
        warnings.append(Warning("merged_cells", None, f"{len(sheet.merges)} 个合并区间"))

    confidence = header.confidence if header else 0.0
    source = header.source if header else "heuristic"
    if forced_columns and header:
        source = "template"
        confidence = 0.95
    if repeated:
        confidence = min(confidence, 0.7)

    table = Table(
        table_key=table_key,
        sheet=sheet.name,
        range=(f"{get_column_letter(region.start_col)}{region.start_row}:"
               f"{get_column_letter(region.end_col)}{region.end_row}"),
        header_rows=header_rows,
        header_values=header_values,
        columns=columns,
        rows=kept_rows,
        footer_rows=footer_rows,
        merge_ranges=[m for m in sheet.merges],
        confidence=max(0.0, min(1.0, confidence)),
        source=source,
        flags=[w.code for w in warnings],
        candidates=[header.to_dict()] if header else [],
    )
    return table, warnings


def rebuild_table(table: Table, new_header_rows: list[int],
                  columns: list[dict], *, settings: Settings) -> tuple[Table, list[Warning]]:
    warnings: list[Warning] = []
    if not new_header_rows or new_header_rows != sorted(set(new_header_rows)):
        raise ValueError("new_header_rows 必须非空、递增且不重复")
    grid = table.full_grid()
    start = new_header_rows[0]
    offset = start - table.range_start_row
    depth = len(new_header_rows)
    if offset < 0 or offset + depth > len(grid):
        raise ValueError("表头行超出表格范围")
    header_values = [list(row) for row in grid[offset:offset + depth]]
    rows = [list(row) for row in grid[offset + depth:]]
    data_start = start + depth
    footer_rows = [r for r in table.footer_rows if r >= data_start]
    new_columns = [Column(
        key=cfg.get("key", f"c{i}"), name=cfg.get("name", f"列{i}"),
        dtype=cfg.get("dtype", "string"), unit=cfg.get("unit"),
        source_names=cfg.get("source_names", []),
    ) for i, cfg in enumerate(columns, start=1)]
    rebuilt = table.model_copy(update={
        "header_rows": new_header_rows,
        "header_values": header_values,
        "columns": new_columns,
        "rows": rows,
        "footer_rows": footer_rows,
        "confidence": 1.0,
        "source": "human",
        "flags": sorted(set(table.flags) | {"human_confirmed"}),
        "candidates": table.candidates,
    })
    return rebuilt, warnings
```

`ingest/parsers/xlsx_parser.py`：

```python
from __future__ import annotations

import hashlib
from pathlib import Path

from ingest.config import Settings, get_settings
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning
from ingest.parsers.header_llm import try_llm_header
from ingest.parsers.header_rules import (
    HeaderCandidate, compose_header_names, detect_header,
)
from ingest.parsers.regions import split_regions
from ingest.parsers.table_build import build_table
from ingest.parsers.templates import (
    build_signature, match_template, template_columns,
)
from ingest.parsers.xlsx_reader import read_workbook


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
        warnings: list[dict] = list(workbook.warnings)
        blocks: list[Block] = []
        table_no = 0
        llm_calls = 0

        for sheet in workbook.sheets:
            if sheet.hidden and not self.settings.include_hidden_sheets:
                warnings.append({"code": "hidden_sheet_skipped", "detail": sheet.name})
                continue
            regions = split_regions(sheet)
            if len(regions) > 1:
                warnings.append({"code": "multiple_tables", "detail": sheet.name})
            for region in regions:
                table_no += 1
                decision = detect_header(sheet, region, self.settings)
                header = decision.candidate if decision.accepted else None
                forced_columns = None

                if header is None and decision.candidates:
                    for cand in decision.candidates[:3]:
                        names = compose_header_names(
                            sheet, region, cand.start_row, cand.depth)
                        signature = build_signature(
                            sheet.name, region, cand, names)
                        template = match_template(signature, self.templates)
                        if template:
                            header = HeaderCandidate(
                                cand.start_row, cand.depth, 0.95,
                                dict(cand.features), (), "template")
                            forced_columns = template_columns(template)
                            break

                if header is None and self.settings.header_llm_enabled \
                        and self.llm is not None and llm_calls < 3:
                    header, raw = try_llm_header(sheet, region, self.llm)
                    llm_calls += 1
                    if header is None:
                        warnings.append({"code": "ambiguous_header",
                                         "detail": raw[:500]})
                elif header is None and decision.candidate is not None:
                    warnings.append({
                        "code": "ambiguous_header",
                        "detail": f"置信度 {decision.candidate.confidence:.2f}",
                    })

                table_key = f"t{table_no}"
                try:
                    table, table_warnings = build_table(
                        sheet, region, header, table_key=table_key,
                        settings=self.settings, forced_columns=forced_columns)
                except Exception as exc:  # 单表失败不拖垮整份文件
                    warnings.append({"code": "parse_partial",
                                     "detail": f"{sheet.name} {table_key}: {exc}"})
                    continue
                for warning in table_warnings:
                    warnings.append({"code": warning.code, "detail": warning.detail})
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="table",
                    table=table, path=[],
                ))

        if not blocks:
            warnings.append({"code": "empty_document", "detail": "未找到可入库内容"})
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(title=path.stem, source_filename=path.name, sha256=sha),
            blocks=blocks,
            warnings=[Warning(code=w["code"], detail=w.get("detail", ""))
                      for w in warnings],
        )
```

`ingest/parsers/__init__.py` 增加 `xlsm` 已由 `xlsx` 分支处理，无需改。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_table_build.py tests/ingest/test_xlsx_parser.py -v
```

Expected: 9 passed。若 `test_single_table_auto_accepted` 因样式/关键词得分不足失败，先检查 `bold_rows=(1,)` 是否生效，再按 §8.3 调整测试数据（不得为过测试而改权重）。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/table_build.py ingest/parsers/xlsx_parser.py \
  ingest/parsers/__init__.py tests/ingest/test_table_build.py \
  tests/ingest/test_xlsx_parser.py
git commit -m "feat(ingest): 表格规范化与 XLSX 解析编排"
```

---

### Task 10: DOCX 解析器

**Files:**
- Create: `ingest/parsers/docx_parser.py`, `tests/ingest/test_docx_parser.py`
- Modify: `ingest/parsers/__init__.py`

**Interfaces:**
- Consumes: `DocModel`、`build_table`、`detect_header`、`templates`、`convert`。
- Produces:
  - `DocxParser(templates=None, llm=None, settings=None)`，`name="docx"`、`version="0.1.0"`、`supports(sniff)`、`parse(path) -> DocModel`。
  - `DocParser(...)`，`name="doc"`、`version="0.1.0"`（先 LibreOffice 转 docx 再解析）。

- [ ] **Step 1: 写失败测试**

```python
from unittest.mock import patch

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from ingest import config
from ingest.convert import ConversionError
from ingest.parsers.docx_parser import DocParser, DocxParser
from tests.ingest.builders import build_docx


def test_parses_headings_paragraphs_and_table(tmp_path):
    path = build_docx(
        tmp_path / "a.docx",
        paragraphs=[
            ("Heading 1", "二、预算管理"),
            (None, "各部门按季度编制预算。"),
            ("Heading 2", "（一）编制口径"),
            (None, "预算金额以万元为单位。"),
        ],
        table=[
            ["部门", "Q1预算（万元）", "Q2预算（万元）"],
            ["综合处", "120", "130"],
            ["人事处", "98", "101"],
        ],
    )
    model = DocxParser().parse(path)
    blocks = model.blocks
    assert [b.type for b in blocks] == ["heading", "paragraph", "heading", "paragraph", "table"]
    assert blocks[0].level == 1
    assert blocks[2].path == ["二、预算管理", "（一）编制口径"]
    table = model.tables()[0]
    assert [c.name for c in table.columns] == ["部门", "Q1预算", "Q2预算"]
    assert table.columns[1].unit == "万元"
    assert table.confidence >= 0.75
    assert blocks[4].path == ["二、预算管理", "（一）编制口径"]


def test_merged_cell_table(tmp_path):
    path = tmp_path / "merged.docx"
    doc = Document()
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).merge(table.cell(0, 2))
    values = [["预算", "", ""], ["部门", "Q1", "Q2"], ["综合处", "120", "130"]]
    for r, row in enumerate(values):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    doc.save(path)

    model = DocxParser().parse(path)
    assert len(model.tables()) == 1
    names = [c.name for c in model.tables()[0].columns]
    assert names == ["预算/部门", "预算/Q1", "预算/Q2"]


def test_textbox_content_is_captured(tmp_path):
    path = tmp_path / "textbox.docx"
    doc = Document()
    doc.add_paragraph("正文段落")
    xml = (
        f'<w:p {nsdecls("w")}><w:r><w:t>另一段</w:t></w:r>'
        '<w:r><w:pict xmlns:v="urn:schemas-microsoft-com:vml"><v:shape>'
        '<v:textbox><w:txbxContent><w:p><w:r><w:t>文本框内容</w:t></w:r></w:p>'
        '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>'
    )
    doc.element.body.append(parse_xml(xml))
    doc.save(path)

    model = DocxParser().parse(path)
    texts = [b.text for b in model.blocks]
    assert any("文本框内容" in t for t in texts)


def test_empty_docx_is_flagged(tmp_path):
    path = tmp_path / "empty.docx"
    Document().save(path)
    model = DocxParser().parse(path)
    assert model.blocks == []
    assert any(w.code == "empty_document" for w in model.warnings)


def test_doc_conversion_failure_is_parse_error(tmp_path):
    path = tmp_path / "a.doc"
    path.write_bytes(b"fake")
    with patch("ingest.parsers.docx_parser.convert_document",
               side_effect=ConversionError("no soffice")):
        with pytest.raises(Exception) as exc:
            DocParser().parse(path)
    assert "soffice" in str(exc.value)


def test_revision_is_warned(tmp_path):
    path = tmp_path / "rev.docx"
    doc = Document()
    doc.add_paragraph("正文")
    xml = (f'<w:p {nsdecls("w")}><w:ins w:id="1" w:author="a" w:date="2026-01-01T00:00:00Z">'
           '<w:r><w:t>插入</w:t></w:r></w:ins></w:p>')
    doc.element.body.append(parse_xml(xml))
    doc.save(path)
    model = DocxParser().parse(path)
    assert any(w.code == "revision_accepted" for w in model.warnings)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_docx_parser.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.docx_parser'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/docx_parser.py`：

```python
from __future__ import annotations

import hashlib
import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph
from openpyxl.utils import get_column_letter

from ingest.config import Settings, get_settings
from ingest.convert import ConversionError, convert_document
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning
from ingest.parsers.base import ParseError
from ingest.parsers.header_llm import try_llm_header
from ingest.parsers.header_rules import (
    HeaderCandidate, compose_header_names, detect_header,
)
from ingest.parsers.regions import Region
from ingest.parsers.table_build import build_table
from ingest.parsers.templates import build_signature, match_template, template_columns
from ingest.parsers.xlsx_reader import Cell, SheetGrid

_HEADING_STYLE = re.compile(r"(?:Heading|标题)\s*(\d)", re.IGNORECASE)
_DATE_RE = re.compile(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})")


def _iter_blocks(doc):
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield DocxTable(child, doc)


def _paragraph_text(paragraph: Paragraph) -> str:
    parts = [paragraph.text.strip()]
    for textbox in paragraph._p.iter(qn("w:txbxContent")):
        texts = [t.text for t in textbox.iter(qn("w:t")) if t.text]
        joined = "".join(texts).strip()
        if joined and joined not in parts:
            parts.append(joined)
    return "\n".join(p for p in parts if p)


def _heading_level(paragraph: Paragraph, text: str) -> int | None:
    match = _HEADING_STYLE.search(paragraph.style.name or "")
    if match:
        return min(6, int(match.group(1)))
    if re.match(r"^第[一二三四五六七八九十百]+[章节]", text):
        return 1
    if re.match(r"^[一二三四五六七八九十]+、", text):
        return 2
    if re.match(r"^（[一二三四五六七八九十]+）", text):
        return 3
    if re.match(r"^\d+(\.\d+)*[、.]", text):
        return 3
    return None


def _table_grid(table: DocxTable) -> SheetGrid:
    nrows = len(table.rows)
    ncols = len(table.columns)
    rows = [[Cell(text=table.cell(r, c).text) for c in range(ncols)] for r in range(nrows)]
    bounds: dict[int, list[int]] = {}
    for r in range(nrows):
        for c in range(ncols):
            key = id(table.cell(r, c)._tc)
            if key not in bounds:
                bounds[key] = [r, c, r, c]
            else:
                box = bounds[key]
                box[2] = max(box[2], r)
                box[3] = max(box[3], c)
    merges = [
        f"{get_column_letter(c0 + 1)}{r0 + 1}:{get_column_letter(c1 + 1)}{r1 + 1}"
        for (r0, c0, r1, c1) in bounds.values() if (r0, c0) != (r1, c1)
    ]
    return SheetGrid(name="", hidden=False, rows=rows, merges=merges,
                     styles_available=False)


class DocxParser:
    name = "docx"
    version = "0.1.0"

    def __init__(self, templates: list[dict] | None = None, llm=None,
                 settings: Settings | None = None):
        self.templates = templates or []
        self.llm = llm
        self.settings = settings or get_settings()

    def supports(self, sniff) -> bool:
        return sniff.kind in ("docx", "doc")

    def parse(self, path) -> DocModel:
        document = Document(path)
        warnings: list[Warning] = []
        if document.element.body.findall(".//" + qn("w:ins")):
            warnings.append(Warning("revision_accepted", None, "已按接受修订处理"))
        blocks: list[Block] = []
        path_stack: list[str] = []
        table_no = 0

        for item in _iter_blocks(document):
            if isinstance(item, Paragraph):
                text = _paragraph_text(item)
                if not text:
                    continue
                level = _heading_level(item, text)
                if level:
                    path_stack = path_stack[: level - 1] + [text]
                    blocks.append(Block(
                        block_id=f"b{len(blocks) + 1}", type="heading",
                        level=level, text=text, path=list(path_stack)))
                else:
                    blocks.append(Block(
                        block_id=f"b{len(blocks) + 1}", type="paragraph",
                        text=text, path=list(path_stack)))
            else:
                table_no += 1
                grid = _table_grid(item)
                if grid.nrows < 2 or grid.ncols < 2:
                    continue
                region = Region(1, grid.nrows, 1, grid.ncols, 1.0)
                decision = detect_header(grid, region, self.settings)
                header = decision.candidate if decision.accepted else None
                forced_columns = None
                if header is None and decision.candidates and self.templates:
                    for cand in decision.candidates[:3]:
                        names = compose_header_names(
                            grid, region, cand.start_row, cand.depth)
                        template = match_template(
                            build_signature(None, region, cand, names), self.templates)
                        if template:
                            header = HeaderCandidate(
                                cand.start_row, cand.depth, 0.95,
                                dict(cand.features), (), "template")
                            forced_columns = template_columns(template)
                            break
                if header is None and self.settings.header_llm_enabled and self.llm:
                    header, raw = try_llm_header(grid, region, self.llm)
                    if header is None:
                        warnings.append(Warning("ambiguous_header", None, raw[:500]))
                table, table_warnings = build_table(
                    grid, region, header, table_key=f"t{table_no}",
                    settings=self.settings, forced_columns=forced_columns)
                warnings.extend(table_warnings)
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="table", table=table,
                    path=list(path_stack)))

        if not blocks:
            warnings.append(Warning("empty_document", None, "Word 中没有可入库内容"))

        header_footer_text = "\n".join(
            p.text for section in document.sections
            for container in (section.header, section.footer)
            for p in container.paragraphs
        )
        date_match = _DATE_RE.search(header_footer_text)
        effective_date = (
            f"{date_match.group(1)}-{int(date_match.group(2)):02d}-"
            f"{int(date_match.group(3)):02d}" if date_match else None
        )
        title = (document.core_properties.title or "").strip() or path.stem
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(
                title=title, source_filename=path.name,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                effective_date=effective_date,
            ),
            blocks=blocks, warnings=warnings,
        )


class DocParser:
    name = "doc"
    version = "0.1.0"

    def __init__(self, templates: list[dict] | None = None, llm=None,
                 settings: Settings | None = None):
        self._inner = DocxParser(templates=templates, llm=llm, settings=settings)
        self.settings = settings or get_settings()

    def supports(self, sniff) -> bool:
        return sniff.kind == "doc"

    def parse(self, path) -> DocModel:
        try:
            converted = convert_document(path, "docx", timeout=120)
        except ConversionError as exc:
            raise ParseError(str(exc)) from exc
        model = self._inner.parse(converted)
        return model.model_copy(update={
            "parser": ParserInfo(name=self.name, version=self.version),
            "meta": model.meta.model_copy(update={
                "source_filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }),
        })
```

`ingest/parsers/__init__.py` 增加：

```python
    if kind == "docx":
        from ingest.parsers.docx_parser import DocxParser
        return DocxParser(**kwargs)
    if kind == "doc":
        from ingest.parsers.docx_parser import DocParser
        return DocParser(**kwargs)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_docx_parser.py -v
```

Expected: 6 passed。若合并单元格列名断言失败，检查 `_table_grid` 的 merge 展开是否把父列值带到每个子列。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/docx_parser.py ingest/parsers/__init__.py \
  tests/ingest/test_docx_parser.py
git commit -m "feat(ingest): DOCX/DOC 解析（标题层级/表格/文本框/修订）"
```

---

### Task 11: PDF 解析器

**Files:**
- Create: `ingest/parsers/pdf_parser.py`, `tests/ingest/test_pdf_parser.py`
- Modify: `ingest/parsers/__init__.py`

**Interfaces:**
- Consumes: `DocModel`、`build_table`、`detect_header`、`try_llm_header`。
- Produces: `PdfParser(templates=None, llm=None, settings=None)`，`name="pdf"`、`version="0.1.0"`、`supports(sniff)`、`parse(path) -> DocModel`；文本块与表格块均带 `page`（1-based）与 `bbox`。

- [ ] **Step 1: 写失败测试**

```python
import pytest

from ingest.config import Settings, get_settings
from ingest.errors import TooLarge
from ingest.parsers.pdf_parser import PdfParser


def _text_pdf(path, pages=2):
    import fitz
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 60), "内部资料", fontname="china-s", fontsize=9)
        page.insert_text((72, 100), f"第 {i + 1} 页 政务服务中心办公时间", fontname="china-s", fontsize=14)
        page.insert_text((72, 130), "工作日 8:30 至 17:00。", fontname="china-s", fontsize=10)
        page.insert_text((72, 800), f"第 {i + 1} 页", fontname="china-s", fontsize=9)
    doc.save(path)
    doc.close()


def test_extracts_text_bbox_and_removes_repeated_header(tmp_path):
    path = tmp_path / "a.pdf"
    _text_pdf(path)
    model = PdfParser().parse(path)
    texts = [b.text for b in model.blocks]
    assert any("办公时间" in t for t in texts)
    assert "内部资料" not in texts
    assert all(b.page in (1, 2) for b in model.blocks)
    assert all(b.bbox for b in model.blocks)


def test_scanned_pdf_is_flagged(tmp_path):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), alpha=False)
    page.insert_image(fitz.Rect(72, 72, 272, 272), pixmap=pix)
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()

    model = PdfParser().parse(path)
    assert any(w.code == "scanned_pdf" for w in model.warnings)


def test_page_limit_is_enforced(tmp_path):
    path = tmp_path / "a.pdf"
    _text_pdf(path, pages=1)
    settings = get_settings().__class__(**{**get_settings().__dict__, "max_pdf_pages": 0})
    with pytest.raises(TooLarge):
        PdfParser(settings=settings).parse(path)


def test_table_is_extracted_with_bbox(tmp_path):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    x0, y0, x1, y1 = 72, 100, 372, 200
    for x in (x0, x0 + 100, x0 + 200, x1):
        page.draw_line((x, y0), (x, y1))
    for y in (y0, y0 + 40, y0 + 70, y1):
        page.draw_line((x0, y), (x1, y))
    rows = [["Dept", "Q1", "Q2"], ["A", "120", "130"], ["B", "98", "101"]]
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            page.insert_text((x0 + 8 + c * 100, y0 + 25 + r * 30), value,
                             fontname="helv", fontsize=10)
    path = tmp_path / "table.pdf"
    doc.save(path)
    doc.close()

    model = PdfParser().parse(path)
    tables = model.tables()
    assert len(tables) >= 1
    assert tables[0].page == 1
    assert tables[0].bbox
    assert len(tables[0].columns) == 3
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_pdf_parser.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.pdf_parser'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/pdf_parser.py`：

```python
from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter
from pathlib import Path

import fitz
import pdfplumber

from ingest.config import Settings, get_settings
from ingest.errors import TooLarge
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning
from ingest.parsers.base import ParseError
from ingest.parsers.header_llm import try_llm_header
from ingest.parsers.header_rules import compose_header_names, detect_header
from ingest.parsers.regions import Region
from ingest.parsers.table_build import build_table
from ingest.parsers.templates import build_signature, match_template, template_columns
from ingest.parsers.xlsx_reader import Cell, SheetGrid


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _blocks_for_page(page) -> list[dict]:
    data = page.get_text("dict")
    result = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines, sizes, bold = [], [], False
        for line in block.get("lines", []):
            span_text = "".join(span.get("text", "") for span in line.get("spans", []))
            lines.append(span_text)
            for span in line.get("spans", []):
                sizes.append(float(span.get("size", 0)))
                bold = bold or bool(span.get("flags", 0) & 16)
        text = "\n".join(lines).strip()
        if text:
            result.append({
                "text": text, "bbox": [float(v) for v in block["bbox"]],
                "size": max(sizes or [0.0]), "bold": bold,
            })
    return result


def _overlaps(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    area = (ax1 - ax0) * (ay1 - ay0)
    return (ix * iy / area) if area else 0.0


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

    def parse(self, path: Path) -> DocModel:
        doc = fitz.open(path)
        try:
            if doc.needs_pass:
                raise ParseError("PDF 已加密")
            if doc.page_count > self.settings.max_pdf_pages:
                raise TooLarge(f"PDF 超过 {self.settings.max_pdf_pages} 页")
            page_blocks: list[list[dict]] = []
            page_tables: list[list[dict]] = []
            total_chars = 0
            has_image = False

            with pdfplumber.open(path) as plumber:
                for index, page in enumerate(doc):
                    plumber_page = plumber.pages[index]
                    table_bboxes = []
                    tables = []
                    for found in plumber_page.find_tables():
                        grid = found.extract()
                        if not grid or len(grid) < 2:
                            continue
                        table_bboxes.append([float(v) for v in found.bbox])
                        tables.append({"grid": grid, "bbox": table_bboxes[-1]})
                    page_tables.append(tables)
                    blocks = _blocks_for_page(page)
                    blocks = [b for b in blocks
                              if not any(_overlaps(b["bbox"], tb) > 0.5 for tb in table_bboxes)]
                    page_blocks.append(blocks)
                    total_chars += sum(len(b["text"]) for b in blocks)
                    has_image = has_image or bool(page.get_images())
            page_count = doc.page_count
        finally:
            doc.close()

        warnings: list[Warning] = []
        if total_chars < 50 and has_image:
            warnings.append(Warning("scanned_pdf", None, "疑似扫描件，P0 不做 OCR"))
        if page_count >= 3:
            first = Counter()
            last = Counter()
            for blocks in page_blocks:
                if blocks:
                    first[_norm(blocks[0]["text"])] += 1
                    last[_norm(blocks[-1]["text"])] += 1
            repeated = {
                text for counter in (first, last)
                for text, count in counter.items()
                if text and count / page_count >= 0.6
            }
            for blocks in page_blocks:
                blocks[:] = [b for b in blocks if _norm(b["text"]) not in repeated]

        all_sizes = [b["size"] for blocks in page_blocks for b in blocks if b["size"] > 0]
        body_size = statistics.median(all_sizes) if all_sizes else 10.0

        blocks_out: list[Block] = []
        path_stack: list[str] = []
        table_no = 0
        for page_no, (text_blocks, tables) in enumerate(zip(page_blocks, page_tables), start=1):
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
                grid_rows = [
                    [Cell(text="" if v is None else str(v)) for v in row]
                    for row in table_info["grid"]
                ]
                width = max(len(row) for row in grid_rows)
                grid_rows = [row + [Cell()] * (width - len(row)) for row in grid_rows]
                grid = SheetGrid(name=f"page{page_no}", hidden=False, rows=grid_rows,
                                 merges=[], styles_available=False)
                region = Region(1, grid.nrows, 1, grid.ncols, 1.0)
                decision = detect_header(grid, region, self.settings)
                header = decision.candidate if decision.accepted else None
                forced_columns = None
                if header is None and decision.candidates and self.templates:
                    for cand in decision.candidates[:3]:
                        names = compose_header_names(
                            grid, region, cand.start_row, cand.depth)
                        template = match_template(
                            build_signature(None, region, cand, names), self.templates)
                        if template:
                            from ingest.parsers.header_rules import HeaderCandidate
                            header = HeaderCandidate(
                                cand.start_row, cand.depth, 0.95,
                                dict(cand.features), (), "template")
                            forced_columns = template_columns(template)
                            break
                if header is None and self.settings.header_llm_enabled and self.llm:
                    header, raw = try_llm_header(grid, region, self.llm)
                    if header is None:
                        warnings.append(Warning("ambiguous_header", None, raw[:500]))
                table, table_warnings = build_table(
                    grid, region, header, table_key=f"t{table_no}",
                    settings=self.settings, forced_columns=forced_columns)
                warnings.extend(table_warnings)
                table = table.model_copy(update={
                    "page": page_no, "bbox": table_info["bbox"]})
                blocks_out.append(Block(
                    block_id=f"b{len(blocks_out) + 1}", type="table",
                    table=table, path=list(path_stack), page=page_no,
                    bbox=table_info["bbox"]))

        if not blocks_out:
            warnings.append(Warning("empty_document", None, "PDF 中没有可入库文本"))
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(title=path.stem, source_filename=path.name,
                         sha256=hashlib.sha256(path.read_bytes()).hexdigest()),
            blocks=blocks_out, warnings=warnings,
        )
```

`ingest/parsers/__init__.py` 增加 `pdf` 分支。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_pdf_parser.py -v
```

Expected: 4 passed。`test_table_is_extracted_with_bbox` 若 `find_tables` 未识别，先加 `table_settings={"vertical_strategy":"lines","horizontal_strategy":"lines"}`，不得改成“只要没有表格就算通过”。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/pdf_parser.py ingest/parsers/__init__.py \
  tests/ingest/test_pdf_parser.py
git commit -m "feat(ingest): PDF 文本层解析、页眉页脚剔除与表格回源信息"
```

---

### Task 12: Markdown 解析器（现有语料导入）

**Files:**
- Create: `ingest/parsers/markdown_parser.py`, `tests/ingest/test_markdown_parser.py`
- Modify: `ingest/parsers/__init__.py`

**Interfaces:**
- Consumes: `DocModel`。
- Produces: `MarkdownParser(settings=None)`，`name="markdown"`、`version="0.1.0"`、`parse(path) -> DocModel`；公开 `read_front_matter(path) -> tuple[dict, str]` 供导入 CLI 使用。

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path

from ingest.parsers.markdown_parser import MarkdownParser, read_front_matter

CORPUS = Path(__file__).resolve().parents[2] / "data" / "gov_docs"


def test_reads_front_matter():
    meta, body = read_front_matter(CORPUS / "01_政务公开_办事指南.md")
    assert meta["doc_id"] == "gov-001"
    assert meta["classification"] == "公开"
    assert body.lstrip().startswith("#")


def test_parses_existing_corpus():
    model = MarkdownParser().parse(CORPUS / "01_政务公开_办事指南.md")
    assert model.meta.title == "政务公开与办事指南"
    assert model.meta.effective_date == "2026-01-01"
    assert model.meta.classification is None
    headings = [b.text for b in model.blocks if b.type == "heading"]
    assert "一、办公时间与地点" in headings
    assert any("8:30" in b.text for b in model.blocks if b.type == "paragraph")
    assert model.warnings == []


def test_all_corpus_documents_parse():
    models = [MarkdownParser().parse(p) for p in sorted(CORPUS.glob("*.md"))]
    assert len(models) == 4
    assert all(m.blocks for m in models)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/ingest/test_markdown_parser.py -v
```

Expected: FAIL，`No module named 'ingest.parsers.markdown_parser'`。

- [ ] **Step 3: 写实现**

`ingest/parsers/markdown_parser.py`：

```python
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from ingest.config import Settings, get_settings
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def read_front_matter(path: Path) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ValueError(f"{path.name} 缺少 front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name} front matter 必须是键值对")
    return meta, text[match.end():]


class MarkdownParser:
    name = "markdown"
    version = "0.1.0"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def supports(self, sniff) -> bool:
        return sniff.kind == "markdown"

    def parse(self, path: Path) -> DocModel:
        meta, body = read_front_matter(path)
        blocks: list[Block] = []
        path_stack: list[str] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            text = "\n".join(buffer).strip()
            buffer.clear()
            if text:
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="paragraph",
                    text=text, path=list(path_stack)))

        for line in body.splitlines():
            heading = _HEADING.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                text = heading.group(2).strip()
                path_stack = path_stack[: level - 1] + [text]
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="heading",
                    level=level, text=text, path=list(path_stack)))
            elif not line.strip():
                flush()
            else:
                buffer.append(line)
        flush()

        warnings = [] if blocks else [Warning("empty_document", None, "空文档")]
        effective = meta.get("effective_date")
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(
                title=str(meta.get("title") or path.stem),
                effective_date=str(effective) if effective else None,
                source_filename=path.name,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            ),
            blocks=blocks, warnings=warnings,
        )
```

`ingest/parsers/__init__.py` 增加 `markdown` 分支。

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/ingest/test_markdown_parser.py -v
```

Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
git add ingest/parsers/markdown_parser.py ingest/parsers/__init__.py \
  tests/ingest/test_markdown_parser.py
git commit -m "feat(ingest): Markdown 语料解析器（front matter + 标题层级）"
```

---

### Task 13: 解析流水线（parse job）

**Files:**
- Create: `ingest/repository.py`, `ingest/pipeline.py`, `tests/ingest/test_pipeline.py`

**Interfaces:**
- Consumes: `db`、`queue`、`sniff`、`storage`、`parsers`、`ir`。
- Produces:
  - `ingest.repository`：`create_document`, `get_document`, `lock_document`, `next_version_no`, `create_version`, `get_version`, `set_version_status`, `list_versions`, `ensure_review_item`, `list_open_review_items`, `resolve_review_item`, `add_approval`, `count_approvals`, `set_document_meta`, `list_templates`。
  - `ingest.pipeline.handle_parse(conn, payload)`、`handle_parse_dead_letter(conn, job)`、`HANDLERS`、`DEAD_LETTER_HOOKS`。
  - 解析成功且无需复核：版本 `embedding`，并已入队 `embed`；否则 `needs_review` + `review_items`。

- [ ] **Step 1: 写失败测试**

```python
import hashlib
from dataclasses import replace

import pytest

from ingest import config, pipeline, repository as repo
from ingest.ir import Block, Column, DocMeta, DocModel, ParserInfo, Table
from ingest.queue import claim, enqueue
from ingest.storage import FileStore
from ingest.worker import run_job
from tests.ingest.builders import build_docx


class _FakeParser:
    name = "fake"
    version = "0.1.0"

    def __init__(self, model=None, error=None):
        self.model = model
        self.error = error

    def parse(self, path):
        if self.error:
            raise self.error
        return self.model


def _model(confidence=0.9):
    return DocModel(
        parser=ParserInfo(name="fake", version="0.1.0"),
        meta=DocMeta(title="测试文档", source_filename="a.docx", sha256="a" * 64),
        blocks=[Block(block_id="b1", type="table", table=Table(
            table_key="t1", sheet=None, range="A1:B2",
            header_rows=[1], header_values=[["部门", "金额"]],
            columns=[Column(key="c1", name="部门"),
                     Column(key="c2", name="金额", dtype="number")],
            rows=[["综合处", "120"]], confidence=confidence,
            source="heuristic"))],
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = replace(config.get_settings(), upload_dir=tmp_path / "uploads")
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    path = build_docx(tmp_path / "a.docx", paragraphs=[(None, "正文")])
    store = FileStore(settings.upload_dir)
    rel = store.put_file(path, hashlib.sha256(path.read_bytes()).hexdigest())
    return settings, path, rel


def _seed(conn, rel, classification=1, filename="a.docx"):
    doc = repo.create_document(conn, title="测试文档", dept="综合处",
                               classification=classification, effective_date=None,
                               created_by="zhang")
    version = repo.create_version(
        conn, document_id=doc["id"], version_no=1, sha256="a" * 64,
        filename=filename, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=100, storage_path=rel, parser="docx", parser_version="0.1.0")
    conn.commit()
    return doc, version


def test_auto_embedding_when_confident_and_classified(conn, env, monkeypatch):
    settings, path, rel = env
    doc, version = _seed(conn, rel, classification=1)
    monkeypatch.setattr(pipeline, "get_parser", lambda *a, **k: _FakeParser(_model(0.9)))
    pipeline.handle_parse(conn, {"version_id": version["id"]})

    row = conn.execute("SELECT status FROM doc_versions WHERE id=%s",
                       (version["id"],)).fetchone()
    assert row["status"] == "embedding"
    job = conn.execute("SELECT kind, payload FROM jobs WHERE status='queued'").fetchone()
    assert job["kind"] == "embed"
    assert job["payload"]["version_id"] == version["id"]


def test_unclassified_goes_to_review(conn, env, monkeypatch):
    settings, path, rel = env
    doc, version = _seed(conn, rel, classification=None)
    monkeypatch.setattr(pipeline, "get_parser", lambda *a, **k: _FakeParser(_model(0.9)))
    pipeline.handle_parse(conn, {"version_id": version["id"]})

    assert conn.execute("SELECT status FROM doc_versions WHERE id=%s",
                        (version["id"],)).fetchone()["status"] == "needs_review"
    items = repo.list_open_review_items(conn, version["id"])
    assert [i["kind"] for i in items] == ["classification"]


def test_secret_always_goes_to_two_person_review(conn, env, monkeypatch):
    settings, path, rel = env
    doc, version = _seed(conn, rel, classification=2)
    monkeypatch.setattr(pipeline, "get_parser", lambda *a, **k: _FakeParser(_model(0.9)))
    pipeline.handle_parse(conn, {"version_id": version["id"]})
    items = repo.list_open_review_items(conn, version["id"])
    assert [i["kind"] for i in items] == ["classification"]
    assert items[0]["payload"]["reason"] == "secret_two_person"


def test_low_confidence_goes_to_structure_review(conn, env, monkeypatch):
    settings, path, rel = env
    doc, version = _seed(conn, rel, classification=1)
    monkeypatch.setattr(pipeline, "get_parser", lambda *a, **k: _FakeParser(_model(0.5)))
    pipeline.handle_parse(conn, {"version_id": version["id"]})
    items = repo.list_open_review_items(conn, version["id"])
    assert [i["kind"] for i in items] == ["structure"]


def test_trusted_import_skips_review(conn, env, monkeypatch):
    settings, path, rel = env
    doc, version = _seed(conn, rel, classification=2)
    monkeypatch.setattr(pipeline, "get_parser", lambda *a, **k: _FakeParser(_model(0.5)))
    pipeline.handle_parse(conn, {"version_id": version["id"], "trusted_import": True})
    assert conn.execute("SELECT status FROM doc_versions WHERE id=%s",
                        (version["id"],)).fetchone()["status"] == "embedding"
    assert repo.list_open_review_items(conn, version["id"]) == []


def test_parse_failure_dead_letters_to_review(conn, env, monkeypatch):
    settings, path, rel = env
    doc, version = _seed(conn, rel, classification=1)
    monkeypatch.setattr(pipeline, "get_parser",
                        lambda *a, **k: _FakeParser(error=RuntimeError("bad file")))
    job_id = enqueue(conn, "parse", {"version_id": version["id"]}, max_attempts=0)
    job = claim(conn, "w")
    assert run_job(conn, job, pipeline.HANDLERS, "w",
                   pipeline.DEAD_LETTER_HOOKS) is False
    row = conn.execute("SELECT status, error FROM doc_versions WHERE id=%s",
                       (version["id"],)).fetchone()
    assert row["status"] == "failed"
    assert "bad file" in row["error"]
    items = repo.list_open_review_items(conn, version["id"])
    assert [i["kind"] for i in items] == ["parse_warning"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_pipeline.py -v
```

Expected: FAIL，`No module named 'ingest.repository'`。

- [ ] **Step 3: 写实现**

`ingest/repository.py`：

```python
from __future__ import annotations

from datetime import date

from psycopg import Connection
from psycopg.types.json import Jsonb

_DOC_FIELDS = {"title", "dept", "classification", "effective_date"}


def create_document(conn: Connection, *, title: str, dept: str,
                    classification: int | None, effective_date: date | str | None,
                    created_by: str) -> dict:
    return conn.execute(
        """INSERT INTO documents (title, dept, classification, effective_date, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (title, dept, classification, effective_date, created_by),
    ).fetchone()


def get_document(conn: Connection, document_id: str) -> dict | None:
    return conn.execute("SELECT * FROM documents WHERE id=%s", (document_id,)).fetchone()


def lock_document(conn: Connection, document_id: str) -> dict | None:
    return conn.execute("SELECT * FROM documents WHERE id=%s FOR UPDATE",
                        (document_id,)).fetchone()


def next_version_no(conn: Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM doc_versions WHERE document_id=%s",
        (document_id,),
    ).fetchone()
    return int(row["n"])


def create_version(conn: Connection, *, document_id: str, version_no: int,
                   sha256: str, filename: str, mime: str, size_bytes: int,
                   storage_path: str, parser: str, parser_version: str,
                   status: str = "uploaded") -> dict:
    return conn.execute(
        """INSERT INTO doc_versions
           (document_id, version_no, sha256, filename, mime, size_bytes,
            storage_path, parser, parser_version, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (document_id, version_no, sha256, filename, mime, size_bytes,
         storage_path, parser, parser_version, status),
    ).fetchone()


def get_version(conn: Connection, version_id: str) -> dict | None:
    return conn.execute("SELECT * FROM doc_versions WHERE id=%s", (version_id,)).fetchone()


def list_versions(conn: Connection, document_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM doc_versions WHERE document_id=%s ORDER BY version_no DESC",
        (document_id,),
    ).fetchall()


def set_version_status(conn: Connection, version_id: str, status: str, *,
                       ir: object = None, warnings: list | None = None,
                       error: str | None = None) -> None:
    sets = ["status=%s"]
    params: list = [status]
    if ir is not None:
        sets.append("ir=%s")
        params.append(Jsonb(ir.model_dump(mode="json") if hasattr(ir, "model_dump") else ir))
    if warnings is not None:
        sets.append("warnings=%s")
        params.append(Jsonb(warnings))
    if error is not None:
        sets.append("error=%s")
        params.append(error)
    params.append(version_id)
    conn.execute(f"UPDATE doc_versions SET {', '.join(sets)} WHERE id=%s", params)


def set_document_meta(conn: Connection, document_id: str, **fields) -> None:
    updates = {k: v for k, v in fields.items() if k in _DOC_FIELDS}
    if not updates:
        return
    sets = ", ".join(f"{k}=%s" for k in updates)
    conn.execute(
        f"UPDATE documents SET {sets}, updated_at=now() WHERE id=%s",
        (*updates.values(), document_id),
    )


def ensure_review_item(conn: Connection, version_id: str, kind: str,
                       payload: dict) -> int:
    reason = payload.get("reason")
    row = conn.execute(
        """SELECT id FROM review_items
           WHERE version_id=%s AND kind=%s AND status='open'
             AND payload->>'reason' = %s ORDER BY id LIMIT 1""",
        (version_id, kind, reason),
    ).fetchone()
    if row:
        return int(row["id"])
    created = conn.execute(
        """INSERT INTO review_items (version_id, kind, payload)
           VALUES (%s, %s, %s) RETURNING id""",
        (version_id, kind, Jsonb(payload)),
    ).fetchone()
    return int(created["id"])


def list_open_review_items(conn: Connection, version_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM review_items WHERE version_id=%s AND status='open' ORDER BY id",
        (version_id,),
    ).fetchall()


def resolve_review_item(conn: Connection, item_id: int, *, status: str = "resolved",
                        resolved_by: str, note: str | None = None) -> None:
    conn.execute(
        """UPDATE review_items SET status=%s, resolved_by=%s, resolved_at=now(), note=%s
           WHERE id=%s AND status='open'""",
        (status, resolved_by, note, item_id),
    )


def add_approval(conn: Connection, item_id: int, user_id: str, decision: str = "approve",
                 note: str | None = None) -> bool:
    row = conn.execute(
        """INSERT INTO review_approvals (review_item_id, user_id, decision, note)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (review_item_id, user_id) DO NOTHING
           RETURNING id""",
        (item_id, user_id, decision, note),
    ).fetchone()
    return row is not None


def count_approvals(conn: Connection, item_id: int, decision: str = "approve") -> int:
    return int(conn.execute(
        "SELECT count(*) AS n FROM review_approvals WHERE review_item_id=%s AND decision=%s",
        (item_id, decision),
    ).fetchone()["n"])


def list_templates(conn: Connection, dept: str) -> list[dict]:
    return conn.execute(
        """SELECT * FROM layout_templates
           WHERE scope_dept IS NULL OR scope_dept=%s ORDER BY updated_at DESC""",
        (dept,),
    ).fetchall()
```

`ingest/pipeline.py`：

```python
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from psycopg import Connection

from app.audit import write_audit
from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.errors import Quarantined
from ingest.parsers import get_parser
from ingest.queue import enqueue
from ingest.sniff import sniff_file, strip_macros
from ingest.storage import FileStore

_STRUCTURE_CODES = {"table_no_header", "ambiguous_header", "scanned_pdf", "empty_document"}


def _needs_structure_review(ir, settings: Settings) -> bool:
    if any(w.code in _STRUCTURE_CODES for w in ir.warnings):
        return True
    return any(t.confidence < settings.header_accept for t in ir.tables())


def _parser_for(conn: Connection, kind: str, dept: str, settings: Settings):
    templates = repo.list_templates(conn, dept)
    llm = None
    if settings.header_llm_enabled:
        import main as core
        llm = core.init_llm()
    return get_parser(kind, templates=templates, llm=llm, settings=settings)


def _parse_file(conn: Connection, version: dict, source: Path,
                settings: Settings):
    sniff = sniff_file(source, version["filename"], settings)
    path = source
    tmpdir = None
    if sniff.macro:
        tmpdir = tempfile.mkdtemp(prefix="rag-macro-")
        stripped = Path(tmpdir) / Path(version["filename"]).name
        if not strip_macros(source, stripped):
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise Quarantined("宏剥离失败")
        path = stripped
    try:
        parser = _parser_for(conn, sniff.kind, version_dept := _dept_of(conn, version), settings)
        return parser.parse(path)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _dept_of(conn: Connection, version: dict) -> str:
    doc = repo.get_document(conn, version["document_id"])
    return doc["dept"]


def handle_parse(conn: Connection, payload: dict) -> None:
    settings = get_settings()
    version = repo.get_version(conn, payload["version_id"])
    if version is None:
        raise ValueError(f"版本不存在: {payload['version_id']}")
    doc = repo.get_document(conn, version["document_id"])
    repo.set_version_status(conn, version["id"], "parsing")
    conn.commit()

    source = FileStore(settings.upload_dir).resolve(version["storage_path"])
    ir = _parse_file(conn, version, source, settings)
    warnings = [w.model_dump() for w in ir.warnings]

    if not payload.get("trusted_import"):
        if _needs_structure_review(ir, settings):
            repo.ensure_review_item(conn, version["id"], "structure", {
                "reason": "structure",
                "tables": [t.model_dump(mode="json") for t in ir.tables()],
                "warnings": [w.code for w in ir.warnings],
            })
        if doc["classification"] is None:
            repo.ensure_review_item(conn, version["id"], "classification",
                                    {"reason": "unclassified"})
        elif doc["classification"] == 2:
            repo.ensure_review_item(conn, version["id"], "classification",
                                    {"reason": "secret_two_person"})
    elif doc["classification"] is None:
        raise ValueError("trusted_import 必须先在 documents 上定密级")

    items = repo.list_open_review_items(conn, version["id"])
    if items:
        repo.set_version_status(conn, version["id"], "needs_review",
                                ir=ir, warnings=warnings)
        conn.commit()
        return
    repo.set_version_status(conn, version["id"], "embedding", ir=ir, warnings=warnings)
    enqueue(conn, "embed", {"version_id": version["id"]}, priority=50)


def handle_parse_dead_letter(conn: Connection, job: dict) -> None:
    version_id = job["payload"]["version_id"]
    row = conn.execute("SELECT last_error FROM jobs WHERE id=%s", (job["id"],)).fetchone()
    error = (row or {}).get("last_error") or "parse failed"
    repo.set_version_status(conn, version_id, "failed", error=error)
    repo.ensure_review_item(conn, version_id, "parse_warning",
                            {"reason": "parse_failed", "error": error})
    conn.commit()
    write_audit({"action": "parse_failed", "version_id": version_id, "detail": error})


HANDLERS = {"parse": handle_parse}
DEAD_LETTER_HOOKS = {"parse": handle_parse_dead_letter}
```

> `_parse_file` 里的海象表达式只为少写一行，实现时直接写 `dept = _dept_of(conn, version)` 更清楚。下面的代码以此为准：

```python
        dept = _dept_of(conn, version)
        parser = _parser_for(conn, sniff.kind, dept, settings)
        return parser.parse(path)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_pipeline.py -v
```

Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add ingest/repository.py ingest/pipeline.py tests/ingest/test_pipeline.py
git commit -m "feat(ingest): 解析流水线、复核项生成与失败转人工"
```

---

### Task 14: 上传、列表、详情、下线、删除 API

**Files:**
- Modify: `app/users.py`（增加 `roles`）、`app/server.py`（挂载路由 + 错误映射）、`ingest/errors.py`（`NotFound` / `Forbidden` / `DocumentInactive`）、`tests/ingest/conftest.py`（`clean_db` fixture）
- Create: `app/deps.py`、`app/routers/__init__.py`、`app/routers/documents.py`、`ingest/upload_service.py`、`ingest/activation.py`、`tests/ingest/test_documents_api.py`

**Interfaces:**
- Consumes: `ingest.repository`、`ingest.queue.enqueue/queue_depth`、`ingest.storage.FileStore`、`ingest.errors`、`app.users.User`。
- Produces:
  - `app.deps.current_user(request) -> User`（委托 `server.current_user`，避免循环导入）、`app.deps.get_db()`、`app.deps.require_roles(user, *roles)`、`app.deps.parse_classification(value) -> int | None`、`app.deps.parse_date(value) -> date | None`、`app.deps.client_ip(request) -> str`、`app.deps.audit_event(request, user, action, **fields)`。
  - `ingest.upload_service.save_upload(conn, src, filename, content_type, *, created_by, dept, classification, effective_date=None, title=None, document_id=None, settings=None) -> dict`，返回 `{"document_id","version_id","job_id","status"}`。
  - `ingest.activation.deactivate_document(conn, document_id, *, actor) -> dict`、`ingest.activation.soft_delete_document(conn, document_id, *, actor) -> dict`（Task 20 继续在此文件加 `activate_version` / `purge_document`）。
  - HTTP：`POST /api/documents`、`GET /api/documents`、`GET /api/documents/{id}`、`POST /api/documents/{id}/versions`、`POST /api/documents/{id}/deactivate`、`DELETE /api/documents/{id}`。
  - `User.roles: tuple[str, ...]`；演示账号 zhang/li = `("uploader","reviewer")`，wang/chen = `("uploader","reviewer","admin")`。

- [ ] **Step 1: 写失败测试**

`tests/ingest/conftest.py` 追加（保留现有 fixture）：

```python
@pytest.fixture()
def clean_db(migrated):
    """给 API 测试用：不依赖 conn fixture，也能保证每个用例从空库开始。"""
    with psycopg.connect(migrated, row_factory=dict_row) as conn:
        conn.execute(TRUNCATE_SQL)
        conn.commit()
    return migrated
```

`tests/ingest/test_documents_api.py`：

```python
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from ingest import repository as repo
from ingest.config import get_settings
from ingest.db import reset_pool_for_tests
from tests.ingest.builders import build_xlsx

PASSWORD = "test-pass-123"


def login(client: TestClient, username: str) -> TestClient:
    resp = client.post("/api/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


@pytest.fixture()
def app_client(clean_db, tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_DB_URL", clean_db)
    monkeypatch.setenv("INGEST_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    reset_pool_for_tests()
    from app import server

    client = TestClient(server.app)
    login(client, "wang")
    yield client, clean_db
    reset_pool_for_tests()
    get_settings.cache_clear()


def fetch_all(db_url: str, sql: str, params: tuple = ()) -> list[dict]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def upload(client: TestClient, path, **form):
    return client.post(
        "/api/documents",
        files={"file": (path.name, path.read_bytes())},
        data={k: str(v) for k, v in form.items()},
    )


def test_upload_creates_document_version_and_parse_job(app_client, tmp_path):
    client, db_url = app_client
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])

    resp = upload(client, xlsx, title="2025预算", classification=1,
                  effective_date="2026-01-01")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "uploaded" and body["job_id"] > 0

    docs = fetch_all(db_url, "SELECT * FROM documents")
    assert docs[0]["title"] == "2025预算"
    assert docs[0]["dept"] == "财务处"
    assert docs[0]["classification"] == 1
    assert str(docs[0]["effective_date"]) == "2026-01-01"

    versions = fetch_all(db_url, "SELECT * FROM doc_versions")
    assert versions[0]["status"] == "uploaded"
    assert versions[0]["parser"] == "xlsx"
    assert len(versions[0]["sha256"]) == 64
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        stored = conn.execute("SELECT storage_path FROM doc_versions").fetchone()["storage_path"]
    assert (tmp_path / "uploads" / stored).exists()

    jobs = fetch_all(db_url, "SELECT * FROM jobs")
    assert jobs[0]["kind"] == "parse"
    assert jobs[0]["payload"]["version_id"] == str(versions[0]["id"])


def test_rejects_unsupported_type_and_oversize(app_client, tmp_path, monkeypatch):
    client, db_url = app_client
    bad = client.post("/api/documents", files={"file": ("a.exe", b"MZ")})
    assert bad.status_code == 400
    assert bad.json()["code"] == "unsupported_type"

    monkeypatch.setenv("INGEST_MAX_FILE_MB", "1")
    get_settings.cache_clear()
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
    large = client.post("/api/documents", files={"file": ("a.pdf", big)})
    assert large.status_code == 413
    assert large.json()["code"] == "too_large"
    assert fetch_all(db_url, "SELECT count(*) AS n FROM documents")[0]["n"] == 0


def test_queue_full_returns_429(app_client, tmp_path, monkeypatch):
    client, _ = app_client
    monkeypatch.setenv("INGEST_QUEUE_MAX", "0")
    get_settings.cache_clear()
    xlsx = build_xlsx(tmp_path / "a.xlsx", [["部门", "金额"], ["综合处", 1]])
    resp = upload(client, xlsx)
    assert resp.status_code == 429
    assert resp.json()["code"] == "queue_full"


def test_duplicate_content_and_new_version(app_client, tmp_path):
    client, db_url = app_client
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    first = upload(client, xlsx)
    assert first.status_code == 202
    doc_id = first.json()["document_id"]

    dup = upload(client, xlsx, document_id=doc_id)
    assert dup.status_code == 409
    assert dup.json()["code"] == "duplicate_content"

    updated = build_xlsx(tmp_path / "预算v2.xlsx", [["部门", "金额"], ["综合处", 20]])
    second = upload(client, updated, document_id=doc_id)
    assert second.status_code == 202
    assert second.json()["document_id"] == doc_id
    versions = fetch_all(db_url, "SELECT version_no FROM doc_versions ORDER BY version_no")
    assert [v["version_no"] for v in versions] == [1, 2]


def test_department_and_classification_permissions(app_client, tmp_path):
    from app import server

    client, _ = app_client
    zhang = login(TestClient(server.app), "zhang")
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])

    too_secret = upload(zhang, xlsx, classification=2)
    assert too_secret.status_code == 403

    other_dept = upload(zhang, xlsx, dept="人事处")
    assert other_dept.status_code == 403


def test_list_and_detail_hide_other_departments(app_client, tmp_path, monkeypatch):
    from dataclasses import replace

    from app.users import DEMO_USERS

    # 列表的可见性是「检索 ACL + 本部门待定级」；管理员看全部。
    # 这里临时摘掉 wang 的 admin，验证普通上传者的过滤。
    monkeypatch.setitem(DEMO_USERS, "wang",
                        replace(DEMO_USERS["wang"], roles=("uploader", "reviewer")))
    client, db_url = app_client
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    upload(client, xlsx, title="2025预算", classification=1)

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        repo.create_document(conn, title="人事名册", dept="人事处", classification=1,
                             effective_date=None, created_by="li")
        conn.commit()

    listing = client.get("/api/documents").json()
    assert listing["total"] == 1
    assert [item["title"] for item in listing["items"]] == ["2025预算"]

    other_id = fetch_all(db_url, "SELECT id FROM documents WHERE dept='人事处'")[0]["id"]
    assert client.get(f"/api/documents/{other_id}").status_code == 404

    detail = client.get(f"/api/documents/{fetch_all(db_url, 'SELECT id FROM documents WHERE dept=%s', ('财务处',))[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["versions"][0]["version_no"] == 1


def test_deactivate_and_soft_delete(app_client, tmp_path):
    client, db_url = app_client
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    body = upload(client, xlsx, classification=1).json()
    doc_id, version_id = body["document_id"], body["version_id"]

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        conn.execute(
            """INSERT INTO chunks (document_id, version_id, ordinal, chunk_type, content,
                   content_tokens, embed_model, dept, classification, chunk_hash, active)
               VALUES (%s, %s, 1, 'text', '内容', '内容', 'bge-m3', '财务处', 1, %s, true)""",
            (doc_id, version_id, "a" * 64),
        )
        conn.execute("UPDATE documents SET status='active', current_version_id=%s WHERE id=%s",
                     (version_id, doc_id))
        conn.commit()

    resp = client.post(f"/api/documents/{doc_id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
    assert fetch_all(db_url, "SELECT active FROM chunks")[0]["active"] is False
    assert fetch_all(db_url, "SELECT status FROM documents")[0]["status"] == "inactive"

    deleted = client.delete(f"/api/documents/{doc_id}")
    assert deleted.status_code == 200
    row = fetch_all(db_url, "SELECT status, deleted_at FROM documents")[0]
    assert row["status"] == "deleting" and row["deleted_at"] is not None
    purge = fetch_all(db_url, "SELECT * FROM jobs WHERE kind='purge'")
    assert len(purge) == 1
    assert purge[0]["payload"]["document_id"] == doc_id


def test_delete_requires_admin(app_client, tmp_path):
    from app import server

    client, db_url = app_client
    xlsx = build_xlsx(tmp_path / "预算.xlsx", [["部门", "金额"], ["综合处", 10]])
    doc_id = upload(client, xlsx).json()["document_id"]

    # 临时把 wang 的 admin 角色摘掉，验证越权被拒
    from app.users import DEMO_USERS
    original = DEMO_USERS["wang"]
    DEMO_USERS["wang"] = type(original)(
        username=original.username, display_name=original.display_name,
        dept=original.dept, max_level=original.max_level, role=original.role,
        roles=("uploader", "reviewer"))
    try:
        assert client.delete(f"/api/documents/{doc_id}").status_code == 403
    finally:
        DEMO_USERS["wang"] = original
```

- [ ] **Step 2: 运行测试确认失败**

```bash
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_documents_api.py -v
```

Expected: FAIL，`No module named 'app.deps'`。

- [ ] **Step 3: 写实现**

`ingest/errors.py` 追加：

```python
class NotFound(IngestError):
    code, http_status = "not_found", 404


class Forbidden(IngestError):
    code, http_status = "forbidden", 403


class DocumentInactive(IngestError):
    code, http_status = "document_inactive", 409
```

`app/users.py`：`User` 增加字段（放在 `role` 之后，保持现有 5 参数构造兼容）：

```python
    roles: tuple[str, ...] = ()
```

`to_dict()` 增加一行 `"roles": list(self.roles),`。`DEMO_USERS` 四个账号补 `roles=`：

```python
DEMO_USERS: dict[str, User] = {
    "zhang": User("zhang", "张伟", "综合处", max_level=1, role="工作人员",
                  roles=("uploader", "reviewer")),
    "li": User("li", "李娜", "人事处", max_level=1, role="人事专员",
               roles=("uploader", "reviewer")),
    "wang": User("wang", "王强", "财务处", max_level=2, role="财务主管",
                 roles=("uploader", "reviewer", "admin")),
    "chen": User("chen", "陈静", "信息中心", max_level=2, role="安全管理员",
                 roles=("uploader", "reviewer", "admin")),
}
```

`app/deps.py`：

```python
from __future__ import annotations

from datetime import date
from typing import Iterator
from uuid import uuid4

from fastapi import HTTPException, Request
from psycopg import Connection

from app.users import CLASSIFICATION_LEVELS, User


def current_user(request: Request) -> User:
    """延迟导入 server，避免 server → routers → deps 的循环导入。"""
    from app import server

    return server.current_user(request)


def get_db() -> Iterator[Connection]:
    from ingest.db import connection

    with connection() as conn:
        yield conn


def require_roles(user: User, *roles: str) -> None:
    if not set(roles) & set(user.roles):
        raise HTTPException(status_code=403, detail="无权限执行该操作")


def parse_classification(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        level = value
    elif str(value).isdigit():
        level = int(value)
    else:
        level = CLASSIFICATION_LEVELS.get(str(value))
    if level not in (0, 1, 2):
        raise HTTPException(status_code=400,
                            detail="classification 只能是 0/1/2 或 公开/内部/秘密")
    return level


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400,
                            detail="effective_date 必须是 YYYY-MM-DD") from exc


def client_ip(request: Request) -> str:
    from app import server

    return server._client_ip(request)


def audit_event(request: Request, user: User, action: str, **fields) -> None:
    from app.audit import write_audit

    write_audit({
        "request_id": request.headers.get("x-request-id") or uuid4().hex,
        "action": action,
        "user": user.username,
        "dept": user.dept,
        "role": user.role,
        "ip": client_ip(request),
        **fields,
    })
```

`ingest/upload_service.py`：

```python
from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import BinaryIO

from psycopg import Connection

from ingest import repository as repo
from ingest.config import Settings, get_settings
from ingest.errors import (
    DocumentInactive, DuplicateContent, Forbidden, NotFound, QueueFull, TooLarge,
    UnsupportedType,
)
from ingest.queue import enqueue, queue_depth
from ingest.storage import FileStore

SUPPORTED = {
    ".docx": ("docx", "0.1.0"),
    ".doc": ("doc", "0.1.0"),
    ".xlsx": ("xlsx", "0.1.0"),
    ".xlsm": ("xlsx", "0.1.0"),
    ".pdf": ("pdf", "0.1.0"),
}
READ_SIZE = 1024 * 1024


def save_upload(conn: Connection, src: BinaryIO, filename: str,
                content_type: str | None, *, created_by: str, dept: str,
                classification: int | None, effective_date: date | None = None,
                title: str | None = None, document_id: str | None = None,
                settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise UnsupportedType(f"不支持的文件类型: {suffix or '无扩展名'}")
    if queue_depth(conn) >= settings.queue_max:
        raise QueueFull("解析队列已满，请稍后重试")
    parser, parser_version = SUPPORTED[suffix]

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.upload_dir / f".upload-{os.getpid()}-{id(src)}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = src.read(READ_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise TooLarge(f"文件超过 {settings.max_file_mb} MB")
                digest.update(chunk)
                out.write(chunk)
        sha256 = digest.hexdigest()
        storage_path = FileStore(settings.upload_dir).put_file(tmp, sha256)
    finally:
        tmp.unlink(missing_ok=True)

    if document_id:
        doc = repo.lock_document(conn, document_id)
        if doc is None:
            raise NotFound("文档不存在")
        if doc["deleted_at"] is not None or doc["status"] == "deleting":
            raise DocumentInactive("文档已删除，不能追加版本")
        if doc["dept"] != dept:
            raise Forbidden("只能为本部门文档追加版本")
        updates: dict = {}
        if classification is not None:
            updates["classification"] = classification
        if effective_date is not None:
            updates["effective_date"] = effective_date
        if title:
            updates["title"] = title
        if updates:
            repo.set_document_meta(conn, document_id, **updates)
    else:
        doc = repo.create_document(
            conn, title=title or Path(filename).stem, dept=dept,
            classification=classification, effective_date=effective_date,
            created_by=created_by,
        )
        document_id = doc["id"]

    duplicate = conn.execute(
        "SELECT id FROM doc_versions WHERE document_id=%s AND sha256=%s",
        (document_id, sha256),
    ).fetchone()
    if duplicate:
        conn.rollback()
        raise DuplicateContent("相同内容已上传")

    version = repo.create_version(
        conn, document_id=document_id,
        version_no=repo.next_version_no(conn, document_id),
        sha256=sha256, filename=Path(filename).name,
        mime=content_type or "application/octet-stream", size_bytes=size,
        storage_path=storage_path, parser=parser, parser_version=parser_version,
    )
    job_id = enqueue(conn, "parse", {"version_id": version["id"]}, priority=50)
    return {"document_id": document_id, "version_id": version["id"],
            "job_id": job_id, "status": "uploaded"}
```

`ingest/activation.py`（Task 20 继续扩展）：

```python
from __future__ import annotations

from psycopg import Connection

from ingest import repository as repo
from ingest.errors import NotFound
from ingest.queue import enqueue


def deactivate_document(conn: Connection, document_id: str, *, actor: str) -> dict:
    if repo.lock_document(conn, document_id) is None:
        raise NotFound("文档不存在")
    conn.execute("UPDATE chunks SET active=false WHERE document_id=%s", (document_id,))
    conn.execute(
        "UPDATE documents SET status='inactive', updated_at=now() WHERE id=%s",
        (document_id,),
    )
    conn.commit()
    return {"document_id": document_id, "status": "inactive"}


def soft_delete_document(conn: Connection, document_id: str, *, actor: str) -> dict:
    doc = repo.lock_document(conn, document_id)
    if doc is None:
        raise NotFound("文档不存在")
    if doc["status"] == "deleting":
        return {"document_id": document_id, "status": "deleting", "queued": False}
    conn.execute("UPDATE chunks SET active=false WHERE document_id=%s", (document_id,))
    conn.execute(
        """UPDATE documents SET status='deleting', deleted_at=now(), updated_at=now()
           WHERE id=%s""",
        (document_id,),
    )
    enqueue(conn, "purge", {"document_id": document_id}, priority=200)
    return {"document_id": document_id, "status": "deleting", "queued": True}
```

`app/routers/__init__.py`：空文件。

`app/routers/documents.py`：

```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from psycopg import Connection

from app.deps import (
    audit_event, current_user, get_db, parse_classification, parse_date, require_roles,
)
from app.users import User, can_view
from ingest import repository as repo
from ingest.activation import deactivate_document, soft_delete_document
from ingest.upload_service import save_upload

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _visible(user: User, doc: dict) -> bool:
    """管理台可见性 = 检索 ACL + 本部门待定级（否则复核台打不开）。"""
    if "admin" in user.roles:
        return True
    if doc["dept"] == user.dept and doc["classification"] is None:
        return True
    return can_view(user, {
        "classification_level": doc["classification"],
        "dept": doc["dept"],
    })


def _can_manage(user: User, doc: dict) -> bool:
    if "admin" in user.roles:
        return True
    if not {"uploader", "reviewer"} & set(user.roles):
        return False
    return doc["dept"] == user.dept


def _store(request: Request, user: User, conn: Connection, file: UploadFile,
           *, title: str | None, dept: str | None, classification: str | None,
           effective_date: str | None, document_id: str | None) -> dict:
    require_roles(user, "uploader", "admin")
    target_dept = dept or user.dept
    if target_dept != user.dept and "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="只能上传到本部门")
    level = parse_classification(classification)
    if level is not None and level > user.max_level:
        raise HTTPException(status_code=403, detail="密级超过你的权限")
    result = save_upload(
        conn, file.file, file.filename or "", file.content_type,
        created_by=user.username, dept=target_dept, classification=level,
        effective_date=parse_date(effective_date), title=title,
        document_id=document_id,
    )
    audit_event(request, user, "upload", doc_id=result["document_id"],
                version_id=result["version_id"], classification=level,
                detail={"filename": file.filename, "size_bytes": file.size})
    return result


@router.post("", status_code=202)
def upload_document(request: Request, file: UploadFile = File(...),
                    title: str | None = Form(None), dept: str | None = Form(None),
                    classification: str | None = Form(None),
                    effective_date: str | None = Form(None),
                    document_id: str | None = Form(None),
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    return _store(request, user, conn, file, title=title, dept=dept,
                  classification=classification, effective_date=effective_date,
                  document_id=document_id)


@router.post("/{document_id}/versions", status_code=202)
def upload_version(document_id: str, request: Request, file: UploadFile = File(...),
                   title: str | None = Form(None), dept: str | None = Form(None),
                   classification: str | None = Form(None),
                   effective_date: str | None = Form(None),
                   user: User = Depends(current_user),
                   conn: Connection = Depends(get_db)) -> dict:
    return _store(request, user, conn, file, title=title, dept=dept,
                  classification=classification, effective_date=effective_date,
                  document_id=document_id)


@router.get("")
def list_documents(status: str | None = None, dept: str | None = None,
                   q: str | None = None, page: int = 1, page_size: int = 20,
                   user: User = Depends(current_user),
                   conn: Connection = Depends(get_db)) -> dict:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where = ["d.deleted_at IS NULL"]
    params: list[Any] = []
    if "admin" not in user.roles:
        where.append("((d.classification IS NOT NULL AND d.classification <= %s "
                     "AND (d.classification = 0 OR d.dept = %s)) "
                     "OR (d.dept = %s AND d.classification IS NULL))")
        params += [user.max_level, user.dept, user.dept]
    if dept:
        where.append("d.dept = %s")
        params.append(dept)
    if q:
        where.append("d.title ILIKE %s")
        params.append(f"%{q}%")
    if status:
        where.append("v.status = %s")
        params.append(status)
    cond = " AND ".join(where)
    base = f"""FROM documents d
               LEFT JOIN LATERAL (
                   SELECT status, version_no FROM doc_versions v
                   WHERE v.document_id = d.id ORDER BY version_no DESC LIMIT 1
               ) v ON true
               WHERE {cond}"""
    total = conn.execute(f"SELECT count(*) AS n {base}", params).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT d.id, d.title, d.dept, d.classification, d.status,
                   d.effective_date, d.created_at, d.updated_at,
                   v.status AS version_status, v.version_no
            {base}
            ORDER BY d.updated_at DESC, d.id
            LIMIT %s OFFSET %s""",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {"total": int(total), "page": page, "page_size": page_size,
            "items": [dict(row) for row in rows]}


@router.get("/{document_id}")
def get_document(document_id: str, user: User = Depends(current_user),
                 conn: Connection = Depends(get_db)) -> dict:
    doc = repo.get_document(conn, document_id)
    if doc is None or doc["deleted_at"] is not None or not _visible(user, doc):
        raise HTTPException(status_code=404, detail="文档不存在")
    open_reviews = conn.execute(
        """SELECT count(*) AS n FROM review_items r
           JOIN doc_versions v ON v.id = r.version_id
           WHERE v.document_id=%s AND r.status='open'""",
        (document_id,),
    ).fetchone()["n"]
    return {"document": doc, "versions": repo.list_versions(conn, document_id),
            "open_reviews": int(open_reviews)}


@router.post("/{document_id}/deactivate")
def deactivate(document_id: str, request: Request,
               user: User = Depends(current_user),
               conn: Connection = Depends(get_db)) -> dict:
    doc = repo.get_document(conn, document_id)
    if doc is None or doc["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if not _can_manage(user, doc):
        raise HTTPException(status_code=403, detail="无权下线该文档")
    result = deactivate_document(conn, document_id, actor=user.username)
    audit_event(request, user, "deactivate", doc_id=document_id,
                classification=doc["classification"])
    return result


@router.delete("/{document_id}")
def delete_document(document_id: str, request: Request,
                    user: User = Depends(current_user),
                    conn: Connection = Depends(get_db)) -> dict:
    require_roles(user, "admin")
    doc = repo.get_document(conn, document_id)
    if doc is None or doc["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="文档不存在")
    result = soft_delete_document(conn, document_id, actor=user.username)
    audit_event(request, user, "delete", doc_id=document_id,
                classification=doc["classification"])
    return result
```

`app/server.py` 三处修改：

1. 导入与错误映射（放在 `app = FastAPI(...)` 之后）：

```python
from ingest.errors import IngestError


@app.exception_handler(IngestError)
async def _ingest_error_handler(request: Request, exc: IngestError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "detail": str(exc), **exc.detail},
    )
```

2. 文件末尾（`if __name__ == "__main__":` 之前）挂载路由：

```python
from app.routers import documents as documents_api

app.include_router(documents_api.router)
```

3. `server.write_audit` 兼容包装保持不变；**不要**改现有调用点。

> `app.audit.write_audit` 必须保留现有 server 实现里的进程内锁与 `AUDIT_FSYNC` 语义（Task 1 已抽取），否则并发问答时审计行会交错。

- [ ] **Step 4: 运行测试确认通过**

```bash
INGEST_TEST_DB_URL=postgresql://rag:rag@127.0.0.1:5433/rag_test \
  uv run pytest tests/ingest/test_documents_api.py -v
```

Expected: 8 passed。

- [ ] **Step 5: 回归现有测试**

```bash
uv run pytest tests/test_main.py tests/test_web.py -q
```

Expected: 73 passed（新增路由不影响 FAISS 路径）。

- [ ] **Step 6: Commit**

```bash
git add app/deps.py app/users.py app/server.py app/routers/__init__.py \
  app/routers/documents.py ingest/errors.py ingest/upload_service.py \
  ingest/activation.py tests/ingest/conftest.py tests/ingest/test_documents_api.py
git commit -m "feat(api): 文档上传、列表、详情、下线与软删除"
```

---
