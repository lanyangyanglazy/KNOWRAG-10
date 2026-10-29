import zipfile
from dataclasses import replace

import fitz
import pytest

from ingest.config import get_settings
from ingest.errors import Quarantined, TooLarge, UnsupportedType
from ingest.sniff import sniff_file, strip_macros
from tests.ingest.builders import build_xlsx


def test_sniffs_xlsx(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [["部门", "金额"], ["综合处", 120]])
    result = sniff_file(path, "a.xlsx")
    assert result.kind == "xlsx"
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    assert result.macro is False


def test_extension_must_match_content(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [["x"]])
    with pytest.raises(UnsupportedType):
        sniff_file(path, "a.docx")


def test_rejects_unknown_and_oversize(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a document")
    with pytest.raises(UnsupportedType):
        sniff_file(junk, "junk.docx")

    path = build_xlsx(tmp_path / "a.xlsx", [["x"]])
    small = replace(get_settings(), max_file_mb=0)
    with pytest.raises(TooLarge):
        sniff_file(path, "a.xlsx", small)


def test_rejects_zip_bomb_ratio(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsx", [["x"]])
    with zipfile.ZipFile(path, "a") as zf:
        # 追加模式默认 ZIP_STORED，压缩比恒为 1；显式 DEFLATE 才是真实压缩炸弹
        zf.writestr("xl/media/big.bin", b"\0" * 30_000_000,
                    compress_type=zipfile.ZIP_DEFLATED)
    with pytest.raises(Quarantined):
        sniff_file(path, "a.xlsx")


def test_detects_and_strips_macros(tmp_path):
    path = build_xlsx(tmp_path / "a.xlsm", [["x"]])
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("xl/vbaProject.bin", b"macro")
    assert sniff_file(path, "a.xlsm").macro is True

    clean = tmp_path / "clean.xlsm"
    assert strip_macros(path, clean) is True
    with zipfile.ZipFile(clean) as zf:
        assert "xl/vbaProject.bin" not in zf.namelist()


def test_encrypted_pdf_is_quarantined(tmp_path):
    path = tmp_path / "secret.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(Quarantined):
        sniff_file(path, "secret.pdf")
