from __future__ import annotations

import shutil
from pathlib import Path

from ingest.errors import Quarantined

BASE_DIR = Path(__file__).resolve().parents[1]


class FileStore:
    """内容寻址本地存储：uploads/{sha[:2]}/{sha}。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put_file(self, src: Path, sha256: str) -> str:
        rel = f"{sha256[:2]}/{sha256}"
        dest = self.resolve(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
        return rel

    def resolve(self, rel: str) -> Path:
        candidate = (self.root / rel).resolve()
        root = self.root.resolve()
        if root not in candidate.parents and candidate != root:
            raise Quarantined(f"非法存储路径: {rel}")
        return candidate

    def delete(self, rel: str) -> None:
        path = self.resolve(rel)
        if path.exists():
            path.unlink()
