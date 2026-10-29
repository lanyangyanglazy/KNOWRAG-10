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


@pytest.mark.parametrize("payload", [
    # 控制器裁决：带空行间隔的非连续表头无法规范化，必须转人工（IR 依赖连续性）
    {"header_rows": [3, 5], "data_start": 6, "end_row": 57, "columns": ["a", "b"]},
    # 规则层最多接受 3 行表头，LLM 也不得突破
    {"header_rows": [3, 4, 5, 6], "data_start": 7, "end_row": 57,
     "columns": ["a", "b"]},
    # footer 只能落在数据区内
    {"header_rows": [3], "data_start": 4, "end_row": 57, "columns": ["a", "b"],
     "footer_rows": [58]},
    # footer 类型必须严格是整数列表（0/"" 不能被当成“无 footer”）
    {"header_rows": [3], "data_start": 4, "end_row": 57, "columns": ["a", "b"],
     "footer_rows": 0},
])
def test_parse_strict_contract_raises(payload):
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
    # ensure_ascii=False：raw 是模型原文，中文应按原样出现（审计用）
    llm = _FakeLLM("```json\n" + json.dumps({
        "header_rows": [1], "data_start": 2, "end_row": 2,
        "columns": ["部门", "Q1"], "footer_rows": [], "confidence": 0.8,
    }, ensure_ascii=False) + "\n```")
    candidate, raw = try_llm_header(sheet, region, llm)
    assert candidate is not None
    assert candidate.source == "llm"
    assert candidate.confidence == 0.8
    assert "部门" in raw
