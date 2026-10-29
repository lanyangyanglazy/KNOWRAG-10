"""评测台的测试：指标函数、用例加载校验、运行器与报告、种子集完整性。"""

import json

import pytest
from langchain_core.documents import Document as LCDocument

from evaluation.cases import EvalCase, load_cases, validate_against_corpus
from evaluation.metrics import (
    CaseOutcome,
    compare_reports,
    evidence_coverage,
    find_leaks,
    mrr_at_k,
    normalize,
    percentile,
    recall_at_k,
    summarize,
)


# ---------- 指标：证据命中 ----------

class TestEvidence:
    def test_normalize_strips_all_whitespace(self):
        """切片换行/空格不该影响金标匹配，中文语料按去空白比较。"""
        assert normalize("一类城市 每人每天\n900 元") == "一类城市每人每天900元"

    def test_evidence_coverage_counts_each_span(self):
        chunks = ["住宿费限额标准按出差城市类别执行：一类城市每人每天 900 元，二类城市 600 元。"]
        assert evidence_coverage(chunks, ["一类城市每人每天 900 元"]) == 1.0
        assert evidence_coverage(chunks, ["900 元", "市内交通费每人每天 80 元"]) == 0.5

    def test_evidence_coverage_matches_across_chunks(self):
        """多条证据可以分散在不同切片里，命中即算。"""
        chunks = ["年休假 5 天", "伙食补助费每人每天 100 元"]
        assert evidence_coverage(chunks, ["年休假5天", "伙食补助"]) == 1.0

    def test_evidence_coverage_empty_gold_is_none(self):
        """拒答类用例没有金标证据，返回 None 表示「不适用」而不是 0。"""
        assert evidence_coverage(["任意内容"], []) is None

    def test_evidence_coverage_no_chunks_is_zero(self):
        assert evidence_coverage([], ["900 元"]) == 0.0


# ---------- 指标：召回与排序 ----------

class TestRecallAndMrr:
    def test_recall_at_k_is_fraction_of_expected_docs(self):
        retrieved = ["gov-001", "gov-003", "gov-002"]
        assert recall_at_k(retrieved, ["gov-003"], k=5) == 1.0
        assert recall_at_k(retrieved, ["gov-001", "gov-004"], k=5) == 0.5

    def test_recall_at_k_ignores_ranks_beyond_k(self):
        retrieved = ["gov-001", "gov-002", "gov-003"]
        assert recall_at_k(retrieved, ["gov-003"], k=2) == 0.0

    def test_recall_at_k_empty_expected_is_none(self):
        assert recall_at_k(["gov-001"], [], k=5) is None

    def test_mrr_at_k_uses_first_relevant_rank(self):
        assert mrr_at_k(["gov-001", "gov-003"], ["gov-003"], k=5) == pytest.approx(0.5)
        assert mrr_at_k(["gov-003", "gov-001"], ["gov-003"], k=5) == 1.0

    def test_mrr_at_k_zero_when_relevant_outside_k(self):
        assert mrr_at_k(["gov-001", "gov-002", "gov-003"], ["gov-003"], k=2) == 0.0

    def test_mrr_at_k_zero_when_nothing_retrieved(self):
        assert mrr_at_k([], ["gov-003"], k=5) == 0.0


# ---------- 指标：越权泄露 ----------

class TestLeaks:
    def test_find_leaks_returns_forbidden_docs_in_results(self):
        assert find_leaks(["gov-001", "gov-003"], ["gov-003"]) == ["gov-003"]

    def test_find_leaks_empty_when_clean(self):
        assert find_leaks(["gov-001"], ["gov-003"]) == []

    def test_find_leaks_deduplicates(self):
        assert find_leaks(["gov-003", "gov-003"], ["gov-003"]) == ["gov-003"]


# ---------- 指标：时延 ----------

class TestPercentile:
    def test_percentile_interpolates(self):
        assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
        assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)

    def test_percentile_single_value(self):
        assert percentile([42.0], 95) == 42.0

    def test_percentile_empty_is_zero(self):
        assert percentile([], 50) == 0.0


# ---------- 汇总 ----------

