"""
评测运行器：跑用例 → 算指标 → 出报告 → 与基线对比。

两种模式：

- `retrieval`（默认，日常回归用）：只跑 `service.retrieve()`，**不调用生成模型**。
  快、稳、可重复，适合每次改切分/换 embedding/加 rerank 后立刻跑一遍。
- `answer`（验收用）：跑完整 `service.answer_stream()`，额外统计引用命中率、
  追问改写是否生效、答案文本（留给以后的忠实度判官）。慢且依赖本地模型。

命令行：

    uv run python -m evaluation.runner                       # retrieval 基线
    uv run python -m evaluation.runner --mode answer         # 含生成
    uv run python -m evaluation.runner --baseline evaluation/reports/baseline.json
    uv run python -m evaluation.runner --save-baseline       # 把本次结果存为基线

退出码：0 = 通过；1 = 相对基线回归（可直接用于 CI 门禁）；2 = 用例金标与语料对不上。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from langchain_core.messages import AIMessage, HumanMessage

from evaluation.cases import EvalCase, load_cases, validate_against_corpus
from evaluation.metrics import (
    CaseOutcome,
    compare_reports,
    evidence_coverage,
    find_leaks,
    mrr_at_k,
    recall_at_k,
    summarize,
)

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CASES = BASE_DIR / "evaluation" / "cases.jsonl"
DEFAULT_OUT_DIR = BASE_DIR / "evaluation" / "reports"
BASELINE_NAME = "baseline.json"

MODES = ("retrieval", "answer")

# 报告里保留的答案长度（完整答案进审计日志，报告只要够定位问题）
ANSWER_PREVIEW_CHARS = 400


# ---------- 跑用例 ----------

def _to_messages(history: Sequence[dict]) -> list:
    """把用例里的 history 转成 LangChain 消息，交给服务做追问改写。"""
    messages = []
    for turn in history:
        content = turn.get("content", "")
        if turn.get("role") == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


def _read_events(events: Sequence[dict]) -> dict:
    """从 SSE 事件流里取出评测关心的字段。"""
    result = {
        "refused": False,
        "reason": "",
        "answer": "",
        "citation_doc_ids": [],
        "rewritten_to": "",
    }
    for event in events:
        kind = event.get("type")
        if kind == "rewrite":
            result["rewritten_to"] = event.get("to", "") or ""
        elif kind == "citations":
            result["citation_doc_ids"] = [
                item.get("doc_id") for item in event.get("items", []) if item.get("doc_id")
            ]
        elif kind == "done":
            result["answer"] = event.get("answer", "") or ""
            done_citations = event.get("citations")
            if done_citations:
                result["citation_doc_ids"] = [
                    item.get("doc_id") for item in done_citations if item.get("doc_id")
                ]
        elif kind == "refusal":
            result["refused"] = True
            result["reason"] = event.get("reason", "") or ""
    return result


def evaluate(
    service,
    cases: Sequence[EvalCase],
    mode: str = "retrieval",
    k: int = 5,
    users: dict | None = None,
) -> list[CaseOutcome]:
    """逐条跑用例，返回可汇总的结果列表。"""
    if mode not in MODES:
        raise ValueError(f"mode 只能是 {'/'.join(MODES)}，收到 {mode!r}")

    if users is None:
        from app.users import DEMO_USERS  # 延迟导入，避免评测台强依赖演示用户目录

        users = DEMO_USERS

    outcomes: list[CaseOutcome] = []
    for case in cases:
        user = users.get(case.user)
        if user is None:
            raise ValueError(
                f"用例 {case.id} 的用户 '{case.user}' 不在用户目录中（可选：{', '.join(sorted(users))}）"
            )

        started = time.perf_counter()

        # 检索现场：两种模式都取，证据命中要按完整切片文本判定（引用预览是截断的）
        retrieval = service.retrieve(user, case.retrieval_query)
        docs = list(getattr(retrieval, "docs", []) or [])
        doc_ids = [doc.metadata.get("doc_id") for doc in docs]
        texts = [doc.page_content for doc in docs]
        refused = not getattr(retrieval, "grounded", bool(docs))
        reason = getattr(retrieval, "reason", "") or ""

        answer = ""
        citation_ids: list[str] = []
        rewritten_to = ""
        if mode == "answer":
            stream = _read_events(list(service.answer_stream(user, case.question, _to_messages(case.history))))
            # 用户实际看到的是事件流的结论，以它为准
            refused, reason = stream["refused"], stream["reason"] or reason
            answer = stream["answer"]
            citation_ids = stream["citation_doc_ids"]
            rewritten_to = stream["rewritten_to"]

        elapsed_ms = (time.perf_counter() - started) * 1000

        outcomes.append(
            CaseOutcome(
                id=case.id,
                question=case.question,
                tags=list(case.tags),
                expected_refuse=case.should_refuse,
                refused=refused,
                refusal_reason=reason,
                retrieved_doc_ids=[d for d in doc_ids if d],
                doc_recall=recall_at_k(doc_ids, case.expected_doc_ids, k),
                mrr=mrr_at_k(doc_ids, case.expected_doc_ids, k) if case.expected_doc_ids else None,
                evidence=evidence_coverage(texts, case.evidence),
                citation_recall=(
                    recall_at_k(citation_ids, case.expected_doc_ids, k) if mode == "answer" else None
                ),
                leaked_doc_ids=find_leaks(list(doc_ids) + list(citation_ids), case.forbidden_doc_ids),
                elapsed_ms=elapsed_ms,
                answer=answer[:ANSWER_PREVIEW_CHARS],
                rewritten_to=rewritten_to,
            )
        )

    return outcomes


# ---------- 报告 ----------

def _failure_kinds(outcome: CaseOutcome) -> list[str]:
    """把一条结果归类成可行动的失败原因。"""
    kinds = []
    if outcome.leaked_doc_ids:
        kinds.append("leak")
    if not outcome.expected_refuse and outcome.refused:
        kinds.append("false_refusal")
    if outcome.expected_refuse and not outcome.refused:
        kinds.append("missed_refusal")
    if not outcome.expected_refuse:
        if outcome.evidence is not None and outcome.evidence < 1.0:
            kinds.append("evidence_miss")
        if outcome.doc_recall is not None and outcome.doc_recall < 1.0:
            kinds.append("doc_miss")
        if outcome.citation_recall is not None and outcome.citation_recall < 1.0:
            kinds.append("citation_miss")
    return kinds


def _failure_entry(outcome: CaseOutcome, kinds: list[str]) -> dict:
    return {
        "id": outcome.id,
        "question": outcome.question,
        "tags": outcome.tags,
        "kinds": kinds,
        "expected_refuse": outcome.expected_refuse,
        "refused": outcome.refused,
        "refusal_reason": outcome.refusal_reason,
        "retrieved_doc_ids": outcome.retrieved_doc_ids,
        "leaked_doc_ids": outcome.leaked_doc_ids,
        "evidence": outcome.evidence,
        "doc_recall": outcome.doc_recall,
        "mrr": outcome.mrr,
        "citation_recall": outcome.citation_recall,
    }


def make_report(
    outcomes: Sequence[CaseOutcome],
    mode: str = "retrieval",
    corpus: dict | None = None,
    config: dict | None = None,
) -> dict:
    """汇总成一份可存档、可对比的报告。"""
    failures = []
    for outcome in outcomes:
        kinds = _failure_kinds(outcome)
        if kinds:
            failures.append(_failure_entry(outcome, kinds))

    cases = []
    for outcome in outcomes:
        detail = asdict(outcome)
        cases.append(detail)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "corpus": corpus or {},
        "config": config or {},
        "metrics": summarize(outcomes),
        "failures": failures,
        "cases": cases,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms"


def render_markdown(report: dict) -> str:
    """把报告渲染成人读的 Markdown（指标 + 分类 + 失败清单）。"""
    metrics = report["metrics"]
    corpus = report.get("corpus", {})
    config = report.get("config", {})
    latency = metrics.get("latency_ms", {})

    lines = [
        f"# RAG 评测报告（{report['mode']} 模式）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 语料：{corpus.get('docs', '—')} 篇文档 / {corpus.get('chunks', '—')} 个切片",
        f"- 嵌入模型：{corpus.get('embed_model', '—')}",
        f"- 配置：Top-K={config.get('k', '—')}，用例文件 `{config.get('cases_file', '—')}`",
        "",
        "## 总体指标",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 用例数 | {metrics['cases']}（应答 {metrics['answerable']} / 应拒答 {metrics['refusal_expected']}） |",
        f"| 证据命中率 | {_pct(metrics['evidence_recall'])} |",
        f"| 文档召回率@K | {_pct(metrics['doc_recall'])} |",
        f"| MRR@K | {metrics['mrr']:.3f} |",
        f"| 引用命中率 | {_pct(metrics['citation_recall'])} |",
        f"| 拒答准确率 | {_pct(metrics['refusal_accuracy'])} |",
        f"| 误拒率（应答被拒） | {_pct(metrics['false_refusal_rate'])} |",
        f"| 漏拒率（应拒未拒） | {_pct(metrics['missed_refusal_rate'])} |",
        f"| 越权泄露 | {metrics['leaks']} |",
        f"| 时延 p50 / p95 | {_ms(latency.get('p50'))} / {_ms(latency.get('p95'))} |",
        "",
    ]

    by_tag = metrics.get("by_tag", {})
    if by_tag:
        lines += [
            "## 分类指标",
            "",
            "| 标签 | 用例 | 证据命中率 | 文档召回 | MRR | 拒答准确率 |",
            "|---|---|---|---|---|---|",
        ]
        for tag, item in by_tag.items():
            lines.append(
                f"| {tag} | {item['cases']} | {_pct(item['evidence_recall'])} "
                f"| {_pct(item['doc_recall'])} | {item['mrr']:.3f} | {_pct(item['refusal_accuracy'])} |"
            )
        lines.append("")

    failures = report.get("failures", [])
    lines += ["## 失败用例", "", f"共 {len(failures)} 条。", ""]
    if failures:
        lines += [
            "| id | 失败类型 | 问题 | 命中/期望 | 拒答原因 |",
            "|---|---|---|---|---|",
        ]
        for item in failures:
            got = ",".join(item["retrieved_doc_ids"]) or "（空）"
            lines.append(
                f"| {item['id']} | {'/'.join(item['kinds'])} | {item['question']} "
                f"| {got} / 证据 {_pct(item['evidence'])} | {item['refusal_reason'] or '—'} |"
            )
        lines.append("")

    return "\n".join(lines)


def write_report(report: dict, out_dir: str | Path | None = None) -> tuple[Path, Path]:
    """报告落盘为 JSON（机器对比用）+ Markdown（人读），返回两个路径。"""
    out_dir = Path(out_dir or DEFAULT_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"eval-{report['mode']}-{stamp}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# ---------- 端到端 ----------

def _corpus_stats(service) -> dict:
    stats_fn = getattr(service, "stats", None)
    if callable(stats_fn):
        try:
            stats = stats_fn()
            return {
                key: stats[key]
                for key in ("docs", "chunks", "embed_model", "llm_model", "max_distance")
                if key in stats
            }
        except Exception:  # 服务未就绪时退回按切片估算，评测不该因此中断
            pass
    documents = getattr(service, "documents", []) or []
    return {
        "docs": len({doc.metadata.get("doc_id") for doc in documents}),
        "chunks": len(documents),
    }


def _build_service():
    """构造真实的政务 RAG 服务（需要 Ollama 在运行）。"""
    from app.rag_service import RagService

    return RagService()


def run(
    cases_path: str | Path = DEFAULT_CASES,
    mode: str = "retrieval",
    service=None,
    out_dir: str | Path | None = None,
    baseline: str | Path | None = None,
    tolerance: float = 0.03,
    k: int = 5,
    save_baseline: bool = False,
) -> tuple[int, dict | None, dict | None]:
    """
    跑一轮评测，返回 (退出码, 报告, 基线对比)。

    金标与语料对不上时直接退出（码 2）：带着错题跑出来的 0 分没有任何解释力。
    """
    if mode not in MODES:
        raise ValueError(f"mode 只能是 {'/'.join(MODES)}，收到 {mode!r}")

    cases = load_cases(cases_path)
    service = service if service is not None else _build_service()

    problems = validate_against_corpus(cases, getattr(service, "documents", []) or [])
    if problems:
        print(f"❌ 用例金标与语料不一致（{len(problems)} 处），已中止：")
        for problem in problems:
            print(f"   - {problem}")
        return 2, None, None

    print(f"▶ 开始评测：{len(cases)} 条用例，模式 {mode}，Top-K={k}")
    outcomes = evaluate(service, cases, mode=mode, k=k)
    report = make_report(
        outcomes,
        mode=mode,
        corpus=_corpus_stats(service),
        config={"k": k, "cases_file": str(cases_path), "tolerance": tolerance},
    )

    json_path, md_path = write_report(report, out_dir)
    if save_baseline:
        baseline_path = Path(out_dir or DEFAULT_OUT_DIR) / BASELINE_NAME
        baseline_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 已保存为基线：{baseline_path}")

    metrics = report["metrics"]
    print(
        f"✅ 证据命中率 {_pct(metrics['evidence_recall'])} | 文档召回 {_pct(metrics['doc_recall'])} "
        f"| MRR {metrics['mrr']:.3f} | 拒答准确率 {_pct(metrics['refusal_accuracy'])} "
        f"| 越权泄露 {metrics['leaks']}"
    )
    print(f"   失败 {len(report['failures'])} 条；报告：{md_path}")

    comparison = None
    if baseline:
        baseline_path = Path(baseline)
        if not baseline_path.exists():
            raise FileNotFoundError(f"基线报告不存在: {baseline_path}")
        baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
        comparison = compare_reports(report, baseline_report, tolerance=tolerance)
        for item in comparison["improvements"]:
            print(f"   ↑ {item['metric']}: {item['baseline']:.3f} → {item['current']:.3f}")
        for item in comparison["regressions"]:
            print(f"   ↓ {item['metric']}: {item['baseline']:.3f} → {item['current']:.3f}")
        if not comparison["ok"]:
            print(f"⛔ 相对基线 {baseline_path.name} 出现回归，门禁不通过")
            return 1, report, comparison
        print(f"🟢 相对基线 {baseline_path.name} 无回归")

    return 0, report, comparison


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文报告摘要会 UnicodeEncodeError；统一切到 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="RAG 效果评测：跑用例、出报告、比基线")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="用例 JSONL 路径")
    parser.add_argument("--mode", default="retrieval", choices=MODES,
                        help="retrieval=只跑检索（不调 LLM）；answer=跑完整问答")
    parser.add_argument("--k", type=int, default=5, help="Top-K，默认 5")
    parser.add_argument("--out", default=None, help="报告输出目录")
    parser.add_argument("--baseline", default=None, help="基线报告 JSON，用于回归门禁")
    parser.add_argument("--tolerance", type=float, default=0.03, help="允许的指标跌幅，默认 0.03")
    parser.add_argument("--save-baseline", action="store_true", help="把本次结果另存为 baseline.json")
    args = parser.parse_args(argv)

    code, _, _ = run(
        cases_path=args.cases,
        mode=args.mode,
        out_dir=args.out,
        baseline=args.baseline,
        tolerance=args.tolerance,
        k=args.k,
        save_baseline=args.save_baseline,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
