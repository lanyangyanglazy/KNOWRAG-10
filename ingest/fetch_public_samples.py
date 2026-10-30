"""下载政府公开发布的文件样本，供本地解析 / 切片 / 检索效果测试。

范围与边界：

- 只收录**政府主动公开发布**的政策文件与政务公开附件（gov.cn 及地方政务公开网），
  不涉及个人信息、不绕过登录/验证码、不做批量抓取。
- 单个文件 ≤ 50 MB，总量控制在几十 MB；下载后记录 sha256 与来源 URL，便于复核。
- 这些文件只用于解析质量与检索效果测试，不进入产品语料库，也不对外分发。

用法：

    uv run python -m ingest.fetch_public_samples
    uv run python -m ingest.fetch_public_samples --out data/public_samples --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUT = BASE_DIR / "data" / "public_samples"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class Sample:
    name: str
    url: str
    category: str  # pdf / docx / xlsx / unsupported
    note: str
    referer: str = ""
    expect_supported: bool = True


# 说明：URL 均为页面上的公开附件直链；如遇到站点改版，更新本清单即可。
SAMPLES: tuple[Sample, ...] = (
    # ---- 文本层 PDF（政策文件，长短不同，用于测解析与切片粒度） ----
    Sample(
        "gov_market_access_2025.pdf",
        "https://www.gov.cn/zhengce/zhengceku/202504/P020250424380902075818.pdf",
        "pdf", "《市场准入负面清单（2025年版）》——长文档、含大量表格",
    ),
    Sample(
        "gov_hrss_notice_2025.pdf",
        "https://www.gov.cn/zhengce/zhengceku/202506/P020250613787291509463.pdf",
        "pdf", "人力资源社会保障部办公厅通知——中等长度公文",
    ),
    Sample(
        "gov_energy_saving_2025.pdf",
        "https://www.gov.cn/zhengce/zhengceku/202510/P020251014419322049864.pdf",
        "pdf", "《节能降碳中央预算内投资专项管理办法》——条款型文本",
    ),
    Sample(
        "local_anqing_list_2025.pdf",
        "https://www.anqing.gov.cn/group1/M00/01/35/FBUWEWjRAWuAOnIOAAyuZHcd1fU532.pdf",
        "pdf", "安庆市赋权清单——地方政务公开 PDF",
    ),
    # ---- DOCX（地方政务公开附件） ----
    Sample(
        "naidong_regulation_1.docx",
        "http://www.naidong.gov.cn/zwgk/xxgkml/202511/P020251125459575608801.docx",
        "docx", "乃东区行政规范性文件文本 1",
    ),
    Sample(
        "naidong_regulation_2.docx",
        "http://www.naidong.gov.cn/zwgk/xxgkml/202511/P020251125459575706860.docx",
        "docx", "乃东区行政规范性文件文本 2",
    ),
    Sample(
        "naidong_regulation_3.docx",
        "http://www.naidong.gov.cn/zwgk/xxgkml/202511/P020251125459575931256.docx",
        "docx", "乃东区行政规范性文件文本 3",
    ),
    # ---- XLSX（政务公开表格） ----
    Sample(
        "haidian_penalty_2026.xlsx",
        "https://www.bjhd.gov.cn/zfxxgk/auto4541_51837/auto4541_56335/auto4541_56365/auto4541_56369/202605/P020260709566252184828.xlsx",
        "xlsx", "海淀区行政处罚公示表——报表型表格",
    ),
    # ---- 暂不支持的格式：用于验证拒识路径（不应静默入库） ----
    Sample(
        "gov_market_access_2025.ofd",
        "https://www.gov.cn/zhengce/zhengceku/202504/P020250424380902340702.ofd",
        "unsupported", "OFD 版式文件——当前白名单外，应被拒识",
        expect_supported=False,
    ),
    Sample(
        "naidong_list.et",
        "http://www.naidong.gov.cn/zwgk/xxgkml/202511/P020251125459575474712.et",
        "unsupported", "WPS 表格 .et——当前白名单外，应被拒识",
        expect_supported=False,
    ),
    Sample(
        "naidong_regulation.wps",
        "http://www.naidong.gov.cn/zwgk/xxgkml/202511/P020251125459575816257.wps",
        "unsupported", "WPS 文字 .wps——当前白名单外，应被拒识",
        expect_supported=False,
    ),
)


def _download(sample: Sample, target: Path, timeout: int = 60) -> dict:
    headers = {"User-Agent": USER_AGENT}
    if sample.referer:
        headers["Referer"] = sample.referer
    request = urllib.request.Request(sample.url, headers=headers)
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        with open(tmp, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)
    tmp.replace(target)

    data = target.read_bytes()
    return {
        **asdict(sample),
        "file": target.name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": content_type,
        "public": True,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

    parser = argparse.ArgumentParser(description="下载政府公开文件样本（仅测试用）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    failed = 0
    for sample in SAMPLES:
        target = out_dir / sample.name
        if target.exists() and not args.force:
            data = target.read_bytes()
            record = {
                **asdict(sample), "file": target.name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_type": "(cached)", "public": True, "cached": True,
            }
            manifest.append(record)
            print(f"= {sample.name}（已存在，跳过）")
            continue
        try:
            record = _download(sample, target, timeout=args.timeout)
            manifest.append(record)
            print(f"+ {sample.name}  {record['size_bytes'] / 1024:.1f} KB  {sample.note}")
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断整批
            failed += 1
            print(f"! {sample.name} 下载失败：{type(exc).__name__}: {exc}")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(manifest)} 个文件，失败 {failed} 个；清单：{manifest_path}")
    print("说明：以上均为政府公开文件，仅用于本地解析与检索测试。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
