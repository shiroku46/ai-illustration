"""Small domain types shared by the manifest validator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    document: str
    field: str = ""
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Manifest:
    source: Path
    data: dict[str, Any]

    @property
    def kind(self) -> str:
        value = self.data.get("kind")
        return value if isinstance(value, str) else ""

    @property
    def manifest_id(self) -> str:
        value = self.data.get("id")
        return value if isinstance(value, str) else ""


def load_manifest(path: Path) -> Manifest:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a JSON object")
    return Manifest(path, data)
