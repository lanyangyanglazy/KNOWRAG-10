"""
FAISS → pgvector 切换门禁（设计 §13.1 / §14.1）。

同一批用例、同一个嵌入模型，分别跑 FAISS 与 pg 后端，再比较指标：

- `HIGHER_IS_BETTER` 指标跌幅 > `--tolerance`（默认 3%）→ 阻断；
- pg 端出现任何越权泄露 → 阻断（安全项零容忍，不给容差）；
- 用例数缩水 → 阻断（避免「少跑几条让指标变好看」）。

退出码：0 通过；1 有回归；2 金标与语料对不上（无法比较）。

命令行：

    uv run python -m evaluation.compare_backends
    uv run python -m evaluation.compare_backends --cases evaluation/cases.jsonl --tolerance 0.03
    uv run python -m evaluation.compare_backends --mode answer     # 含生成，依赖本地模型
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from evaluation import runner
from evaluation.metrics import compare_reports

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CASES = BASE_DIR / "evaluation" / "cases.jsonl"
DEFAULT_OUT_DIR = BASE_DIR / "evaluation" / "reports"
TOLERANCE = 0.03

_METRIC_LABELS = {
    "evidence_recall": "证据命中率",
    "doc_recall": "文档召回@K",
    "mrr": "MRR@K",
    "citation_recall": "引用命中率",
    "refusal_accuracy": "拒答准确率",
    "false_refusal_rate": "误拒率",
    "missed_refusal_rate": "漏拒率",
    "leaks": "越权泄露",
}


def _assert_comparable() -> None:
    """两个后端的嵌入模型必须一致，否则向量空间不同，指标对比没有意义。"""
    import main as core
    from ingest.config import get_settings

    faiss_model = core.EMBED_MODEL
    pg_model = get_settings().embed_model
    if faiss_model != pg_model:
        # SystemExit("msg") 的退出码是 1，会和「有回归」撞车；先打到 stderr 再以 2 退出。
        print(
            f"❌ 两个后端的嵌入模型不同（FAISS={faiss_model} / pg={pg_model}），指标不可比。\n"
            "   请把 EMBED_MODEL 与 INGEST_EMBED_MODEL 设为同一模型后重跑。",
            file=sys.stderr,
        )
        raise SystemExit(2)


def judge(pg_report: dict, faiss_report: dict,
          tolerance: float = TOLERANCE) -> dict:
    """比较两份 runner 报告，返回 `{ok, regressions, improvements, reasons}`。"""
    verdict = compare_reports(pg_report, faiss_report, tolerance=tolerance)
    # 浮点减法会把「跌幅恰好等于容差」算成略超（0.97 - 1.0 = -0.030000000000000027），
    # 按 9 位小数归一后再判，避免 3% 的边界被二进制误差翻转。
    verdict["regressions"] = [
        item for item in verdict["regressions"]
        if round(item["delta"], 9) < -tolerance
    ]
    verdict["ok"] = not verdict["regressions"]

    baseline_leaks = int(faiss_report.get("metrics", {}).get("leaks", 0) or 0)
    pg_leaks = int(pg_report.get("metrics", {}).get("leaks", 0) or 0)
    if pg_leaks > 0:
        verdict["ok"] = False
        if not any(item["metric"] == "leaks" for item in verdict["regressions"]):
            verdict["regressions"].append({
                "metric": "leaks",
                "baseline": baseline_leaks,
                "current": pg_leaks,
                "delta": pg_leaks - baseline_leaks,
            })

    verdict["reasons"] = [
        f"{item['metric']}: {item['baseline']} → {item['current']}"
        for item in verdict["regressions"]
    ]
    return verdict


def compare(
    cases_path: str | Path = DEFAULT_CASES,
    mode: str = "retrieval",
    k: int = 5,
    tolerance: float = TOLERANCE,
    out_dir: str | Path | None = None,
) -> tuple[int, dict | None]:
    _assert_comparable()
    out_dir = Path(out_dir or DEFAULT_OUT_DIR)

    print("▶ 后端对比 1/2：FAISS（现有主线）")
    faiss_code, faiss_report, _ = runner.run(
        cases_path=cases_path, mode=mode, backend="faiss", k=k,
        out_dir=out_dir / "faiss")
    if faiss_report is None:
        return faiss_code, None

    print("▶ 后端对比 2/2：pgvector（摄取库）")
    pg_code, pg_report, _ = runner.run(
        cases_path=cases_path, mode=mode, backend="pg", k=k,
        out_dir=out_dir / "pg")
    if pg_report is None:
        return pg_code, None

    verdict = judge(pg_report, faiss_report, tolerance=tolerance)
    report_path = _write_verdict(verdict, pg_report, faiss_report,
                                 tolerance=tolerance, out_dir=out_dir)
    if verdict["ok"]:
        print(f"🟢 后端对比通过（跌幅 ≤ {tolerance:.0%}，零泄露）；报告：{report_path}")
        return 0, verdict

    print("⛔ 后端对比未通过：")
    for reason in verdict["reasons"]:
        print(f"   - {reason}")
    print(f"   报告：{report_path}")
    return 1, verdict


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{value * 100:.1f}%"


def render_markdown(verdict: dict, pg_report: dict, faiss_report: dict,
                    tolerance: float) -> str:
    faiss_metrics = faiss_report.get("metrics", {})
    pg_metrics = pg_report.get("metrics", {})
    lines = [
        "# 检索后端对比（pg vs FAISS）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 用例数：FAISS {faiss_metrics.get('cases', '—')} / pg {pg_metrics.get('cases', '—')}",
        f"- 容差：{tolerance:.0%}（越大越好的指标；泄露零容忍）",
        f"- 结论：{'✅ 通过' if verdict['ok'] else '⛔ 不通过'}",
        "",
        "| 指标 | FAISS | pg | Δ |",
        "|---|---|---|---|",
    ]
    for metric, label in _METRIC_LABELS.items():
        left = faiss_metrics.get(metric)
        right = pg_metrics.get(metric)
        if left is None and right is None:
            continue
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            delta = right - left
            delta_text = f"{delta:+d}" if metric == "leaks" else f"{delta:+.3f}"
        else:
            delta_text = "—"
        lines.append(f"| {label} | {_fmt(left)} | {_fmt(right)} | {delta_text} |")

    if verdict["regressions"]:
        lines += ["", "## 回归项", ""]
        for item in verdict["regressions"]:
            lines.append(f"- `{item['metric']}`：{item['baseline']} → {item['current']}")
    if verdict["improvements"]:
        lines += ["", "## 改进项", ""]
        for item in verdict["improvements"]:
            lines.append(f"- `{item['metric']}`：{item['baseline']} → {item['current']}")
    if not verdict["ok"]:
        lines += ["", "> 未通过时不要把 `RETRIEVAL_BACKEND` 切到 `pg`；"
                      "先定位是切分、embedding、RRF 还是拒答阈值的问题。"]
    return "\n".join(lines) + "\n"


def _write_verdict(verdict: dict, pg_report: dict, faiss_report: dict, *,
                   tolerance: float, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"compare-{stamp}.json"
    md_path = out_dir / f"compare-{stamp}.md"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tolerance": tolerance,
        "verdict": verdict,
        "faiss": faiss_report.get("metrics", {}),
        "pg": pg_report.get("metrics", {}),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render_markdown(verdict, pg_report, faiss_report, tolerance),
                       encoding="utf-8")
    return md_path


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文报告摘要会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="FAISS → pgvector 切换门禁")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="用例 JSONL 路径")
    parser.add_argument("--mode", default="retrieval", choices=runner.MODES,
                        help="retrieval=只跑检索；answer=跑完整问答")
    parser.add_argument("--k", type=int, default=5, help="Top-K，默认 5")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE,
                        help="允许的指标跌幅，默认 0.03")
    parser.add_argument("--out", default=None, help="对比报告目录")
    args = parser.parse_args(argv)

    code, _ = compare(cases_path=args.cases, mode=args.mode, k=args.k,
                      tolerance=args.tolerance, out_dir=args.out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
