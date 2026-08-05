#!/usr/bin/env python3
"""Bounded same-repository GitHub API governance and collision preflight.

This module is deliberately transport-agnostic. Callers provide a fixed
same-repository transport; the governor owns budgets, retries, circuit state,
complete pagination, write-effect recovery, and public-safe collision evidence.
It performs no GitHub mutation by itself.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*[\x00-\x1f\\:])[^?\[\]]+$")
SAFE_BRANCH_RE = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]+$")
RETRYABLE = frozenset({
    "primary-rate-limit",
    "secondary-rate-limit",
    "transport",
    "platform-outage",
})


class FailureKind(str, Enum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    PRIMARY_RATE_LIMIT = "primary-rate-limit"
    SECONDARY_RATE_LIMIT = "secondary-rate-limit"
    TRANSPORT = "transport"
    PLATFORM_OUTAGE = "platform-outage"
    MALFORMED_RESPONSE = "malformed-response"
    INCOMPLETE_PAGINATION = "incomplete-pagination"
    REQUEST_BUDGET = "request-budget-exhausted"
    CIRCUIT_OPEN = "circuit-open"
    AMBIGUOUS_WRITE = "ambiguous-write"


class GovernanceError(RuntimeError):
    def __init__(
        self,
        kind: FailureKind,
        message: str,
        *,
        response: Response | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.response = response

    @property
    def retryable(self) -> bool:
        return self.kind.value in RETRYABLE


class TransportFailure(OSError):
    """The fixed transport could not obtain an HTTP response."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    next_path: str | None = None


class Transport(Protocol):
    def request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Response: ...


@dataclass
class RequestBudget:
    limit: int
    used: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 10_000:
            raise ValueError("request budget must be between 1 and 10000")

    def consume(self) -> None:
        if self.used >= self.limit:
            raise GovernanceError(FailureKind.REQUEST_BUDGET, "GitHub request budget is exhausted")
        self.used += 1


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    consecutive_failures: int = 0
    open_until: float | None = None

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.failure_threshold > 20:
            raise ValueError("circuit threshold is out of bounds")
        if not 0 < self.cooldown_seconds <= 3600:
            raise ValueError("circuit cooldown is out of bounds")

    def require_closed(self, now: float) -> None:
        if self.open_until is not None and now < self.open_until:
            raise GovernanceError(FailureKind.CIRCUIT_OPEN, "GitHub API circuit is open")
        if self.open_until is not None and now >= self.open_until:
            self.open_until = None
            self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.open_until = None

    def record_retryable_failure(self, now: float) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.open_until = now + self.cooldown_seconds


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    previous_filename: str | None = None


@dataclass(frozen=True)
class PullReservation:
    number: int
    repository: str
    head_sha: str
    branch: str
    request_identity: str
    files: tuple[ChangedFile, ...]


@dataclass(frozen=True)
class BranchReservation:
    branch: str
    request_identity: str
    candidate_sha: str | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class IntegrationException:
    pull_number: int
    head_sha: str


@dataclass(frozen=True)
class CollisionResult:
    state: str
    blocking_prs: tuple[int, ...]
    blocking_branches: tuple[str, ...]
    blocking_paths: tuple[str, ...]
    reasons: tuple[str, ...]
    human_action_required: bool = False

    def as_public_state(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "blocking_prs": list(self.blocking_prs),
            "blocking_branches": list(self.blocking_branches),
            "blocking_paths": list(self.blocking_paths),
            "reasons": list(self.reasons),
            "human_action_required": self.human_action_required,
        }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def classify_response(response: Response) -> FailureKind | None:
    if not isinstance(response.status, int) or not 100 <= response.status <= 599:
        return FailureKind.MALFORMED_RESPONSE
    if 200 <= response.status < 300:
        return None
    if response.status == 401:
        return FailureKind.AUTHENTICATION
    if response.status == 403:
        remaining = _header(response.headers, "X-RateLimit-Remaining")
        if remaining == "0":
            return FailureKind.PRIMARY_RATE_LIMIT
        if _header(response.headers, "Retry-After") is not None:
            return FailureKind.SECONDARY_RATE_LIMIT
        return FailureKind.PERMISSION
    if response.status == 429:
        return FailureKind.SECONDARY_RATE_LIMIT
    if 500 <= response.status <= 599:
        return FailureKind.PLATFORM_OUTAGE
    return FailureKind.PERMISSION


