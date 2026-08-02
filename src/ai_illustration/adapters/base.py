"""Stable adapter-domain types. Adapters plan work but do not execute it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class AdapterError(ValueError):
    """Raised when an adapter input fails closed validation."""

    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class ExecutionPlan:
    adapter_id: str
    adapter_version: str
    endpoint: str
    workflow_sha256: str
    request_id: str
    output_directory: str
    bindings: Mapping[str, Any]
    payload_summary: Mapping[str, Any]
    dry_run: bool
    executable_ready: bool
    readiness_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "bindings": dict(sorted(self.bindings.items())),
            "dry_run": self.dry_run,
            "endpoint": self.endpoint,
            "executable_ready": self.executable_ready,
            "output_directory": self.output_directory,
            "payload_summary": dict(sorted(self.payload_summary.items())),
            "readiness_reasons": list(self.readiness_reasons),
            "request_id": self.request_id,
            "workflow_sha256": self.workflow_sha256,
        }


class GenerationAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def check_workflow(self, workflow: Mapping[str, Any]) -> dict[str, Any]: ...

    def plan(
        self,
        request: Mapping[str, Any],
        workflow: Mapping[str, Any],
        bindings: Mapping[str, Any],
        *,
        endpoint: str,
    ) -> ExecutionPlan: ...
