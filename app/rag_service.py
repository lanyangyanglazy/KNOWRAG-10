"""
政府级 RAG 试点原型：服务层（语料 → 权限 → 检索 → 生成 → 拒答）。

设计要点（对应 docs/政府级RAG改造方案.md 第 4 章）：

1. **权限前置**：先按用户裁剪出可见文档集合，再做检索。
   - 稠密检索：FAISS `filter` 回调，只有可见切片能进入候选；
   - 稀疏检索：BM25 语料只由可见切片构建。
   先检索再删结果等于把密级内容送进了模型上下文，属于泄露。

2. **拒答**：可见集合为空、或最佳命中相关性不足时，直接返回拒答事件，
   不调用生成模型——既省算力，也避免模型「硬答」编造。

3. **复用教学主线**：切分、分词、RRF、追问改写、流式 <think> 过滤全部复用
   main.py，本文件只增加政务场景需要的元数据、ACL 与拒答。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List

import yaml
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document as LCDocument
from langchain_core.messages import AIMessage, HumanMessage
from langchain_text_splitters import RecursiveCharacterTextSplitter

# app/ 在仓库根目录下；把根目录加入 sys.path，才能复用 main.py 的检索与生成组件
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import main as core  # noqa: E402  （复用 tokenize / RRF / Prompt / strip_think_stream）

from app.users import CLASSIFICATION_LEVELS, User, can_view  # noqa: E402

# ---------- 路径与参数 ----------
GOV_DOCS_DIR = BASE_DIR / "data" / "gov_docs"
WEB_INDEX_DIR = str(BASE_DIR / "web_index")
WEB_INDEX_META_FILE = os.path.join(WEB_INDEX_DIR, "index.meta.json")

# 相关性门限：FAISS 返回的 L2 距离超过该值即视为「没有可用依据」。
# 数值由 bge-m3 在本语料上实测校准（见 app/README.md「拒答阈值」一节），
# 可用环境变量 RAG_MAX_DISTANCE 覆盖，换 embedding 模型后必须重新校准。
DEFAULT_MAX_DISTANCE = 0.78

# ---------- 检索后端 ----------

RETRIEVAL_BACKENDS = ("faiss", "pg")
# 未显式配置后端、也没有数据库时的兜底：教学主线与既有测试仍开箱即用。
# 配置了 INGEST_DB_URL 就默认走 pg（见 resolve_backend），.env.example 里再显式写死 pg。
DEFAULT_BACKEND = "faiss"

_CLASSIFICATION_NAMES = {level: name for name, level in CLASSIFICATION_LEVELS.items()}


def resolve_backend(explicit: str | None = None) -> str:
    """
    解析检索后端：构造参数 > RETRIEVAL_BACKEND > （配置了 INGEST_DB_URL 则 pg，否则 faiss）。

    非法值直接抛错，而不是悄悄回退到 FAISS——部署时后端名写错，
    应该启动就失败，不能等用户提问后才发现查的是另一个库。
    """
    raw = explicit if explicit is not None else os.getenv("RETRIEVAL_BACKEND", "")
    value = (raw or "").strip().lower()
    if not value:
        value = "pg" if os.getenv("INGEST_DB_URL", "").strip() else DEFAULT_BACKEND
    if value not in RETRIEVAL_BACKENDS:
        raise ValueError(
            f"RETRIEVAL_BACKEND={value!r} 非法，可选：{'/'.join(RETRIEVAL_BACKENDS)}")
    return value


def _default_conn_factory():
    from ingest.db import connection

    return connection()


# 引用片段预览长度
PREVIEW_CHARS = 120


logger = logging.getLogger("rag.service")


def max_distance() -> float:
    """
    实时读取阈值，便于测试与部署时调整（不重启进程也能改）。

    配置写错（非数字/非正数）时回退默认值并告警，而不是让每个请求都在
    `float()` 上抛 ValueError 变成 500。
    """
    raw = os.getenv("RAG_MAX_DISTANCE")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_DISTANCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn_bad_distance(raw)
        return DEFAULT_MAX_DISTANCE
    if value <= 0:
        _warn_bad_distance(raw)
        return DEFAULT_MAX_DISTANCE
    return value


_warned_distance_values: set[str] = set()


def _warn_bad_distance(raw: str) -> None:
    """同一错误值只告警一次，避免每个请求刷屏。"""
    if raw in _warned_distance_values:
        return
    _warned_distance_values.add(raw)
    logger.warning(
        "RAG_MAX_DISTANCE=%r 非法（需为正数），已回退默认值 %.2f", raw, DEFAULT_MAX_DISTANCE
    )


# ---------- 语料加载 ----------

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_REQUIRED_FIELDS = ("doc_id", "title", "dept", "classification")


@dataclass
class GovDoc:
    """一份带密级的原始文档（尚未切片）。"""

    metadata: dict
    body: str
    path: Path


@dataclass
class DocIssue:
    """一份加载失败、被跳过的文档。"""

    path: Path
    reason: str

    def to_dict(self) -> dict:
        return {"file": self.path.name, "reason": self.reason}


def parse_front_matter(text: str) -> tuple[dict, str]:
    """
    拆分 YAML front matter 与正文。

    语料用 Markdown + front matter 存放元数据：
        ---
        doc_id: gov-003
        classification: 秘密
        ---
    正文……
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError("文档缺少 YAML front matter（文件应以 --- 开头）")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("front matter 必须是键值对")
    return meta, text[match.end():]


