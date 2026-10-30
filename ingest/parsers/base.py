from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ingest.errors import ParseError
from ingest.ir import DocModel
from ingest.sniff import SniffResult

__all__ = ["ParseError", "Parser"]


class Parser(Protocol):
    name: str
    version: str

    def supports(self, sniff: SniffResult) -> bool: ...
    def parse(self, path: Path) -> DocModel: ...
