from __future__ import annotations

import contextlib
import hashlib
import posixpath
import re
import zipfile
import zlib
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


_MACRO_PART = re.compile(r"^vbaproject.*\.bin$", re.IGNORECASE)


def _is_macro_part(name: str) -> bool:
    """宏部件（word/vbaProject.bin、xl/vbaProjectSignature.bin 等），忽略大小写。"""
    return bool(_MACRO_PART.match(posixpath.basename(name)))


def _detect_kind(path: Path) -> tuple[str | None, str, bool]:
    """返回 (kind, mime, has_macro)。只读本地字节，不做任何外联。"""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        has_macro = any(_is_macro_part(name) for name in names)
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


# Relationship/Override 元素可能自闭合，也可能写成成对空标签
_RELATIONSHIP = re.compile(
    rb"<Relationship\b[^>]*/>|<Relationship\b[^>]*>\s*</Relationship>",
    re.IGNORECASE,
)
_OVERRIDE = re.compile(
    rb"<Override\b[^>]*/>|<Override\b[^>]*>\s*</Override>",
    re.IGNORECASE,
)
_VBA_REF = re.compile(rb"vbaproject", re.IGNORECASE)

# 归档读取/重写阶段的 zip 与 IO 失败：BadZipFile、成员缺失/CRC 错误、
# 加密成员（RuntimeError）、不支持的压缩算法、磁盘错误
_STRIP_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    KeyError,
    NotImplementedError,
    OSError,
    RuntimeError,
    zlib.error,
)


def _drop_vba_elements(pattern: re.Pattern[bytes], data: bytes) -> bytes:
    """删除引用 vbaProject 的 Relationship/Override 元素（忽略大小写）。"""

    def keep(match: re.Match[bytes]) -> bytes:
        return b"" if _VBA_REF.search(match.group(0)) else match.group(0)

    return pattern.sub(keep, data)


def _clean_part(name: str, data: bytes) -> bytes:
    lower = name.lower()
    if lower.endswith(".rels"):
        return _drop_vba_elements(_RELATIONSHIP, data)
    if lower == "[content_types].xml":
        return _drop_vba_elements(_OVERRIDE, data)
    return data


def strip_macros(src: Path, dst: Path) -> bool:
    """复制 OOXML 归档并剥离宏部件；返回是否剥离过（False = 未发现宏部件）。

    对所有 ``*.rels`` 成员删除 Type/Target 引用 vbaProject 的 Relationship
    （忽略大小写），并在 ``[Content_Types].xml`` 中删除对应 Override，
    同时删除任意目录下的 ``vbaProject*.bin``（含签名部件）。因此 DOCX 与
    XLSX 的产物都能被 python-docx / openpyxl 正常打开。

    归档本身损坏、成员数据缺失/CRC 校验失败、目标写入 IO 失败等 zip/IO 错误
    不会返回 True 或 False，而是抛出 ``ingest.errors.Quarantined``（HTTP 423），
    调用方（API 或 Task 13 解析流水线）据此隔离文件；失败时不会留下半成品 dst。
    """
    stripped = False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
                dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                name = info.filename
                if _is_macro_part(name):
                    stripped = True
                    continue
                data = _clean_part(name, zin.read(info))
                zout.writestr(info, data)
    except _STRIP_ERRORS as exc:
        with contextlib.suppress(OSError):
            dst.unlink(missing_ok=True)
        raise Quarantined(f"宏剥离失败: {exc}") from exc
    return stripped
