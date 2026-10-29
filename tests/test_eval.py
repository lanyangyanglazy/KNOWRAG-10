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
from evaluation.runner import evaluate, make_report, render_markdown, run, write_report


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


# ---------- 运行器 ----------

class _FakeRetrieval:
    """RetrievalResult 的最小替身。"""

    def __init__(self, docs, grounded=True, reason=""):
        self.docs = docs
        self.grounded = grounded
        self.reason = reason
        self.best_distance = 0.42 if grounded else 0.91
        self.visible_docs = 2
        self.visible_chunks = len(docs)


class _FakeEvalService:
    """评测台只需要 retrieve / answer_stream / documents 三个入口。"""

    def __init__(self, documents, result=None, events=None):
        self.documents = documents
        self.result = result if result is not None else _FakeRetrieval([])
        self.events = events or []
        self.retrieve_calls: list[tuple] = []
        self.stream_calls: list[tuple] = []

    def retrieve(self, user, query):
        self.retrieve_calls.append((user.username, query))
        return self.result

    def answer_stream(self, user, question, history=None):
        self.stream_calls.append((user.username, question, list(history or [])))
        yield from self.events


def _citations(*doc_ids):
    return [{"rank": i, "doc_id": d, "title": d, "preview": "..."} for i, d in enumerate(doc_ids, 1)]


def _done_events(*doc_ids, answer="一类城市每人每天 900 元。"):
    return [
        {"type": "citations", "items": _citations(*doc_ids)},
        {"type": "token", "text": answer},
        {"type": "done", "answer": answer, "citations": _citations(*doc_ids), "elapsed_ms": 12},
    ]


_NUM_CASE = EvalCase(
    id="c1", user="wang", question="差旅住宿费标准是多少？",
    expected_doc_ids=["gov-003"], evidence=["一类城市每人每天 900 元"], tags=["numeric"],
)


class TestEvaluateRetrievalMode:
    def test_scores_a_clean_hit(self):
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval(_CORPUS[:1]))
        out = evaluate(service, [_NUM_CASE], mode="retrieval")[0]

        assert out.refused is False
        assert out.retrieved_doc_ids == ["gov-003"]
        assert out.evidence == 1.0
        assert out.doc_recall == 1.0
        assert out.mrr == 1.0
        assert out.citation_recall is None, "retrieval 模式没有引用数据"
        assert out.elapsed_ms >= 0

    def test_never_calls_the_generator(self):
        """retrieval-only 必须不调 LLM，否则日常回归又慢又抖。"""
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval(_CORPUS[:1]))
        evaluate(service, [_NUM_CASE], mode="retrieval")
        assert service.retrieve_calls == [("wang", "差旅住宿费标准是多少？")]
        assert service.stream_calls == []

    def test_uses_search_query_for_multi_turn(self):
        case = EvalCase(
            id="m1", user="li", question="那要提前几天申请？",
            expected_doc_ids=["gov-002"], evidence=["年休假 5 天"],
            search_query="年休假需要提前几个工作日申请？", tags=["multi_turn"],
            history=[{"role": "user", "content": "年休假有几天？"}],
        )
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval([]))
        evaluate(service, [case], mode="retrieval")
        assert service.retrieve_calls[0][1] == "年休假需要提前几个工作日申请？"

    def test_wrong_refusal_scores_zero_not_skipped(self):
        service = _FakeEvalService(
            _CORPUS, result=_FakeRetrieval([], grounded=False, reason="low_relevance")
        )
        out = evaluate(service, [_NUM_CASE], mode="retrieval")[0]
        assert out.refused is True
        assert out.refusal_reason == "low_relevance"
        assert (out.evidence, out.doc_recall, out.mrr) == (0.0, 0.0, 0.0)

    def test_refusal_case_metrics_are_not_applicable(self):
        case = EvalCase(id="r1", user="zhang", question="差旅住宿费标准是多少？",
                        should_refuse=True, forbidden_doc_ids=["gov-003"], tags=["permission"])
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval([], grounded=False, reason="low_relevance"))
        out = evaluate(service, [case], mode="retrieval")[0]
        assert (out.evidence, out.doc_recall, out.mrr) == (None, None, None)
        assert out.refused is True
        assert out.leaked_doc_ids == []

    def test_forbidden_doc_in_results_is_a_leak(self):
        case = EvalCase(id="r1", user="zhang", question="差旅住宿费标准是多少？",
                        should_refuse=True, forbidden_doc_ids=["gov-003"], tags=["permission"])
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval(_CORPUS[:1]))
        out = evaluate(service, [case], mode="retrieval")[0]
        assert out.leaked_doc_ids == ["gov-003"]

    def test_unknown_user_raises(self):
        case = EvalCase(id="x", user="nobody", question="q",
                        expected_doc_ids=["gov-003"], evidence=["900 元"])
        service = _FakeEvalService(_CORPUS)
        with pytest.raises(ValueError, match="nobody"):
            evaluate(service, [case])