def _outcome(**overrides) -> CaseOutcome:
    base = dict(
        id="c1",
        question="问题",
        tags=["single_fact"],
        expected_refuse=False,
        refused=False,
        refusal_reason="",
        retrieved_doc_ids=["gov-001"],
        doc_recall=1.0,
        mrr=1.0,
        evidence=1.0,
        leaked_doc_ids=[],
        citation_recall=None,
        elapsed_ms=100.0,
        answer="",
    )
    base.update(overrides)
    return CaseOutcome(**base)


class TestSummarize:
    def test_retrieval_metrics_only_over_answerable_cases(self):
        """金标为「应答」的用例才进召回指标；拒答用例只进拒答指标。"""
        results = [
            _outcome(id="a", evidence=1.0, doc_recall=1.0, mrr=1.0),
            _outcome(id="b", evidence=0.0, doc_recall=0.0, mrr=0.0, retrieved_doc_ids=[]),
            _outcome(id="r", expected_refuse=True, refused=True,
                     evidence=None, doc_recall=None, mrr=None),
        ]
        s = summarize(results)
        assert s["cases"] == 3
        assert s["answerable"] == 2
        assert s["refusal_expected"] == 1
        assert s["evidence_recall"] == 0.5
        assert s["doc_recall"] == 0.5
        assert s["mrr"] == 0.5

    def test_wrong_refusal_costs_evidence_and_counts_as_false_refusal(self):
        """该答却拒答：证据命中记 0，同时计入误拒率，不能从分母里剔除。"""
        results = [_outcome(id="a", refused=True, refusal_reason="low_relevance",
                            evidence=0.0, doc_recall=0.0, mrr=0.0, retrieved_doc_ids=[])]
        s = summarize(results)
        assert s["evidence_recall"] == 0.0
        assert s["false_refusal_rate"] == 1.0
        assert s["refusal_accuracy"] == 0.0

    def test_missed_refusal_counts_against_accuracy(self):
        results = [_outcome(id="r", expected_refuse=True, refused=False,
                            evidence=None, doc_recall=None, mrr=None)]
        s = summarize(results)
        assert s["missed_refusal_rate"] == 1.0
        assert s["refusal_accuracy"] == 0.0

    def test_leaks_are_counted(self):
        results = [_outcome(id="a", leaked_doc_ids=["gov-003"])]
        assert summarize(results)["leaks"] == 1

    def test_latency_percentiles_reported(self):
        results = [_outcome(id=str(i), elapsed_ms=float(i)) for i in range(1, 5)]
        lat = summarize(results)["latency_ms"]
        assert lat["p50"] == pytest.approx(2.5)
        assert lat["mean"] == pytest.approx(2.5)

    def test_by_tag_breaks_down_metrics(self):
        results = [
            _outcome(id="a", tags=["single_fact"], evidence=1.0),
            _outcome(id="b", tags=["cross_doc"], evidence=0.0),
        ]
        by_tag = summarize(results)["by_tag"]
        assert by_tag["single_fact"]["cases"] == 1
        assert by_tag["single_fact"]["evidence_recall"] == 1.0
        assert by_tag["cross_doc"]["evidence_recall"] == 0.0

    def test_citation_recall_is_none_in_retrieval_mode(self):
        assert summarize([_outcome()])["citation_recall"] is None

    def test_citation_recall_averaged_when_present(self):
        results = [
            _outcome(id="a", citation_recall=1.0),
            _outcome(id="b", citation_recall=0.0),
        ]
        assert summarize(results)["citation_recall"] == 0.5


# ---------- 基线对比 ----------

def _report(**metrics) -> dict:
    base = {
        "cases": 30,
        "evidence_recall": 0.8,
        "doc_recall": 0.85,
        "mrr": 0.7,
        "refusal_accuracy": 0.9,
        "citation_recall": None,
        "leaks": 0,
    }
    base.update(metrics)
    return {"metrics": base}