def _path_parts(path: str) -> tuple[str, str]:
    if not isinstance(path, str) or not path or len(path) > 2048:
        raise ValueError("GitHub API path is invalid")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or path.startswith("/") or "@" in parsed.netloc:
        raise ValueError("absolute or fragmented GitHub API targets are forbidden")
    if any(part in {"", ".", ".."} for part in parsed.path.split("/")):
        raise ValueError("GitHub API path traversal is forbidden")
    if any(ord(character) < 32 or character == "\\" for character in path):
        raise ValueError("GitHub API path contains unsafe characters")
    return parsed.path, parsed.query


def validate_api_path(repository: str, path: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("repository identity is invalid")
    parsed_path, _ = _path_parts(path)
    prefix = f"repos/{repository}/"
    if not parsed_path.startswith(prefix):
        raise ValueError("GitHub API path escaped the fixed repository")
    return path


def _json(response: Response) -> Any:
    if not isinstance(response.body, (bytes, bytearray)) or len(response.body) > 10_000_000:
        raise GovernanceError(FailureKind.MALFORMED_RESPONSE, "GitHub response body is malformed or excessive")
    try:
        return json.loads(bytes(response.body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernanceError(FailureKind.MALFORMED_RESPONSE, "GitHub response is not valid JSON") from exc


class GovernedGitHub:
    """One bounded request pass against one fixed repository."""

    def __init__(
        self,
        repository: str,
        transport: Transport,
        *,
        request_limit: int = 200,
        max_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 30.0,
        jitter: Callable[[float], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        if not REPOSITORY_RE.fullmatch(repository):
            raise ValueError("repository identity is invalid")
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max attempts is out of bounds")
        if not 0 < backoff_base_seconds <= backoff_cap_seconds <= 300:
            raise ValueError("backoff bounds are invalid")
        self.repository = repository
        self.transport = transport
        self.budget = RequestBudget(request_limit)
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.jitter = jitter or (lambda maximum: 0.0)
        self.sleeper = sleeper or (lambda seconds: None)
        self.clock = clock or (lambda: 0.0)
        self.circuit = circuit or CircuitBreaker()
        self.last_failure: FailureKind | None = None

    def _retry_delay(self, response: Response | None, attempt: int) -> float:
        now = self.clock()
        if response is not None:
            retry_after = _header(response.headers, "Retry-After")
            if retry_after is not None:
                try:
                    value = float(retry_after)
                except ValueError:
                    value = -1
                if 0 <= value <= 3600:
                    return value
            reset = _header(response.headers, "X-RateLimit-Reset")
            if reset is not None:
                try:
                    value = float(reset) - now
                except ValueError:
                    value = -1
                if 0 <= value <= 3600:
                    return value
        base = min(self.backoff_cap_seconds, self.backoff_base_seconds * (2 ** max(0, attempt - 1)))
        added = self.jitter(base)
        if not isinstance(added, (int, float)) or isinstance(added, bool) or not 0 <= float(added) <= base:
            raise ValueError("jitter must remain within the supplied bound")
        return min(self.backoff_cap_seconds, base + float(added))

    def _once(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Response:
        if method not in {"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("unsupported GitHub method")
        validate_api_path(self.repository, path)
        now = self.clock()
        self.circuit.require_closed(now)
        self.budget.consume()
        try:
            response = self.transport.request(method, path, payload)
        except (TransportFailure, OSError) as exc:
            self.last_failure = FailureKind.TRANSPORT
            self.circuit.record_retryable_failure(now)
            raise GovernanceError(FailureKind.TRANSPORT, "GitHub transport failed") from exc
        if not isinstance(response, Response):
            self.last_failure = FailureKind.MALFORMED_RESPONSE
            raise GovernanceError(FailureKind.MALFORMED_RESPONSE, "GitHub transport returned an invalid response")
        failure = classify_response(response)
        if failure is None:
            self.last_failure = None
            self.circuit.record_success()
            return response
        self.last_failure = failure
        error = GovernanceError(
            failure,
            f"GitHub request failed as {failure.value}",
            response=response,
        )
        if error.retryable:
            self.circuit.record_retryable_failure(now)
        raise error

    def read_page(self, path: str) -> Response:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._once("GET", path)
            except GovernanceError as exc:
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                self.sleeper(self._retry_delay(exc.response, attempt))
        raise AssertionError("bounded read attempts were exhausted unexpectedly")

    def read_json(self, path: str) -> Any:
        return _json(self.read_page(path))

    def read_all(
        self,
        path: str,
        *,
        item_key: str | None = None,
        max_pages: int = 20,
        max_items: int = 5000,
    ) -> list[Any]:
        if not 1 <= max_pages <= 100 or not 1 <= max_items <= 100_000:
            raise ValueError("pagination bounds are invalid")
        current = validate_api_path(self.repository, path)
        values: list[Any] = []
        for page_number in range(1, max_pages + 1):
            response = self.read_page(current)
            payload = _json(response)
            if item_key is None:
                items = payload
            elif isinstance(payload, dict):
                items = payload.get(item_key)
            else:
                items = None
            if not isinstance(items, list):
                raise GovernanceError(FailureKind.MALFORMED_RESPONSE, "paginated GitHub response is not a list")
            values.extend(items)
            if len(values) > max_items:
                raise GovernanceError(FailureKind.INCOMPLETE_PAGINATION, "GitHub pagination exceeded the item bound")
            if response.next_path is None:
                return values
            current = validate_api_path(self.repository, response.next_path)
            if page_number == max_pages:
                raise GovernanceError(FailureKind.INCOMPLETE_PAGINATION, "GitHub pagination remained incomplete at the page bound")
        raise GovernanceError(FailureKind.INCOMPLETE_PAGINATION, "GitHub pagination did not terminate")

    def write_once_with_probe(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        effect_probe: Callable[[], bool],
    ) -> str:
        """Perform at most one non-idempotent write and prove its connected effect."""
        if method not in {"POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("write method is invalid")
        if effect_probe():
            return "already-present"
        try:
            self._once(method, path, payload)
        except GovernanceError as exc:
            if effect_probe():
                return "recovered-after-uncertain-write"
            if exc.retryable:
                raise GovernanceError(FailureKind.AMBIGUOUS_WRITE, "write outcome is not proven; blind replay is forbidden") from exc
            raise
        if not effect_probe():
            raise GovernanceError(FailureKind.AMBIGUOUS_WRITE, "write response succeeded but the intended effect is unproven")
        return "created"

    def public_state(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "api_state": "circuit-open" if self.circuit.open_until is not None else "normal",
            "request_budget": self.budget.limit,
            "requests_used": self.budget.used,
            "consecutive_retryable_failures": self.circuit.consecutive_failures,
            "circuit_reopen_at": self.circuit.open_until,
            "last_failure": self.last_failure.value if self.last_failure else None,
            "human_action_required": False,
        }


def _repository_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 500 or not SAFE_PATH_RE.fullmatch(path):
        raise ValueError("repository path is invalid")
    return path


def _branch(branch: str) -> str:
    if not isinstance(branch, str) or not branch or len(branch) > 255 or not SAFE_BRANCH_RE.fullmatch(branch):
        raise ValueError("branch identity is invalid")
    return branch


def protected_family(path: str) -> str | None:
    if path.startswith(".github/workflows/"):
        return ".github/workflows/**"
    if path.startswith("bootstrap/"):
        return "bootstrap/**"
    if path.startswith("scripts/github_") or path.startswith("scripts/supervisor_"):
        return "scripts/control-plane/**"
    if path in {
        "AGENTS.md",
        "CLAUDE.md",
        "SECURITY.md",
        "docs/OPERATING_RULES.md",
        "docs/PROJECT_STARTUP.md",
        "scripts/validate_repository.py",
    }:
        return "foundation-policy"
    return None


def _occupied(files: Sequence[ChangedFile]) -> set[str]:
    values: set[str] = set()
    for item in files:
        values.add(_repository_path(item.filename))
        if item.previous_filename is not None:
            values.add(_repository_path(item.previous_filename))
    return values


def discover_collisions(
    repository: str,
    proposed_paths: Sequence[str],
    *,
    request_identity: str,
    intended_branch: str,
    open_pulls: Sequence[PullReservation],
    active_branches: Sequence[BranchReservation] = (),
    integration: IntegrationException | None = None,
) -> CollisionResult:
    """Return deterministic read-only collision evidence before a mutation."""
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("repository identity is invalid")
    if not isinstance(request_identity, str) or not request_identity or len(request_identity) > 256:
        raise ValueError("request identity is invalid")
    branch = _branch(intended_branch)
    proposed = {_repository_path(path) for path in proposed_paths}
    if not proposed:
        raise ValueError("proposed path set is empty")
    proposed_families = {family for path in proposed if (family := protected_family(path)) is not None}
    blocking_prs: set[int] = set()
    blocking_branches: set[str] = set()
    blocking_paths: set[str] = set()
    reasons: set[str] = set()

    for pull in open_pulls:
        if pull.repository != repository:
            continue
        if pull.number <= 0 or not SHA_RE.fullmatch(pull.head_sha):
            raise ValueError("Pull Request reservation identity is invalid")
        _branch(pull.branch)
        if integration and pull.number == integration.pull_number and pull.head_sha == integration.head_sha:
            continue
        occupied = _occupied(pull.files)
        exact = proposed & occupied
        occupied_families = {family for path in occupied if (family := protected_family(path)) is not None}
        family_overlap = proposed_families & occupied_families
        duplicate_request = pull.request_identity == request_identity
        duplicate_branch = pull.branch == branch
        if exact or family_overlap or duplicate_request or duplicate_branch:
            blocking_prs.add(pull.number)
            if exact:
                blocking_paths.update(exact)
                reasons.add("path-overlap")
            if family_overlap:
                blocking_paths.update(path for path in proposed if protected_family(path) in family_overlap)
                blocking_paths.update(path for path in occupied if protected_family(path) in family_overlap)
                reasons.add("protected-family-overlap")
            if duplicate_request:
                reasons.add("duplicate-request-identity")
            if duplicate_branch:
                blocking_branches.add(pull.branch)
                reasons.add("duplicate-branch-identity")

    for reservation in active_branches:
        reserved_branch = _branch(reservation.branch)
        reserved_paths = {_repository_path(path) for path in reservation.paths}
        reserved_families = {family for path in reserved_paths if (family := protected_family(path)) is not None}
        exact = proposed & reserved_paths
        family_overlap = proposed_families & reserved_families
        duplicate_request = reservation.request_identity == request_identity
        duplicate_branch = reserved_branch == branch
        if exact or family_overlap or duplicate_request or duplicate_branch:
            blocking_branches.add(reserved_branch)
            if exact:
                blocking_paths.update(exact)
                reasons.add("path-overlap")
            if family_overlap:
                blocking_paths.update(path for path in proposed if protected_family(path) in family_overlap)
                blocking_paths.update(path for path in reserved_paths if protected_family(path) in family_overlap)
                reasons.add("protected-family-overlap")
            if duplicate_request:
                reasons.add("duplicate-request-identity")
            if duplicate_branch:
                reasons.add("duplicate-branch-identity")

    blocked = bool(blocking_prs or blocking_branches or blocking_paths or reasons)
    return CollisionResult(
        "blocked" if blocked else "clear",
        tuple(sorted(blocking_prs)),
        tuple(sorted(blocking_branches)),
        tuple(sorted(blocking_paths)),
        tuple(sorted(reasons)),
        False,
    )