def load_gov_docs(
    docs_dir: Path = GOV_DOCS_DIR,
    *,
    strict: bool = True,
    issues: list[DocIssue] | None = None,
) -> list[GovDoc]:
    """
    读取 data/gov_docs/*.md，校验元数据并归一化密级。

    strict=True（默认）遇到坏文档立即抛错，保持调用方（测试/工具）的严格语义；
    strict=False 时跳过坏文档并记入 `issues`，用于运行时——单个文件格式错误
    不应让整个知识库 503。全部文档都坏时仍然抛错（没有可用语料不等于可用）。
    """
    docs: list[GovDoc] = []
    paths = sorted(Path(docs_dir).glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"未在 {docs_dir} 找到任何 Markdown 文档")

    for path in paths:
        try:
            meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
            missing = [k for k in _REQUIRED_FIELDS if not meta.get(k)]
            if missing:
                raise ValueError(f"{path.name} 缺少必填元数据: {', '.join(missing)}")
            level = CLASSIFICATION_LEVELS.get(meta["classification"])
            if level is None:
                raise ValueError(
                    f"{path.name} 的 classification='{meta['classification']}' 非法，"
                    f"可选：{'/'.join(CLASSIFICATION_LEVELS)}"
                )
        except (ValueError, yaml.YAMLError, OSError) as exc:
            if strict:
                raise
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("跳过无法加载的文档 %s（%s）", path.name, reason)
            if issues is not None:
                issues.append(DocIssue(path=path, reason=reason))
            continue

        docs.append(GovDoc(metadata={**meta, "classification_level": level}, body=body, path=path))

    if not docs:
        detail = "；".join(f"{i.path.name}: {i.reason}" for i in (issues or []))
        raise RuntimeError(f"{docs_dir} 下没有任何可加载的文档。{detail}")

    return docs


def load_gov_documents(
    docs_dir: Path = GOV_DOCS_DIR,
    *,
    strict: bool = True,
    issues: list[DocIssue] | None = None,
) -> list[LCDocument]:
    """把带密级文档切成检索切片，每个切片携带完整的密级/部门元数据。"""
    splitter = RecursiveCharacterTextSplitter(
        separators=core.CHINESE_SEPARATORS,
        chunk_size=core.CHUNK_SIZE,
        chunk_overlap=core.CHUNK_OVERLAP,
    )
    documents: list[LCDocument] = []
    for gov in load_gov_docs(docs_dir, strict=strict, issues=issues):
        meta = gov.metadata
        chunks = [c for c in splitter.split_text(gov.body) if c.strip()]
        for idx, chunk in enumerate(chunks):
            documents.append(
                LCDocument(
                    page_content=chunk,
                    metadata={
                        "chunk_id": f"{meta['doc_id']}#{idx}",
                        "doc_id": meta["doc_id"],
                        "title": meta["title"],
                        "dept": meta["dept"],
                        "classification": meta["classification"],
                        "classification_level": meta["classification_level"],
                        "effective_date": str(meta.get("effective_date", "")),
                        "source": gov.path.name,
                    },
                )
            )
    return documents


