import pytest
from pydantic import ValidationError

from ingest.ir import DocModel


def _table(**over):
    data = {
        "table_key": "t1",
        "sheet": "2025预算",
        "range": "A3:H7",
        "header_rows": [3, 4],
        "header_values": [["", "Q1"], ["部门", "预算"]],
        "columns": [
            {"key": "c1", "name": "部门", "dtype": "string", "source_names": ["部门"]},
            {"key": "c2", "name": "Q1/预算", "dtype": "number", "unit": "万元"},
        ],
        "rows": [["综合处", "120"], ["人事处", "98"], ["合计", "218"]],
        "footer_rows": [7],
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
    assert table.full_grid()[0] == ["", "Q1"]


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
