"""命名切片配置（profile）：把切分参数从代码常量变成可比较、可留痕的配置。

为什么需要 profile：

- 切分的"最优"是**任务相关**的，不是文本的内在属性。同一份语料，问长文归纳和
  问表格数值，合适的粒度不同，只能靠评测选出默认值。
- 换参数必须能**重跑并对比**。切片一旦落库，参数就成了数据的一部分；不记录
  是哪个 profile 产生的，就无法解释"为什么这次召回变差"。

约定：

- ``profile_id`` 会写进每个切片的 metadata，并进入索引指纹；换 profile 必然
  触发重建，不会出现"改了参数但索引还是旧的"。
- 数值环境变量（``INGEST_CHUNK_SIZE`` 等）仍然可以覆盖 profile 默认值，
  此时 ``profile_from_settings()`` 会在 id 后加 ``+env`` 后缀，保证留痕诚实。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

DEFAULT_PROFILE_ID = "p400"


@dataclass(frozen=True)
class ChunkProfile:
    """一组可复现的切片参数。

    字段含义与 ``docs/文档摄取子系统设计.md`` §9.1/§9.2 对齐：

    - ``chunk_size`` 是**文本切片**的上限（含标题前缀，前缀最多 80 字符）；
    - 表格行窗口不受 ``chunk_size`` 约束，由 ``table_rows_per_chunk`` 控制
      （每行重复列名，单元格截断 200 字），因此表格切片可能长于 ``chunk_size``。
    """

    profile_id: str
    chunk_size: int = 400
    chunk_overlap: int = 60
    table_rows_per_chunk: int = 10
    max_rows_per_table: int = 5000
    title_prefix: bool = True
    min_chunk_chars: int = 20
    merge_chars: int = 40
    description: str = ""

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id 不能为空")
        if self.chunk_size < 10:
            raise ValueError(f"{self.profile_id}: chunk_size 不能小于 10")
        if self.chunk_overlap < 0:
            raise ValueError(f"{self.profile_id}: chunk_overlap 不能为负")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(f"{self.profile_id}: chunk_overlap 必须小于 chunk_size")
        if self.table_rows_per_chunk < 1:
            raise ValueError(f"{self.profile_id}: table_rows_per_chunk 必须 >= 1")
        if self.max_rows_per_table < self.table_rows_per_chunk:
            raise ValueError(
                f"{self.profile_id}: max_rows_per_table 不能小于 table_rows_per_chunk")


# 注册表只放"值得对比"的几组，不追求大而全。
# legacy300 / p400-noprefix 是消融对照：用来区分"结构感知"和"标题前缀"各自的贡献。
PROFILES: dict[str, ChunkProfile] = {
    "legacy300": ChunkProfile(
        profile_id="legacy300", chunk_size=300, chunk_overlap=50,
        title_prefix=False,
        description="无前缀对照：300/50、不带标题前缀",
    ),
    "p300": ChunkProfile(
        profile_id="p300", chunk_size=300, chunk_overlap=50,
        description="小切片：300/50、带标题前缀",
    ),
    "p400": ChunkProfile(
        profile_id="p400", chunk_size=400, chunk_overlap=60,
        description="默认：400/60、带标题前缀（对齐摄取设计 §9.1）",
    ),
    "p600": ChunkProfile(
        profile_id="p600", chunk_size=600, chunk_overlap=100,
        description="大切片：600/100、带标题前缀，适合长段落公文",
    ),
    "p400-noprefix": ChunkProfile(
        profile_id="p400-noprefix", chunk_size=400, chunk_overlap=60,
        title_prefix=False,
        description="消融对照：400/60、去掉标题前缀",
    ),
}


def resolve_profile(profile: str | ChunkProfile | None = None) -> ChunkProfile:
    """解析 profile：显式对象/名称 > 环境变量 > 默认 p400。

    未知名称直接报错，而不是悄悄回退——部署时写错名字应该立刻失败，
    否则整库会按另一套参数切完才发现。
    """
    if isinstance(profile, ChunkProfile):
        return profile
    name = (profile or os.getenv("RAG_CHUNK_PROFILE") or DEFAULT_PROFILE_ID).strip()
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"未知切片 profile: {name!r}，可选：{', '.join(sorted(PROFILES))}") from None


def profile_from_settings(settings) -> ChunkProfile:
    """把 ``Settings`` 上的数值覆盖合并进 profile。

    ``get_settings()`` 在构造 Settings 时已经用 profile 填过默认值，所以正常
    情况下这里返回的就是原 profile；只有当用户显式设置了 ``INGEST_CHUNK_SIZE``
    之类的环境变量时才会出现差异，此时给 id 加 ``+env`` 后缀留痕。
    """
    base = resolve_profile(getattr(settings, "chunk_profile", None))
    chunk_size = int(settings.chunk_size)
    values = {
        "chunk_size": chunk_size,
        # 历史配置允许 overlap >= size（切分器内部会自行收缩到 limit//4），
        # 这里按同样口径钳制，避免环境变量组合在构造 profile 时直接抛错。
        "chunk_overlap": max(0, min(int(settings.chunk_overlap), chunk_size - 1)),
        "table_rows_per_chunk": int(settings.table_rows_per_chunk),
        "max_rows_per_table": int(settings.max_rows_per_table),
    }
    if all(getattr(base, key) == value for key, value in values.items()):
        return base
    return replace(base, profile_id=f"{base.profile_id}+env", **values)
