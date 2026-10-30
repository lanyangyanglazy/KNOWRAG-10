"""Markdown 语料解析：front matter 元数据 + ATX 标题层级 + 段落 → DocModel。

仅供受信任的导入 CLI 解析内置 ``data/gov_docs`` 语料（设计 §14.1），因此
``.md`` 不进入 ``sniff.EXT_KIND``，上传 API 仍会拒绝该扩展名。front matter
里的 ``classification`` 不在这里生效——解析器永不自行定密级，由导入 CLI
读取后写入 ``DocMeta.classification``。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ingest.config import Settings, get_settings
from ingest.ir import Block, DocMeta, DocModel, ParserInfo, Warning
from ingest.sniff import sha256_file

# 起始行必须是独占一行的 ``---``；行尾只容忍空格/制表符，避免把正文里的
# 分隔线或带内容的行误当分隔符。
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def read_front_matter(path: Path) -> tuple[dict, str]:
    """返回 (front matter 字典, 去掉 front matter 后的正文)。

    缺少 front matter、YAML 无法解析或顶层不是键值对时抛 ``ValueError``，
    由导入 CLI 统一转换报错。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ValueError(f"{path.name} 缺少 front matter")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} front matter 解析失败: {exc}") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name} front matter 必须是键值对")
    return meta, text[match.end():]


class MarkdownParser:
    name = "markdown"
    version = "0.1.0"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def supports(self, sniff) -> bool:
        # 协议完整性：sniff 目前不会产出 markdown（.md 仅走 CLI 导入）。
        return sniff.kind == "markdown"

    def parse(self, path: Path) -> DocModel:
        path = Path(path)
        meta, body = read_front_matter(path)
        blocks: list[Block] = []
        path_stack: list[str] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            # 段落内的软换行原样保留为 \n，不做合并。
            text = "\n".join(buffer).strip()
            buffer.clear()
            if text:
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="paragraph",
                    text=text, path=list(path_stack)))

        for line in body.splitlines():
            heading = _HEADING.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                text = heading.group(2).strip()
                path_stack = path_stack[: level - 1] + [text]
                blocks.append(Block(
                    block_id=f"b{len(blocks) + 1}", type="heading",
                    level=level, text=text, path=list(path_stack)))
            elif not line.strip():
                flush()
            else:
                buffer.append(line)
        flush()

        warnings = ([] if blocks
                    else [Warning(code="empty_document", detail="空文档")])
        effective = meta.get("effective_date")
        return DocModel(
            parser=ParserInfo(name=self.name, version=self.version),
            meta=DocMeta(
                title=str(meta.get("title") or path.stem),
                effective_date=str(effective) if effective else None,
                source_filename=path.name,
                sha256=sha256_file(path),
            ),
            blocks=blocks,
            warnings=warnings,
        )
