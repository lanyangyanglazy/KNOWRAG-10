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