class TestEvaluateAnswerMode:
    def test_collects_answer_and_citation_recall(self):
        service = _FakeEvalService(
            _CORPUS, result=_FakeRetrieval(_CORPUS[:1]), events=_done_events("gov-003")
        )
        out = evaluate(service, [_NUM_CASE], mode="answer")[0]

        assert out.refused is False
        assert out.answer == "一类城市每人每天 900 元。"
        assert out.citation_recall == 1.0
        assert out.evidence == 1.0, "证据命中仍按完整切片文本判定，不看引用预览"
        assert service.stream_calls and service.retrieve_calls

    def test_citation_recall_zero_when_gold_doc_not_cited(self):
        service = _FakeEvalService(
            _CORPUS, result=_FakeRetrieval(_CORPUS[:1]), events=_done_events("gov-001")
        )
        out = evaluate(service, [_NUM_CASE], mode="answer")[0]
        assert out.citation_recall == 0.0

    def test_reads_refusal_event(self):
        events = [{"type": "refusal", "reason": "no_visible_docs", "message": "无依据"}]
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval([]), events=events)
        out = evaluate(service, [_NUM_CASE], mode="answer")[0]
        assert out.refused is True
        assert out.refusal_reason == "no_visible_docs"
        assert out.answer == ""
        assert out.citation_recall == 0.0

    def test_history_is_converted_to_messages(self):
        case = EvalCase(
            id="m1", user="li", question="那要提前几天申请？",
            expected_doc_ids=["gov-002"], evidence=["年休假 5 天"], tags=["multi_turn"],
            history=[
                {"role": "user", "content": "年休假有几天？"},
                {"role": "assistant", "content": "按工龄分档，满 1 年不满 10 年 5 天。"},
            ],
        )
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval([]), events=_done_events("gov-002"))
        evaluate(service, [case], mode="answer")

        _, question, history = service.stream_calls[0]
        assert question == "那要提前几天申请？"
        assert [type(m).__name__ for m in history] == ["HumanMessage", "AIMessage"]
        assert history[0].content == "年休假有几天？"

    def test_rewrite_event_is_recorded(self):
        events = [{"type": "rewrite", "from": "那要提前几天申请？", "to": "年休假需要提前几天申请？"}]
        events += _done_events("gov-002")
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval([]), events=events)
        case = EvalCase(id="m1", user="li", question="那要提前几天申请？",
                        expected_doc_ids=["gov-002"], evidence=["年休假 5 天"])
        out = evaluate(service, [case], mode="answer")[0]
        assert out.rewritten_to == "年休假需要提前几天申请？"


# ---------- 报告 ----------

def _good_outcome(case_id="a"):
    return CaseOutcome(id=case_id, question="q", tags=["numeric"], expected_refuse=False,
                       refused=False, retrieved_doc_ids=["gov-003"], doc_recall=1.0,
                       mrr=1.0, evidence=1.0, elapsed_ms=120.0, answer="900 元")


class TestReport:
    def test_clean_run_has_no_failures(self):
        report = make_report([_good_outcome()], mode="retrieval",
                             corpus={"docs": 4, "chunks": 26}, config={"k": 5})
        assert report["metrics"]["cases"] == 1
        assert report["metrics"]["evidence_recall"] == 1.0
        assert report["failures"] == []
        assert report["mode"] == "retrieval"
        assert report["corpus"]["docs"] == 4
        assert report["config"]["k"] == 5
        assert report["generated_at"]

    def test_failures_are_classified(self):
        outcomes = [
            _good_outcome(),
            CaseOutcome(id="b", question="q", expected_refuse=False, refused=True,
                        refusal_reason="low_relevance", evidence=0.0, doc_recall=0.0, mrr=0.0),
            CaseOutcome(id="c", question="q", expected_refuse=True, refused=False,
                        leaked_doc_ids=["gov-003"]),
            CaseOutcome(id="d", question="q", expected_refuse=False, refused=False,
                        retrieved_doc_ids=["gov-001"], doc_recall=0.0, mrr=0.0, evidence=0.5),
        ]
        failures = {f["id"]: f["kinds"] for f in make_report(outcomes, mode="retrieval")["failures"]}
        assert "false_refusal" in failures["b"]
        assert "missed_refusal" in failures["c"]
        assert "leak" in failures["c"]
        assert "evidence_miss" in failures["d"]
        assert "doc_miss" in failures["d"]

    def test_per_case_detail_is_kept_for_debugging(self):
        report = make_report([_good_outcome()], mode="retrieval")
        detail = report["cases"][0]
        assert detail["id"] == "a"
        assert detail["retrieved_doc_ids"] == ["gov-003"]
        assert detail["evidence"] == 1.0

    def test_markdown_shows_metrics_and_failures(self):
        outcomes = [_good_outcome(), CaseOutcome(id="b", question="差旅标准？", tags=["numeric"],
                                                 expected_refuse=False, refused=True,
                                                 refusal_reason="low_relevance", evidence=0.0,
                                                 doc_recall=0.0, mrr=0.0)]
        md = render_markdown(make_report(outcomes, mode="retrieval"))
        assert "证据命中率" in md
        assert "50.0%" in md, "指标应以百分比呈现，便于人读"
        assert "误拒" in md
        assert "b" in md and "low_relevance" in md

    def test_write_report_creates_json_and_markdown(self, tmp_path):
        report = make_report([_good_outcome()], mode="retrieval")
        json_path, md_path = write_report(report, tmp_path)

        assert json_path.exists() and md_path.exists()
        assert "retrieval" in json_path.name and json_path.suffix == ".json"
        assert md_path.suffix == ".md"
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        assert loaded["metrics"]["cases"] == 1
        assert md_path.read_text(encoding="utf-8").startswith("#")


