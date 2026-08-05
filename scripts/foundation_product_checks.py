#!/usr/bin/env python3
"""Parse the target-owned default-branch product workflow contract."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

CONFIG_PATH = ".github/foundation-product-checks.json"
SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 16_384
MAX_CHECKS = 20
MAX_NAME_LENGTH = 100
MAX_PATH_LENGTH = 240
RESERVED_CHECK_NAMES = frozenset({"CI", "Unit Tests"})
RESERVED_WORKFLOW_PATHS = frozenset({
    ".github/workflows/ci.yml",
    ".github/workflows/unit-tests.yml",
    ".github/workflows/trusted-checks.yml",
    ".github/workflows/claude-queue.yml",
    ".github/workflows/claude-queue-comment-bridge.yml",
    ".github/workflows/ci-reconcile.yml",
    ".github/workflows/supervisor.yml",
})
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")
_WORKFLOW_RE = re.compile(
    r"^\.github/workflows/(?!.*(?:^|/)\.\.(?:/|$))(?!.*[\\:\x00-\x1f])[^/][^:]*\.(?:yml|yaml)$"
)


class ProductCheckConfigError(ValueError):
    """The product-check configuration is malformed or unsafe."""


@dataclass(frozen=True, order=True)
class ProductCheck:
    name: str
    workflow: str


def _text(value: Any, *, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProductCheckConfigError(f"{label} is invalid")
    if value != value.strip() or _NAME_RE.fullmatch(value) is None:
        raise ProductCheckConfigError(f"{label} is invalid")
    return value


def parse_product_checks(content: bytes | str) -> tuple[ProductCheck, ...]:
    """Return a deterministic bounded tuple or fail closed."""
    if isinstance(content, str):
        raw = content.encode("utf-8")
    elif isinstance(content, bytes):
        raw = content
    else:
        raise ProductCheckConfigError("configuration content type is invalid")
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ProductCheckConfigError("configuration size is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductCheckConfigError("configuration is malformed") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "checks"}:
        raise ProductCheckConfigError("configuration object keys are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProductCheckConfigError("configuration schema version is unsupported")
    checks = value.get("checks")
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ProductCheckConfigError("configuration check list is invalid")

    result: list[ProductCheck] = []
    names: set[str] = set()
    paths: set[str] = set()
    reserved = {item.casefold() for item in RESERVED_CHECK_NAMES}
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"name", "workflow"}:
            raise ProductCheckConfigError("product check entry is invalid")
        name = _text(item.get("name"), label="product check name", limit=MAX_NAME_LENGTH)
        workflow = _text(
            item.get("workflow"), label="product workflow path", limit=MAX_PATH_LENGTH
        )
        if name.casefold() in reserved:
            raise ProductCheckConfigError("product check name is reserved")
        if (
            _WORKFLOW_RE.fullmatch(workflow) is None
            or ".." in workflow.split("/")
            or "//" in workflow
            or workflow in RESERVED_WORKFLOW_PATHS
        ):
            raise ProductCheckConfigError("product workflow path is unsafe")
        folded = name.casefold()
        if folded in names or workflow in paths:
            raise ProductCheckConfigError("product checks contain a duplicate identity")
        names.add(folded)
        paths.add(workflow)
        result.append(ProductCheck(name, workflow))
    return tuple(sorted(result))
