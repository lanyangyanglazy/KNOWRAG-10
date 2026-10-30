from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ConversionError(Exception):
    pass


def find_soffice() -> Path | None:
    env = os.getenv("SOFFICE_BIN")
    if env and Path(env).exists():
        return Path(env)
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
        Path("/usr/bin/soffice"),
        Path("/opt/libreoffice/program/soffice"),
    ):
        if candidate.exists():
            return candidate
    return None


def convert_document(src: Path, target_ext: str, outdir: Path | None = None,
                     timeout: int = 120) -> Path:
    soffice = find_soffice()
    if soffice is None:
        raise ConversionError("未找到 LibreOffice，请设置 SOFFICE_BIN 或安装后重试")
    target_ext = target_ext.lstrip(".")
    outdir = Path(outdir or tempfile.mkdtemp(prefix="rag-convert-"))
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{Path(src).stem}.{target_ext}"
    # 上一次失败可能留下同名半成品，先清掉，避免把旧文件误当本次产物
    out.unlink(missing_ok=True)
    try:
        subprocess.run(
            [str(soffice), "--headless", "--convert-to", target_ext,
             "--outdir", str(outdir), str(src)],
            check=True, timeout=timeout, capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ConversionError(f"LibreOffice 转换失败: {exc}") from exc
    if not out.exists():
        raise ConversionError(f"转换未生成文件: {out}")
    return out
