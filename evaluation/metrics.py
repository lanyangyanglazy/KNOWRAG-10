"""
RAG 效果评测指标（纯函数，不依赖模型与网络）。

设计取向：

1. **金标绑「文档 + 证据原文」，不绑 chunk_id**。
   切分方案以后要换成结构化切分、存储要迁 pgvector，`doc_id#idx` 必然全变；
   绑 chunk_id 的题库每改一次切分就要重标一遍。这里用「命中切片是否包含证据文本」
   判命中，切分怎么变，题库都不用动（见 `evidence_coverage`）。

2. **只算确定性指标**。忠实度/答案相关性这类需要 LLM 判官的指标先留接口，
   等真实语料和真实问题到位再校准判官，现在接进来只会得到噪声。

3. **该答却拒答，按 0 分计入分母**，不从统计里剔除——否则误拒越多、指标越好看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# 参与基线对比的「越大越好」指标
HIGHER_IS_BETTER = (
    "evidence_recall",
    "doc_recall",
    "mrr",
    "refusal_accuracy",
    "citation_recall",
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """去掉所有空白字符。中文语料里换行/空格是排版产物，不该影响金标匹配。"""
    return _WHITESPACE_RE.sub("", text or "")


# ---------- 单条用例的结果 ----------

@dataclass
class CaseOutcome:
    """一条评测用例跑完后的现场，供汇总与报告使用。"""

    id: str
    question: str
    tags: list[str] = field(default_factory=list)

    # 拒答判定
    expected_refuse: bool = False
    refused: bool = False
    refusal_reason: str = ""

    # 检索质量（None 表示「本条不适用」，例如拒答类用例没有金标证据）
    retrieved_doc_ids: list[str] = field(default_factory=list)
    doc_recall: float | None = None
    mrr: float | None = None
    evidence: float | None = None
    citation_recall: float | None = None

    # 安全
    leaked_doc_ids: list[str] = field(default_factory=list)

    # 性能与答案
    elapsed_ms: float = 0.0
    answer: str = ""


# ---------- 证据命中 ----------

def evidence_coverage(retrieved_texts: Sequence[str], evidence: Sequence[str]) -> float | None:
    """
    金标证据被命中的比例（0.0–1.0）。

    - `evidence` 为空 → 返回 None，表示该用例不适用此指标（如拒答类用例）
    - 每条证据只要在**任一切片**里出现即算命中；按切片逐条比对，
      不拼接后再找，避免跨切片拼出并不存在的「命中」
    """
    if not evidence:
        return None
    if not retrieved_texts:
        return 0.0

    haystacks = [normalize(text) for text in retrieved_texts]
    hits = sum(1 for span in evidence if any(normalize(span) in h for h in haystacks))
    return hits / len(evidence)


# ---------- 召回与排序 ----------

def recall_at_k(retrieved_doc_ids: Sequence[str], expected_doc_ids: Sequence[str], k: int = 5) -> float | None:
    """金标文档出现在 Top-K 里的比例；没有金标文档时返回 None（不适用）。"""
    if not expected_doc_ids:
        return None
    top = set(retrieved_doc_ids[:k])
    hits = sum(1 for doc_id in expected_doc_ids if doc_id in top)
    return hits / len(expected_doc_ids)


def mrr_at_k(retrieved_doc_ids: Sequence[str], expected_doc_ids: Sequence[str], k: int = 5) -> float:
    """第一个金标文档的倒数排名；Top-K 内没有金标文档则为 0。"""
    expected = set(expected_doc_ids)
    for rank, doc_id in enumerate(retrieved_doc_ids[:k], start=1):
        if doc_id in expected:
            return 1.0 / rank
    return 0.0


# ---------- 越权泄露 ----------

def find_leaks(retrieved_doc_ids: Sequence[str], forbidden_doc_ids: Sequence[str]) -> list[str]:
    """结果集里出现的「不该出现的文档」，按首次出现顺序去重返回。"""
    forbidden = set(forbidden_doc_ids)
    seen: list[str] = []
    for doc_id in retrieved_doc_ids:
        if doc_id in forbidden and doc_id not in seen:
            seen.append(doc_id)
    return seen


# ---------- 时延 ----------

def percentile(values: Sequence[float], q: float) -> float:
    """线性插值百分位（与 numpy 默认一致）；空序列返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1 - frac) + ordered[high] * frac)


