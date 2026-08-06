"""Static local tool catalog, license-scope review, and hardware compatibility."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .models import Diagnostic

TOKEN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^v[0-9]{3}$")
STATES = {"unreviewed", "reviewing", "approved", "rejected"}
SCOPE_STATES = STATES | {"not-applicable"}
INSTALL_STATES = {"unknown", "uninstalled", "installed"}
OFFLINE_STATES = {"yes", "no", "unknown"}
OPERATING_SYSTEMS = {"windows", "linux", "macos"}
PROFILE_TYPES = {"tool", "model-configuration"}
MODEL_SCOPE_FIELDS = {
    "benchmark_use_review_state",
    "production_model_use_review_state",
    "commercial_output_use_review_state",
}

TOOL_REQUIRED = {
    "kind", "schema_version", "id", "version", "profile_type", "adapter_type",
    "runtime_type", "offline_capability", "deterministic_seed_support",
    "control_capabilities", "minimum_vram_gb", "minimum_ram_gb",
    "supported_operating_systems", "install_state", "evidence_references",
    "license_evidence_state", "commercial_use_review_state", "decision_state",
}
TOOL_ALLOWED = TOOL_REQUIRED | MODEL_SCOPE_FIELDS
HARDWARE_REQUIRED = {
    "kind", "schema_version", "id", "operating_system", "ram_gb", "vram_gb",
    "runtime_types", "adapter_types",
}


@dataclass(frozen=True)
class CompatibilityResult:
    profile_id: str
    status: str
    hard_incompatibilities: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    licensing: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "hard_incompatibilities": list(self.hard_incompatibilities),
            "missing_evidence": list(self.missing_evidence),
            "licensing": dict(sorted(self.licensing.items())),
        }


@dataclass(frozen=True)
class ModelLicenseEligibility:
    profile_id: str
    benchmark_eligible: bool
    production_eligible: bool
    commercial_output_eligible: bool
    denial_reasons: tuple[str, ...]
    states: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "benchmark_eligible": self.benchmark_eligible,
            "production_eligible": self.production_eligible,
            "commercial_output_eligible": self.commercial_output_eligible,
            "denial_reasons": list(self.denial_reasons),
            "states": dict(sorted(self.states.items())),
        }


def _read_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def _diag(path: Path, code: str, message: str, field: str = "") -> Diagnostic:
    return Diagnostic(code, message, str(path), field)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_token_list(path: Path, data: dict[str, Any], field: str, *, allowed: set[str] | None = None) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    value = data.get(field)
    if not isinstance(value, list) or not value:
        return [_diag(path, "INVALID_LIST", "must be a non-empty list", field)]
    for index, item in enumerate(value):
        if not isinstance(item, str) or not TOKEN_RE.fullmatch(item):
            diagnostics.append(_diag(path, "INVALID_TOKEN", "must contain lowercase ASCII tokens", f"{field}[{index}]"))
        elif allowed is not None and item not in allowed:
            diagnostics.append(_diag(path, "INVALID_ENUM", f"unsupported value: {item}", f"{field}[{index}]"))
    if len(value) != len(set(value)):
        diagnostics.append(_diag(path, "DUPLICATE_VALUE", "duplicate values are not allowed", field))
    return diagnostics


def validate_tool_profile(path: Path, data: dict[str, Any] | None = None) -> list[Diagnostic]:
    try:
        document = _read_object(path) if data is None else data
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [_diag(path, "LOAD_ERROR", str(exc))]
    diagnostics: list[Diagnostic] = []
    unknown = sorted(set(document) - TOOL_ALLOWED)
    missing = sorted(TOOL_REQUIRED - set(document))
    if document.get("profile_type") == "model-configuration":
        missing.extend(sorted(MODEL_SCOPE_FIELDS - set(document)))
    for field in sorted(set(missing)):
        diagnostics.append(_diag(path, "MISSING_FIELD", "required field is missing", field))
    if unknown:
        diagnostics.append(_diag(path, "UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}"))
    if document.get("kind") != "tool-profile":
        diagnostics.append(_diag(path, "KIND", "kind must be tool-profile", "kind"))
    if document.get("schema_version") != "1.0":
        diagnostics.append(_diag(path, "SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    if not isinstance(document.get("id"), str) or not TOKEN_RE.fullmatch(document.get("id", "")):
        diagnostics.append(_diag(path, "INVALID_ID", "id must be a lowercase ASCII token", "id"))
    if not isinstance(document.get("version"), str) or not VERSION_RE.fullmatch(document.get("version", "")):
        diagnostics.append(_diag(path, "INVALID_VERSION", "version must use vNNN", "version"))
    if document.get("profile_type") not in PROFILE_TYPES:
        diagnostics.append(_diag(path, "PROFILE_TYPE", "invalid profile_type", "profile_type"))
    for field in ("adapter_type", "runtime_type"):
        if not isinstance(document.get(field), str) or not TOKEN_RE.fullmatch(document.get(field, "")):
            diagnostics.append(_diag(path, "INVALID_TOKEN", "must be a lowercase ASCII token", field))
    if document.get("offline_capability") not in OFFLINE_STATES:
        diagnostics.append(_diag(path, "OFFLINE_CAPABILITY", "invalid offline_capability", "offline_capability"))
    if not isinstance(document.get("deterministic_seed_support"), bool):
        diagnostics.append(_diag(path, "DETERMINISTIC_SEED", "must be boolean", "deterministic_seed_support"))
    diagnostics.extend(_validate_token_list(path, document, "control_capabilities"))
    diagnostics.extend(_validate_token_list(path, document, "supported_operating_systems", allowed=OPERATING_SYSTEMS))
    for field in ("minimum_vram_gb", "minimum_ram_gb"):
        value = document.get(field)
        if not _is_number(value) or value < 0:
            diagnostics.append(_diag(path, "HARDWARE_REQUIREMENT", "must be a non-negative number", field))
    if document.get("install_state") not in INSTALL_STATES:
        diagnostics.append(_diag(path, "INSTALL_STATE", "invalid install_state", "install_state"))
    for field in ("license_evidence_state", "commercial_use_review_state", "decision_state"):
        if document.get(field) not in STATES:
            diagnostics.append(_diag(path, "REVIEW_STATE", "invalid review state", field))
    for field in MODEL_SCOPE_FIELDS & set(document):
        if document.get(field) not in SCOPE_STATES:
            diagnostics.append(_diag(path, "LICENSE_SCOPE_STATE", "invalid license-scope review state", field))
    evidence = document.get("evidence_references")
    if not isinstance(evidence, list):
        diagnostics.append(_diag(path, "EVIDENCE", "evidence_references must be a list", "evidence_references"))
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                diagnostics.append(_diag(path, "EVIDENCE", "evidence entry must be an object", f"evidence_references[{index}]"))
                continue
            allowed = {"source_url", "retrieved_at", "claim"}
            if set(item) != allowed:
                diagnostics.append(_diag(path, "EVIDENCE", "evidence entry requires source_url, retrieved_at, and claim only", f"evidence_references[{index}]"))
            if not isinstance(item.get("source_url"), str) or not item.get("source_url", "").startswith("https://"):
                diagnostics.append(_diag(path, "EVIDENCE_URL", "source_url must use https", f"evidence_references[{index}].source_url"))
            if not isinstance(item.get("retrieved_at"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.get("retrieved_at", "")):
                diagnostics.append(_diag(path, "EVIDENCE_DATE", "retrieved_at must use YYYY-MM-DD", f"evidence_references[{index}].retrieved_at"))
            if not isinstance(item.get("claim"), str) or not item.get("claim", "").strip():
                diagnostics.append(_diag(path, "EVIDENCE_CLAIM", "claim must be non-empty", f"evidence_references[{index}].claim"))
    if document.get("decision_state") == "approved":
        if document.get("license_evidence_state") != "approved":
            diagnostics.append(_diag(path, "APPROVAL_WITHOUT_LICENSE", "approved decision requires approved license evidence", "decision_state"))
        if document.get("commercial_use_review_state") != "approved":
            diagnostics.append(_diag(path, "APPROVAL_WITHOUT_COMMERCIAL_REVIEW", "approved decision requires approved commercial-use review", "decision_state"))
        if not evidence:
            diagnostics.append(_diag(path, "APPROVAL_WITHOUT_EVIDENCE", "approved decision requires evidence", "decision_state"))
        if document.get("profile_type") == "model-configuration":
            if document.get("benchmark_use_review_state") != "approved":
                diagnostics.append(_diag(path, "APPROVAL_WITHOUT_BENCHMARK_USE", "approved model profile requires approved benchmark use", "benchmark_use_review_state"))
            if document.get("commercial_output_use_review_state") not in {"approved", "not-applicable"}:
                diagnostics.append(_diag(path, "APPROVAL_WITHOUT_OUTPUT_REVIEW", "approved model profile requires approved or not-applicable output-use review", "commercial_output_use_review_state"))
            if document.get("production_model_use_review_state") not in {"approved", "rejected", "not-applicable"}:
                diagnostics.append(_diag(path, "APPROVAL_WITHOUT_PRODUCTION_REVIEW", "approved model profile requires a completed production-use review", "production_model_use_review_state"))
    return diagnostics


def validate_hardware_profile(path: Path, data: dict[str, Any] | None = None) -> list[Diagnostic]:
    try:
        document = _read_object(path) if data is None else data
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [_diag(path, "LOAD_ERROR", str(exc))]
    diagnostics: list[Diagnostic] = []
    unknown = sorted(set(document) - HARDWARE_REQUIRED)
    missing = sorted(HARDWARE_REQUIRED - set(document))
    for field in missing:
        diagnostics.append(_diag(path, "MISSING_FIELD", "required field is missing", field))
    if unknown:
        diagnostics.append(_diag(path, "UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}"))
    if document.get("kind") != "hardware-profile":
        diagnostics.append(_diag(path, "KIND", "kind must be hardware-profile", "kind"))
    if document.get("schema_version") != "1.0":
        diagnostics.append(_diag(path, "SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    if not isinstance(document.get("id"), str) or not TOKEN_RE.fullmatch(document.get("id", "")):
        diagnostics.append(_diag(path, "INVALID_ID", "id must be a lowercase ASCII token", "id"))
    if document.get("operating_system") not in OPERATING_SYSTEMS:
        diagnostics.append(_diag(path, "OPERATING_SYSTEM", "unsupported operating system", "operating_system"))
    for field in ("ram_gb", "vram_gb"):
        value = document.get(field)
        if not _is_number(value) or value < 0:
            diagnostics.append(_diag(path, "HARDWARE_VALUE", "must be a non-negative number", field))
    diagnostics.extend(_validate_token_list(path, document, "runtime_types"))
    diagnostics.extend(_validate_token_list(path, document, "adapter_types"))
    return diagnostics


def load_catalog(path: Path) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    paths = [path] if path.is_file() else sorted(path.rglob("*.json"))
    profiles: list[dict[str, Any]] = []
    diagnostics: list[Diagnostic] = []
    if not paths:
        return [], [_diag(path, "NO_PROFILES", "no JSON profiles found")]
    for item in paths:
        try:
            document = _read_object(item)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            diagnostics.append(_diag(item, "LOAD_ERROR", str(exc)))
            continue
        if document.get("kind") != "tool-profile":
            diagnostics.append(_diag(item, "KIND", "catalog entries must be tool-profile documents", "kind"))
            continue
        diagnostics.extend(validate_tool_profile(item, document))
        profiles.append(document)
    profiles.sort(key=lambda item: (str(item.get("id", "")), str(item.get("version", ""))))
    return profiles, diagnostics


def catalog_listing(profiles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "id", "version", "profile_type", "adapter_type", "runtime_type",
        "offline_capability", "install_state", "license_evidence_state",
        "commercial_use_review_state", "decision_state",
        "benchmark_use_review_state", "production_model_use_review_state",
        "commercial_output_use_review_state",
    )
    return [{field: profile.get(field) for field in fields} for profile in sorted(profiles, key=lambda item: (str(item.get("id", "")), str(item.get("version", ""))))]


def evaluate_model_license_eligibility(profile: dict[str, Any]) -> ModelLicenseEligibility:
    states = {
        "license_evidence_state": str(profile.get("license_evidence_state", "")),
        "commercial_use_review_state": str(profile.get("commercial_use_review_state", "")),
        "decision_state": str(profile.get("decision_state", "")),
        "benchmark_use_review_state": str(profile.get("benchmark_use_review_state", "")),
        "production_model_use_review_state": str(profile.get("production_model_use_review_state", "")),
        "commercial_output_use_review_state": str(profile.get("commercial_output_use_review_state", "")),
    }
    reasons: list[str] = []
    if profile.get("profile_type") != "model-configuration":
        reasons.append("not-model-configuration")
    if profile.get("license_evidence_state") != "approved":
        reasons.append("license-evidence-not-approved")
    if profile.get("commercial_use_review_state") != "approved":
        reasons.append("commercial-review-not-approved")
    if profile.get("decision_state") != "approved":
        reasons.append("profile-decision-not-approved")
    if profile.get("benchmark_use_review_state") != "approved":
        reasons.append("benchmark-use-not-approved")
    output_state = profile.get("commercial_output_use_review_state")
    if output_state not in {"approved", "not-applicable"}:
        reasons.append("commercial-output-use-not-approved")
    benchmark_eligible = not reasons
    production_eligible = benchmark_eligible and profile.get("production_model_use_review_state") == "approved"
    commercial_output_eligible = (
        profile.get("license_evidence_state") == "approved"
        and output_state == "approved"
    )
    if benchmark_eligible and not production_eligible:
        reasons.append("production-model-use-not-approved")
    return ModelLicenseEligibility(
        profile_id=str(profile.get("id", "")),
        benchmark_eligible=benchmark_eligible,
        production_eligible=production_eligible,
        commercial_output_eligible=commercial_output_eligible,
        denial_reasons=tuple(sorted(set(reasons))),
        states=states,
    )


def evaluate_compatibility(profile: dict[str, Any], hardware: dict[str, Any]) -> CompatibilityResult:
    hard: list[str] = []
    missing: list[str] = []
    if hardware.get("operating_system") not in profile.get("supported_operating_systems", []):
        hard.append("operating-system")
    if _is_number(profile.get("minimum_ram_gb")) and _is_number(hardware.get("ram_gb")):
        if hardware["ram_gb"] < profile["minimum_ram_gb"]:
            hard.append("ram")
    else:
        missing.append("ram-requirement")
    if _is_number(profile.get("minimum_vram_gb")) and _is_number(hardware.get("vram_gb")):
        if hardware["vram_gb"] < profile["minimum_vram_gb"]:
            hard.append("vram")
    else:
        missing.append("vram-requirement")
    if profile.get("runtime_type") not in hardware.get("runtime_types", []):
        hard.append("runtime")
    if profile.get("adapter_type") not in hardware.get("adapter_types", []):
        hard.append("adapter")
    if profile.get("offline_capability") == "unknown":
        missing.append("offline-capability")
    elif profile.get("offline_capability") == "no":
        hard.append("offline-capability")
    if not profile.get("evidence_references"):
        missing.append("evidence")
    for field in ("license_evidence_state", "commercial_use_review_state"):
        if profile.get(field) in {"unreviewed", "reviewing"}:
            missing.append(field.replace("_", "-"))
        elif profile.get(field) == "rejected":
            missing.append(field.replace("_", "-") + "-rejected")
    if profile.get("profile_type") == "model-configuration":
        eligibility = evaluate_model_license_eligibility(profile)
        if not eligibility.benchmark_eligible:
            missing.extend(eligibility.denial_reasons)
    status = "hard-incompatible" if hard else "missing-evidence" if missing else "compatible-by-declaration"
    licensing = {
        "license_evidence_state": str(profile.get("license_evidence_state", "")),
        "commercial_use_review_state": str(profile.get("commercial_use_review_state", "")),
        "compatibility_implies_license_approval": "false",
    }
    for field in sorted(MODEL_SCOPE_FIELDS):
        if field in profile:
            licensing[field] = str(profile.get(field, ""))
    return CompatibilityResult(
        profile_id=str(profile.get("id", "")),
        status=status,
        hard_incompatibilities=tuple(sorted(set(hard))),
        missing_evidence=tuple(sorted(set(missing))),
        licensing=licensing,
    )