class TestCompareReports:
    def test_no_change_is_ok(self):
        cmp = compare_reports(_report(), _report(), tolerance=0.03)
        assert cmp["ok"] is True
        assert cmp["regressions"] == []

    def test_regression_beyond_tolerance_fails(self):
        cmp = compare_reports(_report(evidence_recall=0.7), _report(), tolerance=0.03)
        assert cmp["ok"] is False
        assert cmp["regressions"][0]["metric"] == "evidence_recall"
        assert cmp["regressions"][0]["delta"] == pytest.approx(-0.1)

    def test_small_drop_within_tolerance_is_ok(self):
        cmp = compare_reports(_report(evidence_recall=0.79), _report(), tolerance=0.03)
        assert cmp["ok"] is True

    def test_improvement_is_recorded(self):
        cmp = compare_reports(_report(evidence_recall=0.95), _report(), tolerance=0.03)
        assert cmp["ok"] is True
        assert cmp["improvements"][0]["metric"] == "evidence_recall"

    def test_any_new_leak_fails_regardless_of_tolerance(self):
        cmp = compare_reports(_report(leaks=1), _report(leaks=0), tolerance=0.03)
        assert cmp["ok"] is False
        assert any(r["metric"] == "leaks" for r in cmp["regressions"])

    def test_metric_present_only_in_current_is_skipped(self):
        """新增指标没有基线可比，不应误判为回归。"""
        cmp = compare_reports(_report(citation_recall=0.5), _report(), tolerance=0.03)
        assert cmp["ok"] is True
        assert cmp["regressions"] == []


# ---------- 用例加载与校验 ----------

def _write_cases(tmp_path, cases: list[dict]):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8",
    )
    return path


_MINIMAL = {
    "id": "c1",
    "user": "wang",
    "question": "差旅住宿费标准是多少？",
    "expected_doc_ids": ["gov-003"],
    "evidence": ["一类城市每人每天 900 元"],
    "tags": ["single_fact"],
}