# ---------- 索引指纹与构建 ----------

def corpus_signature(docs_dir: Path = GOV_DOCS_DIR) -> dict:
    """
    语料指纹：所有文件内容 hash + 切分配置 + embedding 模型。

    任一变化都会触发 FAISS 重建，避免「密级或正文改了、索引还是旧的」。
    """
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(docs_dir).glob("*.md"))
    }
    return {
        "files": files,
        "chunk_config": {
            "chunk_size": core.CHUNK_SIZE,
            "chunk_overlap": core.CHUNK_OVERLAP,
        },
        "embed_model": core.EMBED_MODEL,
    }


def _load_meta(meta_file: str = WEB_INDEX_META_FILE) -> dict | None:
    if not os.path.exists(meta_file):
        return None
    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_meta(sig: dict, index_dir: str = WEB_INDEX_DIR, meta_file: str = WEB_INDEX_META_FILE) -> None:
    os.makedirs(index_dir, exist_ok=True)
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(sig, f, ensure_ascii=False, indent=2)


def build_or_load_index(
    documents: List[LCDocument],
    embed_model,
    index_dir: str = WEB_INDEX_DIR,
    meta_file: str = WEB_INDEX_META_FILE,
) -> FAISS:
    """指纹一致则加载已有索引，否则重建并持久化（与 main.build_index 同思路）。"""
    sig = corpus_signature()
    faiss_ok = os.path.exists(os.path.join(index_dir, "index.faiss"))

    if faiss_ok and _load_meta(meta_file) == sig:
        print("📦 检测到有效政务索引（指纹匹配），加载中...")
        return FAISS.load_local(index_dir, embed_model, allow_dangerous_deserialization=True)

    if faiss_ok:
        print("♻️ 政务语料或切分配置已变更，重建索引...")
    else:
        print("🔍 构建政务知识库索引...")
    vectorstore = FAISS.from_documents(documents=documents, embedding=embed_model)
    vectorstore.save_local(index_dir)
    _save_meta(sig, index_dir, meta_file)
    print(f"   政务索引已保存（{len(documents)} 个切片）")
    return vectorstore


# ---------- 检索结果 ----------

@dataclass
class RetrievalResult:
    """一次检索的完整现场，供生成、引用和审计使用。"""

    query: str
    docs: list[LCDocument] = field(default_factory=list)
    best_distance: float | None = None
    dense_hits: int = 0
    sparse_hits: int = 0
    visible_docs: int = 0
    visible_chunks: int = 0
    grounded: bool = False
    reason: str = ""

    def citations(self) -> list[dict]:
        """把命中切片转成前端/审计用的引用结构。"""
        items = []
        for rank, doc in enumerate(self.docs, start=1):
            md = doc.metadata
            items.append({
                "rank": rank,
                "chunk_id": md["chunk_id"],
                "doc_id": md["doc_id"],
                "title": md["title"],
                "dept": md["dept"],
                "classification": md["classification"],
                "effective_date": md.get("effective_date", ""),
                "source": md.get("source", ""),
                "preview": doc.page_content[:PREVIEW_CHARS].replace("\n", " "),
            })
        return items


# ---------- 服务 ----------

