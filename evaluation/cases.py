"""
评测用例的格式定义、加载与校验。

用例是一份 JSONL，每行一条：

    {"id": "gov-num-01", "user": "wang", "question": "差旅住宿费标准是多少？",
     "expected_doc_ids": ["gov-003"], "evidence": ["一类城市每人每天 900 元"],
     "tags": ["numeric"]}

字段约定：

- `expected_doc_ids` + `evidence`：**应答类**用例的金标。金标绑「文档 + 证据原文」，
  不绑 chunk_id——切分方案换了、存储迁到 pgvector，题库都不用重标。
- `should_refuse` + `forbidden_doc_ids`：**拒答类**用例。`forbidden_doc_ids` 是
  「绝不能出现在结果里的文档」，用于越权泄露检查。
- `history` / `search_query`：多轮用例。answer 模式走服务自己的追问改写；
  retrieval-only 模式不调 LLM，改用 `search_query`（缺省则用 `question`）。

校验原则是**尽早失败**：字段拼错、金标文档不存在、证据在语料里找不到，
都在跑评测之前就报错。否则一条写错的用例只会安静地拉低指标，很难定位。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from evaluation.metrics import normalize

# 用例可用字段；出现集合外的字段一律报错，避免 `evidences` 这类拼写错误被静默忽略
ALLOWED_FIELDS = {
    "id",
    "user",
    "question",
    "expected_doc_ids",
    "evidence",
    "forbidden_doc_ids",
    "should_refuse",
    "tags",
    "history",
    "search_query",
}

# 六类覆盖标签（种子集与报告按它分组看短板）
KNOWN_TAGS = {
    "single_fact",   # 单文档事实
    "cross_doc",     # 跨文档对比
    "numeric",       # 条款数值
    "permission",    # 权限拒答
    "out_of_scope",  # 超纲拒答
    "multi_turn",    # 多轮指代
}

_HISTORY_ROLES = {"user", "assistant"}


@dataclass
class EvalCase:
    """一条评测用例。"""

    id: str
    user: str
    question: str
    expected_doc_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    forbidden_doc_ids: list[str] = field(default_factory=list)
    should_refuse: bool = False
    tags: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    search_query: str = ""

    @property
    def retrieval_query(self) -> str:
        """retrieval-only 模式实际送进检索的查询。"""
        return self.search_query or self.question


# ---------- 加载 ----------

def load_cases(path: str | Path) -> list[EvalCase]:
    """读取 JSONL 用例文件；空行与 `#` 注释行会被跳过。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"评测用例文件不存在: {path}")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} 第 {lineno} 行不是合法 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name} 第 {lineno} 行必须是 JSON 对象")

        case = _parse_case(raw, lineno)
        if case.id in seen:
            raise ValueError(f"{path.name} 第 {lineno} 行：用例 id 重复（{case.id}）")
        seen.add(case.id)
        cases.append(case)

    if not cases:
        raise ValueError(f"{path.name} 里没有任何用例")
    return cases


def _require_str_list(raw: dict, key: str, lineno: int, *, required: bool) -> list[str]:
    value = raw.get(key, [])
    if value in (None, ""):
        value = []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"第 {lineno} 行：{key} 必须是非空字符串数组，实际为 {value!r}")
    if required and not value:
        raise ValueError(f"第 {lineno} 行：{key} 不能为空")
    return [v.strip() for v in value]


