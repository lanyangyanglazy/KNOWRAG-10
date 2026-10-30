from pathlib import Path
from unittest.mock import patch

import pytest

from ingest import convert


def test_find_soffice_from_env(tmp_path, monkeypatch):
    fake = tmp_path / "soffice"
    fake.write_text("")
    monkeypatch.setenv("SOFFICE_BIN", str(fake))
    assert convert.find_soffice() == fake


def test_missing_soffice_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SOFFICE_BIN", raising=False)
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.exists", return_value=False):
        assert convert.find_soffice() is None
    with pytest.raises(convert.ConversionError):
        convert.convert_document(tmp_path / "a.doc", "docx")


@pytest.mark.skipif(convert.find_soffice() is None, reason="未安装 LibreOffice")
def test_real_conversion(tmp_path):
    from docx import Document

    src = tmp_path / "a.docx"
    Document().save(src)
    out = convert.convert_document(src, "pdf", outdir=tmp_path)
    assert out.exists() and out.stat().st_size > 0