# ---------- 汇总 ----------

def _mean(values: Iterable[float | None]) -> float | None:
    usable = [v for v in values if v is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def summarize(results: Sequence[CaseOutcome]) -> dict:
    """把逐条结果汇总成一份指标字典（报告与基线对比都用它）。"""
    answerable = [r for r in results if not r.expected_refuse]
    refusals = [r for r in results if r.expected_refuse]

    correct = sum(1 for r in results if r.refused == r.expected_refuse)
    false_refusals = sum(1 for r in answerable if r.refused)
    missed_refusals = sum(1 for r in refusals if not r.refused)

    latencies = [r.elapsed_ms for r in results]

    return {
        "cases": len(results),
        "answerable": len(answerable),
        "refusal_expected": len(refusals),
        # 检索质量：只对「金标为应答」的用例统计，误拒按 0 分计入
        "evidence_recall": _mean(r.evidence for r in answerable) or 0.0,
        "doc_recall": _mean(r.doc_recall for r in answerable) or 0.0,
        "mrr": _mean(r.mrr for r in answerable) or 0.0,
        # 引用质量：只有 answer 模式才有数据
        "citation_recall": _mean(r.citation_recall for r in answerable),
        # 拒答判定
        "refusal_accuracy": correct / len(results) if results else 0.0,
        "false_refusal_rate": false_refusals / len(answerable) if answerable else 0.0,
        "missed_refusal_rate": missed_refusals / len(refusals) if refusals else 0.0,
        # 安全：任何越权泄露都是硬失败
        "leaks": sum(len(r.leaked_doc_ids) for r in results),
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "mean": sum(latencies) / len(latencies) if latencies else 0.0,
        },
        "by_tag": _by_tag(results),
    }


def _by_tag(results: Sequence[CaseOutcome]) -> dict:
    """按标签拆分指标，用来定位短板是「没召回」还是「拒答误杀」。"""
    tags: dict[str, list[CaseOutcome]] = {}
    for r in results:
        for tag in r.tags or ["untagged"]:
            tags.setdefault(tag, []).append(r)

    breakdown = {}
    for tag, items in sorted(tags.items()):
        answerable = [r for r in items if not r.expected_refuse]
        correct = sum(1 for r in items if r.refused == r.expected_refuse)
        breakdown[tag] = {
            "cases": len(items),
            "evidence_recall": _mean(r.evidence for r in answerable) or 0.0,
            "doc_recall": _mean(r.doc_recall for r in answerable) or 0.0,
            "mrr": _mean(r.mrr for r in answerable) or 0.0,
            "refusal_accuracy": correct / len(items) if items else 0.0,
        }
    return breakdown


# ---------- 基线对比 ----------

def compare_reports(current: dict, baseline: dict, tolerance: float = 0.03) -> dict:
    """
    与基线对比，给出回归/改进清单。

    - 越大越好的指标：跌幅超过 `tolerance` 记为回归
    - `leaks`：只要增加就是回归（安全项不给容差）
    - `cases`：评测集缩水也算回归，避免「少跑几条让指标变好看」
    - 基线里没有的指标（新增能力）跳过，不误判
    """
    cur = current.get("metrics", current)
    base = baseline.get("metrics", baseline)

    regressions: list[dict] = []
    improvements: list[dict] = []

    for metric in HIGHER_IS_BETTER:
        cur_value, base_value = cur.get(metric), base.get(metric)
        if cur_value is None or base_value is None:
            continue
        delta = cur_value - base_value
        entry = {"metric": metric, "baseline": base_value, "current": cur_value, "delta": delta}
        if delta < -tolerance:
            regressions.append(entry)
        elif delta > tolerance:
            improvements.append(entry)

    if cur.get("leaks", 0) > base.get("leaks", 0):
        regressions.append({
            "metric": "leaks",
            "baseline": base.get("leaks", 0),
            "current": cur.get("leaks", 0),
            "delta": cur.get("leaks", 0) - base.get("leaks", 0),
        })

    if cur.get("cases", 0) < base.get("cases", 0):
        regressions.append({
            "metric": "cases",
            "baseline": base.get("cases", 0),
            "current": cur.get("cases", 0),
            "delta": cur.get("cases", 0) - base.get("cases", 0),
        })

    return {"ok": not regressions, "regressions": regressions, "improvements": improvements}