def _parse_case(raw: dict, lineno: int) -> EvalCase:
    unknown = set(raw) - ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            f"第 {lineno} 行：未知字段 {', '.join(sorted(unknown))}"
            f"（可用字段：{', '.join(sorted(ALLOWED_FIELDS))}）"
        )

    case_id = str(raw.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"第 {lineno} 行：id 不能为空")

    user = str(raw.get("user") or "").strip()
    if not user:
        raise ValueError(f"第 {lineno} 行（{case_id}）：user 不能为空")

    question = str(raw.get("question") or "").strip()
    if not question:
        raise ValueError(f"第 {lineno} 行（{case_id}）：question 不能为空")

    should_refuse = raw.get("should_refuse", False)
    if not isinstance(should_refuse, bool):
        raise ValueError(f"第 {lineno} 行（{case_id}）：should_refuse 必须是布尔值")

    tags = raw.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise ValueError(f"第 {lineno} 行（{case_id}）：tags 必须是字符串数组")
    tags = [t.strip() for t in tags]

    history = raw.get("history", []) or []
    if not isinstance(history, list):
        raise ValueError(f"第 {lineno} 行（{case_id}）：history 必须是数组")
    for i, turn in enumerate(history):
        if not isinstance(turn, dict):
            raise ValueError(f"第 {lineno} 行（{case_id}）：history[{i}] 必须是对象")
        if turn.get("role") not in _HISTORY_ROLES:
            raise ValueError(
                f"第 {lineno} 行（{case_id}）：history[{i}].role 只能是 user/assistant，"
                f"实际为 {turn.get('role')!r}"
            )
        if not str(turn.get("content") or "").strip():
            raise ValueError(f"第 {lineno} 行（{case_id}）：history[{i}].content 不能为空")

    search_query = str(raw.get("search_query") or "").strip()

    if should_refuse:
        # 拒答用例的金标是「不该出现什么」，再给 expected/evidence 会让命中与泄露互相矛盾
        if raw.get("expected_doc_ids") or raw.get("evidence"):
            raise ValueError(
                f"第 {lineno} 行（{case_id}）：should_refuse=true 的用例不应再给 "
                f"expected_doc_ids / evidence，请改用 forbidden_doc_ids 标注不可见文档"
            )
        return EvalCase(
            id=case_id, user=user, question=question, should_refuse=True,
            forbidden_doc_ids=_require_str_list(raw, "forbidden_doc_ids", lineno, required=False),
            tags=tags, history=history, search_query=search_query,
        )

    return EvalCase(
        id=case_id,
        user=user,
        question=question,
        expected_doc_ids=_require_str_list(raw, "expected_doc_ids", lineno, required=True),
        evidence=_require_str_list(raw, "evidence", lineno, required=True),
        forbidden_doc_ids=_require_str_list(raw, "forbidden_doc_ids", lineno, required=False),
        should_refuse=False,
        tags=tags,
        history=history,
        search_query=search_query,
    )


# ---------- 金标与语料一致性 ----------

def validate_against_corpus(
    cases: Sequence[EvalCase],
    documents: Sequence,
    known_users: Sequence[str] | None = None,
) -> list[str]:
    """
    检查用例金标是否真的对得上语料，返回问题清单（空列表表示全部通过）。

    三类问题最容易埋雷：
    1. 用户不在演示用户目录里 → 用例永远拿不到可见文档
    2. 金标 doc_id 不存在 → Recall 恒为 0
    3. 证据原文写错一个字，或落在别的文档里 → 证据命中率恒为 0
    """
    if known_users is None:
        from app.users import DEMO_USERS  # 延迟导入：评测台也可用于其他用户目录

        known_users = list(DEMO_USERS)
    known_users = set(known_users)

    doc_texts: dict[str, list[str]] = {}
    for doc in documents:
        doc_id = doc.metadata.get("doc_id")
        if doc_id:
            doc_texts.setdefault(doc_id, []).append(normalize(doc.page_content))
    known_docs = set(doc_texts)

    problems: list[str] = []
    for case in cases:
        if case.user not in known_users:
            problems.append(
                f"[{case.id}] 用户 '{case.user}' 不在用户目录中（可选：{', '.join(sorted(known_users))}）"
            )

        for doc_id in case.expected_doc_ids:
            if doc_id not in known_docs:
                problems.append(f"[{case.id}] 金标文档 '{doc_id}' 不在语料中")
        for doc_id in case.forbidden_doc_ids:
            if doc_id not in known_docs:
                problems.append(f"[{case.id}] forbidden 文档 '{doc_id}' 不在语料中")

        for span in case.evidence:
            # 只按「单个切片」匹配：拼接后再找会跨切片边界产生假命中。
            # 证据片段跨了切片边界时会判为找不到——这正是要把片段写短一点的原因。
            haystacks = [
                text
                for doc_id in case.expected_doc_ids
                for text in doc_texts.get(doc_id, [])
            ]
            if not any(normalize(span) in text for text in haystacks):
                problems.append(
                    f"[{case.id}] 证据「{span}」未出现在金标文档 "
                    f"{case.expected_doc_ids or '（未指定）'} 的任何切片中"
                )

    return problems