class RagService:
    """
    权限感知的 RAG 服务。

    一次问答的链路：
        提问 → （多轮）追问改写 → 权限裁剪 → 混合检索 → 相关性判定
             → 有依据：流式生成 + 引用
             → 无依据：拒答（不调用 LLM）
    """

    def __init__(self, llm=None, embed_model=None,
                 history_turns: int = core.MAX_HISTORY_TURNS, *,
                 backend: str | None = None, conn_factory=None,
                 embedder=None):
        self.backend = resolve_backend(backend)
        self.llm = llm or core.init_llm()
        self.history_turns = history_turns
        self._history: dict[str, list] = {}
        self.load_issues: list[DocIssue] = []

        if self.backend == "pg":
            # pg 后端不需要语料目录、FAISS 与 LangChain embedding：
            # 查询向量由入库同一模型（INGEST_EMBED_MODEL）生成，见 _require_embedder()。
            self.embed_model = None
            self.embedder = embedder
            self.documents = []
            self.vectorstore = None
            self._conn_factory = conn_factory or _default_conn_factory
            # 启动即探库：未迁移/连不上要让 get_service() 变成 503，
            # 而不是等第一个提问在 retrieve 里抛 UndefinedTable。
            with self._conn_factory() as conn:
                conn.execute("SELECT 1 FROM documents LIMIT 1")
            return

        self.embedder = None
        self._conn_factory = None
        self.embed_model = embed_model or core.init_embedding()
        # 运行时用宽松模式：单个坏文件只跳过并留痕，不让整库不可用
        self.documents = load_gov_documents(issues=self.load_issues)
        if self.load_issues:
            logger.warning(
                "语料加载跳过 %d 个文件（以部分文档启动）：%s",
                len(self.load_issues),
                "；".join(f"{i.path.name}（{i.reason}）" for i in self.load_issues),
            )
        self.vectorstore = build_or_load_index(self.documents, self.embed_model)

    # ---- 权限 ----

    def visible_documents(self, user: User) -> list[LCDocument]:
        """
        用户可见的切片集合——FAISS 检索的唯一候选来源。

        pg 后端没有「内存切片集合」这个概念（切片可能上百万），
        显式报错，避免调用方把空列表误判成「无可见文档」而错误拒答。
        """
        if self.backend == "pg":
            raise RuntimeError(
                "pg 后端不提供 visible_documents()，请使用 visible_docs() "
                "或 ingest.retrieval.retrieve()")
        return [doc for doc in self.documents if can_view(user, doc.metadata)]

    def visible_docs(self, user: User) -> list[dict]:
        """可见文档的去重清单（前端展示「您能看到哪些文件」）。"""
        if self.backend == "pg":
            return self._visible_docs_pg(user)
        seen: dict[str, dict] = {}
        for doc in self.visible_documents(user):
            md = doc.metadata
            seen.setdefault(md["doc_id"], {
                "doc_id": md["doc_id"],
                "title": md["title"],
                "dept": md["dept"],
                "classification": md["classification"],
                "effective_date": md.get("effective_date", ""),
            })
        return list(seen.values())

    def _visible_docs_pg(self, user: User) -> list[dict]:
        with self._conn_factory() as conn:
            rows = conn.execute(
                """SELECT d.id, d.title, d.dept, d.classification, d.effective_date
                     FROM documents d
                    WHERE d.status = 'active' AND d.deleted_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM chunks c
                           WHERE c.document_id = d.id AND c.active
                             AND c.classification IS NOT NULL
                             AND c.classification <= %s
                             AND (c.classification = 0 OR c.dept = %s))
                    ORDER BY d.title""",
                (user.max_level, user.dept),
            ).fetchall()
        return [{
            "doc_id": str(row["id"]),
            "title": row["title"],
            "dept": row["dept"],
            "classification": _CLASSIFICATION_NAMES.get(row["classification"], "未知"),
            "effective_date": str(row["effective_date"] or ""),
        } for row in rows]

    def chunk_detail(self, user: User, chunk_id: str) -> dict | None:
        """
        按 chunk_id 取切片原文，供前端「查看原文」使用。

        权限判定复用检索侧的 can_view（同一套密级/部门规则），且判定在前：
        不可见切片一律返回 None，不区分「不存在」与「无权查看」，
        避免接口成为枚举密级归属的旁路。
        """
        for doc in self.documents:
            md = doc.metadata
            if md.get("chunk_id") != chunk_id:
                continue
            if not can_view(user, md):
                return None
            return {
                "chunk_id": chunk_id,
                "doc_id": md.get("doc_id", ""),
                "title": md.get("title", ""),
                "dept": md.get("dept", ""),
                "classification": md.get("classification", ""),
                "effective_date": md.get("effective_date", ""),
                "source": md.get("source", ""),
                "text": doc.page_content,
            }
        return None

    # ---- 检索 ----

    def retrieve(self, user: User, query: str) -> RetrievalResult:
        if self.backend == "pg":
            return self._retrieve_pg(user, query)
        return self._retrieve_faiss(user, query)

    def _retrieve_pg(self, user: User, query: str) -> RetrievalResult:
        from ingest import retrieval

        with self._conn_factory() as conn:
            outcome = retrieval.retrieve(
                conn, user, query, embedder=self._require_embedder(),
                max_distance=max_distance(),
            )
        result = RetrievalResult(query=query)
        result.visible_docs = outcome.visible_docs
        result.visible_chunks = outcome.visible_chunks
        result.best_distance = outcome.best_distance
        result.dense_hits = outcome.dense_hits
        result.sparse_hits = outcome.sparse_hits
        result.grounded = outcome.grounded
        result.reason = outcome.reason
        result.docs = [self._to_document(hit) for hit in outcome.hits]
        return result

    def _require_embedder(self):
        if self.embedder is None:
            from ingest.embedding import get_embedder

            self.embedder = get_embedder()
        return self.embedder

    @staticmethod
    def _to_document(hit) -> LCDocument:
        """把 pg 命中还原成原有切片契约，生成/引用/审计代码无需感知后端。"""
        return LCDocument(
            page_content=hit.content,
            metadata={
                "chunk_id": hit.chunk_id,
                "doc_id": hit.doc_id,
                "title": hit.title,
                "dept": hit.dept,
                "classification": _CLASSIFICATION_NAMES.get(hit.classification, "未知"),
                "classification_level": hit.classification,
                "effective_date": hit.effective_date,
                "source": hit.metadata.get("source", ""),
                "chunk_type": hit.chunk_type,
                "expanded": hit.expanded,
            },
        )

    def _retrieve_faiss(self, user: User, query: str) -> RetrievalResult:
        """FAISS + 内存 BM25（现有教学主线，行为不变）。"""
        visible = self.visible_documents(user)
        result = RetrievalResult(query=query, visible_chunks=len(visible))
        result.visible_docs = len(self.visible_docs(user))

        if not visible:
            result.reason = "no_visible_docs"
            return result

        dense = self.vectorstore.similarity_search_with_score(
            query,
            k=core.RETRIEVE_K,
            filter=lambda md: can_view(user, md),
            fetch_k=max(20, len(self.documents)),
        )
        dense_results = [{"doc": doc, "score": float(score)} for doc, score in dense]
        result.dense_hits = len(dense_results)
        result.best_distance = float(dense[0][1]) if dense else None

        tokenized = [core.tokenize(doc.page_content) for doc in visible]
        bm25 = core.BM25Okapi(tokenized) if core.BM25_AVAILABLE else None
        sparse_results = core.sparse_search(
            bm25, [doc.page_content for doc in visible], query, visible,
            k=core.RETRIEVE_K,
        )
        result.sparse_hits = len(sparse_results)

        if sparse_results:
            fused = core.reciprocal_rank_fusion(
                [dense_results, sparse_results], k=core.RRF_K
            )
            result.docs = [item["doc"] for item in fused[: core.FUSION_K]]
        else:
            result.docs = [item["doc"] for item in dense_results[: core.FUSION_K]]

        if result.best_distance is None:
            result.reason = "no_dense_hits"
        elif result.best_distance > max_distance():
            result.reason = "low_relevance"
        else:
            result.grounded = True
        return result

    # ---- 生成 ----

    def history(self, username: str) -> list:
        return list(self._history.get(username, []))

    def clear_history(self, username: str) -> None:
        self._history.pop(username, None)

    def restore_history(self, username: str, turns: list[tuple[str, str]]) -> None:
        """
        回填历史轮次（前端本地历史恢复后调用）。

        turns 为 [(question, answer), ...]，按时间顺序补齐。
        补写后仍受 history_turns 预算裁剪，不会把上下文撑爆。
        """
        for question, answer in turns:
            if not question or not answer:
                continue
            self._remember(username, question, answer)

    def _remember(self, username: str, question: str, answer: str) -> None:
        turns = self._history.setdefault(username, [])
        turns.extend([HumanMessage(content=question), AIMessage(content=answer)])
        del turns[: max(0, len(turns) - self.history_turns * 2)]

    def _llm_text(self, messages) -> Iterator[str]:
        for chunk in self.llm.stream(messages):
            yield chunk.content or ""

    def answer_stream(self, user: User, question: str, history=None) -> Iterator[dict]:
        """
        流式问答，产出结构化事件（由接入层转成 SSE）：

        rewrite / citations / token / done / refusal
        """
        started = time.perf_counter()
        # 历史先按字符预算裁剪，避免长答案把上下文撑爆后被模型静默截断
        history = core.trim_history(history or [])

        # 1) 多轮改写：把「它有几类？」还原成可独立检索的问题
        search_query = core.condense_question(self.llm, history, question)
        if search_query != question:
            yield {"type": "rewrite", "from": question, "to": search_query}

        # 2) 权限前置检索 + 相关性判定
        retrieval = self.retrieve(user, search_query)
        if not retrieval.grounded:
            yield {
                "type": "refusal",
                "reason": retrieval.reason,
                "message": self._refusal_message(retrieval),
                "visible_docs": retrieval.visible_docs,
                "visible_chunks": retrieval.visible_chunks,
                "best_distance": retrieval.best_distance,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
            return

        citations = retrieval.citations()
        yield {"type": "citations", "items": citations}

        # 3) 拼 Prompt 并流式生成（复用 <think> 过滤）
        # 上下文带 [n] 编号且受字符预算约束，编号与 citations 的 rank 一一对应
        context = core.format_context(retrieval.docs)
        messages = core.RAG_PROMPT.invoke(
            {"context": context, "question": question, "history": history}
        )
        parts: list[str] = []
        for piece in core.strip_think_stream(self._llm_text(messages)):
            parts.append(piece)
            yield {"type": "token", "text": piece}

        answer = core.clean_response("".join(parts))
        if answer:
            self._remember(user.username, question, answer)
        yield {
            "type": "done",
            "answer": answer,
            "citations": citations,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    @staticmethod
    def _refusal_message(retrieval: RetrievalResult) -> str:
        if retrieval.reason == "no_visible_docs":
            return "您当前没有任何可访问的文档，无法回答该问题。"
        if retrieval.reason == "no_dense_hits":
            return "在您可见的文档中没有检索到相关内容，无法回答该问题。"
        return (
            "在您可访问的文档中没有找到足够相关的依据，无法回答该问题。"
            "（可能是权限范围不包含相关资料，或问题超出了知识库覆盖范围）"
        )

    # ---- 状态 ----

    def stats(self) -> dict:
        if self.backend == "pg":
            return self._stats_pg()
        return self._stats_faiss()

    def _stats_pg(self) -> dict:
        from ingest.config import get_settings

        with self._conn_factory() as conn:
            counts = conn.execute(
                """SELECT count(*) AS chunks,
                          count(DISTINCT c.document_id) AS docs
                     FROM chunks c
                     JOIN documents d ON d.id = c.document_id
                    WHERE c.active AND d.status = 'active'
                      AND d.deleted_at IS NULL""",
            ).fetchone()
            by_class = conn.execute(
                """SELECT d.classification AS level, count(*) AS n
                     FROM chunks c
                     JOIN documents d ON d.id = c.document_id
                    WHERE c.active AND d.status = 'active'
                      AND d.deleted_at IS NULL
                    GROUP BY d.classification""",
            ).fetchall()
        return {
            "backend": "pg",
            "docs": int(counts["docs"]),
            "chunks": int(counts["chunks"]),
            "chunks_by_classification": {
                _CLASSIFICATION_NAMES.get(int(row["level"]), "未知"): int(row["n"])
                for row in by_class
                if row["level"] is not None
            },
            "load_issues": [],
            "embed_model": get_settings().embed_model,
            "llm_provider": core.LLM_PROVIDER,
            "llm_model": (
                core.DEEPSEEK_MODEL
                if core.LLM_PROVIDER == "deepseek"
                else core.OLLAMA_LLM_MODEL
            ),
            "max_distance": max_distance(),
        }

    def _stats_faiss(self) -> dict:
        by_class: dict[str, int] = {}
        for doc in self.documents:
            name = doc.metadata["classification"]
            by_class[name] = by_class.get(name, 0) + 1
        return {
            "backend": "faiss",
            "docs": len({doc.metadata["doc_id"] for doc in self.documents}),
            "chunks": len(self.documents),
            "chunks_by_classification": by_class,
            "load_issues": [issue.to_dict() for issue in self.load_issues],
            "embed_model": core.EMBED_MODEL,
            "llm_provider": core.LLM_PROVIDER,
            "llm_model": (
                core.DEEPSEEK_MODEL
                if core.LLM_PROVIDER == "deepseek"
                else core.OLLAMA_LLM_MODEL
            ),
            "max_distance": max_distance(),
        }
