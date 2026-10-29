from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import filetype
import fitz

from ingest.config import Settings, get_settings
from ingest.errors import Quarantined, TooLarge, UnsupportedType

OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_TOTAL_LIMIT = 500 * 1024 * 1024
ZIP_RATIO_LIMIT = 100
EXT_KIND = {".docx": "docx", ".doc": "doc", ".xlsx": "xlsx",
            ".xlsm": "xlsm", ".pdf": "pdf"}


@dataclass(frozen=True)
class SniffResult:
    kind: str
    mime: str
    size_bytes: int
    sha256: str
    filename: str
    encrypted: bool = False
    macro: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detect_kind(path: Path) -> tuple[str | None, str, bool]:
    """返回 (kind, mime, has_macro)。只读本地字节，不做任何外联。"""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        has_macro = any(name.endswith("vbaProject.bin") for name in names)
        if "word/document.xml" in names:
            return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", has_macro
        if "xl/workbook.xml" in names:
            if has_macro:
                return "xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", True
            return "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", False
        return None, "application/zip", has_macro
    with path.open("rb") as f:
        head = f.read(8)
    if head == OLE_MAGIC:
        return "doc", "application/msword", False
    if head.startswith(b"%PDF"):
        return "pdf", "application/pdf", False
    guessed = filetype.guess(str(path))
    return None, (guessed.mime if guessed else "application/octet-stream"), False


def _check_zip_safety(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos)
        compressed = max(1, sum(i.compress_size for i in infos))
        if total > ZIP_TOTAL_LIMIT or total / compressed > ZIP_RATIO_LIMIT:
            raise Quarantined("zip 解压总量或压缩比超限")
        for info in infos:
            name = info.filename
            parts = Path(name).parts
            if (not name or name.startswith("/") or Path(name).drive
                    or (len(name) > 1 and name[1] == ":")
                    or ".." in parts or "\\" in name):
                raise Quarantined(f"压缩包内路径非法: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise Quarantined(f"压缩包内包含符号链接: {name}")


def sniff_file(path: Path, filename: str, settings: Settings | None = None) -> SniffResult:
    s = settings or get_settings()
    size = path.stat().st_size
    if size == 0:
        raise UnsupportedType("空文件")
    if size > s.max_file_bytes:
        raise TooLarge(f"文件超过 {s.max_file_mb} MB")
    ext = Path(filename).suffix.lower()
    if ext not in EXT_KIND:
        raise UnsupportedType(f"不支持的扩展名: {ext or '(无)'}")
    try:
        kind, mime, has_macro = _detect_kind(path)
    except zipfile.BadZipFile as exc:
        raise UnsupportedType("无法识别的文件内容") from exc
    if kind is None:
        raise UnsupportedType("无法识别的文件内容")
    expected = EXT_KIND[ext]
    if kind != expected:
        if kind == "doc" and expected in ("docx", "xlsx", "xlsm"):
            raise Quarantined("文件已加密或为旧版 OLE 容器")
        raise UnsupportedType(f"扩展名 {ext} 与内容 {kind} 不一致")

    warnings: list[str] = []
    encrypted = False
    if kind in ("docx", "xlsx", "xlsm"):
        _check_zip_safety(path)
    if kind == "pdf":
        try:
            doc = fitz.open(path)
        except RuntimeError as exc:
            raise UnsupportedType("PDF 文件损坏或无法解析") from exc
        try:
            if doc.needs_pass:
                encrypted = True
                raise Quarantined("PDF 已加密")
            if doc.page_count > s.max_pdf_pages:
                raise TooLarge(f"PDF 超过 {s.max_pdf_pages} 页")
        finally:
            doc.close()
    if has_macro:
        warnings.append("macro_stripped")
    return SniffResult(
        kind=kind, mime=mime, size_bytes=size, sha256=sha256_file(path),
        filename=filename, encrypted=encrypted, macro=has_macro,
        warnings=tuple(warnings),
    )


_REL_MACRO = re.compile(rb'<Relationship[^>]*vbaProject\.bin[^>]*/>')
_OVERRIDE_MACRO = re.compile(rb'<Override[^>]*vbaProject\.bin[^>]*/>')


def strip_macros(src: Path, dst: Path) -> bool:
    """复制 zip 并移除 vbaProject.bin 及其关系；返回是否剥离过。"""
    stripped = False
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename.endswith("vbaProject.bin"):
                stripped = True
                continue
            data = zin.read(info.filename)
            if info.filename in ("xl/_rels/workbook.xml.rels", "[Content_Types].xml"):
                data = _REL_MACRO.sub(b"", _OVERRIDE_MACRO.sub(b"", data))
            zout.writestr(info, data)
    return stripped