# ---------- 端到端：run() ----------

class TestRun:
    def test_writes_report_and_exits_zero(self, tmp_path):
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval(_CORPUS[:1]))
        code, report, comparison = run(
            _write_cases(tmp_path, [_MINIMAL]),
            mode="retrieval", service=service, out_dir=tmp_path / "reports",
        )
        assert code == 0
        assert comparison is None
        assert report["metrics"]["evidence_recall"] == 1.0
        assert list((tmp_path / "reports").glob("*.json"))

    def test_fails_fast_when_gold_does_not_match_corpus(self, tmp_path):
        """金标写错就别跑了：跑完只会得到一堆无法解释的 0 分。"""
        bad = {**_MINIMAL, "evidence": ["一类城市每人每天 999 元"]}
        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval(_CORPUS[:1]))
        code, report, _ = run(
            _write_cases(tmp_path, [bad]), mode="retrieval", service=service,
            out_dir=tmp_path / "reports",
        )
        assert code == 2
        assert report is None
        assert not (tmp_path / "reports").exists()

    def test_returns_one_on_regression_against_baseline(self, tmp_path):
        baseline = {"metrics": {"cases": 1, "evidence_recall": 1.0, "doc_recall": 1.0,
                                "mrr": 1.0, "refusal_accuracy": 1.0, "citation_recall": None,
                                "leaks": 0}}
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval([], grounded=False,
                                                                 reason="low_relevance"))
        code, report, comparison = run(
            _write_cases(tmp_path, [_MINIMAL]), mode="retrieval", service=service,
            out_dir=tmp_path / "reports", baseline=baseline_path,
        )
        assert code == 1
        assert comparison["ok"] is False
        assert report["metrics"]["evidence_recall"] == 0.0

    def test_returns_zero_when_improved_over_baseline(self, tmp_path):
        baseline = {"metrics": {"cases": 1, "evidence_recall": 0.5, "doc_recall": 0.5,
                                "mrr": 0.5, "refusal_accuracy": 1.0, "citation_recall": None,
                                "leaks": 0}}
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

        service = _FakeEvalService(_CORPUS, result=_FakeRetrieval(_CORPUS[:1]))
        code, _, comparison = run(
            _write_cases(tmp_path, [_MINIMAL]), mode="retrieval", service=service,
            out_dir=tmp_path / "reports", baseline=baseline_path,
        )
        assert code == 0
        assert comparison["ok"] is True
        assert comparison["improvements"]

    def test_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(ValueError, match="mode"):
            run(_write_cases(tmp_path, [_MINIMAL]), mode="vibes",
                service=_FakeEvalService(_CORPUS))

    def test_missing_baseline_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run(_write_cases(tmp_path, [_MINIMAL]), mode="retrieval",
                service=_FakeEvalService(_CORPUS), baseline=tmp_path / "nope.json")


class TestSeedCorpus:
    """真实种子集自身必须合法，否则回归门禁的输入就是错的。"""

    @pytest.fixture(scope="class")
    def seed_cases(self):
        from app.rag_service import load_gov_documents
        from evaluation.runner import DEFAULT_CASES

        cases = load_cases(DEFAULT_CASES)
        problems = validate_against_corpus(cases, load_gov_documents())
        assert problems == [], problems
        return cases

    def test_covers_all_known_tags(self, seed_cases):
        from evaluation.cases import KNOWN_TAGS

        used = {tag for case in seed_cases for tag in case.tags}
        assert used == KNOWN_TAGS, f"种子集缺少标签：{KNOWN_TAGS - used}"

    def test_covers_all_demo_users(self, seed_cases):
        from app.users import DEMO_USERS

        assert {case.user for case in seed_cases} == set(DEMO_USERS)

    def test_has_enough_answerable_and_refusal_cases(self, seed_cases):
        assert sum(not case.should_refuse for case in seed_cases) >= 10
        assert sum(case.should_refuse for case in seed_cases) >= 5

    def test_permission_cases_declare_forbidden_docs(self, seed_cases):
        for case in seed_cases:
            if "permission" in case.tags:
                assert case.forbidden_doc_ids, f"{case.id} 权限用例必须声明 forbidden_doc_ids"