class TestLoadCases:
    def test_parses_minimal_case(self, tmp_path):
        cases = load_cases(_write_cases(tmp_path, [_MINIMAL]))
        assert len(cases) == 1
        case = cases[0]
        assert isinstance(case, EvalCase)
        assert case.id == "c1"
        assert case.user == "wang"
        assert case.expected_doc_ids == ["gov-003"]
        assert case.evidence == ["一类城市每人每天 900 元"]
        assert case.should_refuse is False
        assert case.history == []
        assert case.forbidden_doc_ids == []

    def test_keeps_optional_fields(self, tmp_path):
        raw = {
            **_MINIMAL,
            "search_query": "年休假天数如何计算？",
            "history": [
                {"role": "user", "content": "年休假有几天？"},
                {"role": "assistant", "content": "按工龄分档。"},
            ],
        }
        case = load_cases(_write_cases(tmp_path, [raw]))[0]
        assert case.search_query == "年休假天数如何计算？"
        assert len(case.history) == 2

    def test_refusal_case_uses_forbidden_docs(self, tmp_path):
        raw = {
            "id": "r1",
            "user": "zhang",
            "question": "差旅住宿费标准是多少？",
            "should_refuse": True,
            "forbidden_doc_ids": ["gov-003"],
            "tags": ["permission"],
        }
        case = load_cases(_write_cases(tmp_path, [raw]))[0]
        assert case.should_refuse is True
        assert case.forbidden_doc_ids == ["gov-003"]
        assert case.expected_doc_ids == []

    def test_skips_blank_lines_and_comments(self, tmp_path):
        path = tmp_path / "cases.jsonl"
        path.write_text(
            "# 这是一行注释\n\n" + json.dumps(_MINIMAL, ensure_ascii=False) + "\n\n",
            encoding="utf-8",
        )
        assert len(load_cases(path)) == 1

    @pytest.mark.parametrize(
        "patch,keyword",
        [
            ({"id": ""}, "id"),
            ({"user": ""}, "user"),
            ({"question": "  "}, "question"),
            ({"expected_doc_ids": []}, "expected_doc_ids"),
            ({"evidence": []}, "evidence"),
            ({"tags": "single_fact"}, "tags"),
        ],
    )
    def test_answerable_case_rejects_bad_fields(self, tmp_path, patch, keyword):
        raw = {**_MINIMAL, **patch}
        with pytest.raises(ValueError, match=keyword):
            load_cases(_write_cases(tmp_path, [raw]))

    def test_duplicate_id_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="重复"):
            load_cases(_write_cases(tmp_path, [_MINIMAL, dict(_MINIMAL)]))

    def test_refusal_case_must_not_carry_gold(self, tmp_path):
        """拒答用例同时给金标会产生歧义：到底算命中还是算泄露。"""
        raw = {
            "id": "r1", "user": "zhang", "question": "差旅住宿费标准？",
            "should_refuse": True, "expected_doc_ids": ["gov-003"],
        }
        with pytest.raises(ValueError, match="should_refuse"):
            load_cases(_write_cases(tmp_path, [raw]))

    def test_unknown_field_rejected(self, tmp_path):
        """拼错字段名（如 evidences）必须报错，否则金标被静默忽略。"""
        raw = {**_MINIMAL, "evidences": ["900 元"]}
        with pytest.raises(ValueError, match="evidences"):
            load_cases(_write_cases(tmp_path, [raw]))

    def test_bad_history_role_rejected(self, tmp_path):
        raw = {**_MINIMAL, "history": [{"role": "system", "content": "x"}]}
        with pytest.raises(ValueError, match="role"):
            load_cases(_write_cases(tmp_path, [raw]))

    def test_invalid_json_reports_line_number(self, tmp_path):
        path = tmp_path / "cases.jsonl"
        path.write_text(json.dumps(_MINIMAL, ensure_ascii=False) + "\n{坏行\n", encoding="utf-8")
        with pytest.raises(ValueError, match="第 2 行"):
            load_cases(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_cases(tmp_path / "nope.jsonl")


# ---------- 金标与语料的一致性 ----------

_CORPUS = [
    LCDocument(
        page_content="住宿费限额标准按出差城市类别执行：一类城市每人每天 900 元，二类城市 600 元。",
        metadata={"doc_id": "gov-003", "classification_level": 2, "dept": "财务处"},
    ),
    LCDocument(
        page_content="政务服务中心工作日办公时间为上午 8:30 至 12:00。",
        metadata={"doc_id": "gov-001", "classification_level": 0, "dept": "综合处"},
    ),
]


class TestValidateAgainstCorpus:
    def test_clean_case_has_no_problems(self):
        case = EvalCase(
            id="c1", user="wang", question="差旅住宿费标准是多少？",
            expected_doc_ids=["gov-003"], evidence=["一类城市每人每天 900 元"],
        )
        assert validate_against_corpus([case], _CORPUS) == []

    def test_unknown_user_reported(self):
        case = EvalCase(id="c1", user="nobody", question="q",
                        expected_doc_ids=["gov-003"], evidence=["900 元"])
        problems = validate_against_corpus([case], _CORPUS)
        assert any("nobody" in p and "用户" in p for p in problems)

    def test_unknown_doc_id_reported(self):
        case = EvalCase(id="c1", user="wang", question="q",
                        expected_doc_ids=["gov-999"], evidence=["900 元"])
        problems = validate_against_corpus([case], _CORPUS)
        assert any("gov-999" in p for p in problems)

    def test_unknown_forbidden_doc_id_reported(self):
        case = EvalCase(id="r1", user="zhang", question="q", should_refuse=True,
                        forbidden_doc_ids=["gov-888"])
        problems = validate_against_corpus([case], _CORPUS)
        assert any("gov-888" in p for p in problems)

    def test_evidence_not_in_corpus_reported(self):
        """金标证据写错一个字，整条用例就失效——必须在跑之前就拦住。"""
        case = EvalCase(id="c1", user="wang", question="q",
                        expected_doc_ids=["gov-003"], evidence=["一类城市每人每天 999 元"])
        problems = validate_against_corpus([case], _CORPUS)
        assert any("999" in p and "c1" in p for p in problems)

    def test_evidence_in_other_doc_reported(self):
        """证据必须落在金标文档里，否则用例的期望文档标错了。"""
        case = EvalCase(id="c1", user="wang", question="q",
                        expected_doc_ids=["gov-003"], evidence=["上午 8:30 至 12:00"])
        problems = validate_against_corpus([case], _CORPUS)
        assert any("gov-003" in p for p in problems)
