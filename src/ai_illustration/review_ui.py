"""Loopback-only, read-only candidate comparison application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import Manifest, load_manifest
from .validation import _parse_png, validate_document

REVIEW_CATEGORIES = (
    "line_uniformity", "contour_overclean", "excessive_symmetry", "generic_eyes",
    "hand_defect", "repeated_face_template", "gradient_overuse", "mechanical_anatomy",
    "identity_drift", "other",
)
DECISIONS = {"shortlist", "accept", "reject", "needs_revision"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class ReviewUIError(ValueError):
    """A fail-closed review UI configuration or input error."""


@dataclass(frozen=True)
class CandidateView:
    payload: dict[str, Any]
    asset_root: Path
    asset_spec: dict[str, Any]

    def read_verified_asset(self) -> bytes | None:
        return _verified_asset_bytes(self.asset_root, self.asset_spec)


@dataclass(frozen=True)
class ReviewData:
    candidates: tuple[CandidateView, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "categories": list(REVIEW_CATEGORIES),
            "decisions": sorted(DECISIONS),
            "candidates": [item.payload for item in self.candidates],
        }

    def by_id(self, candidate_id: str) -> CandidateView | None:
        return next((item for item in self.candidates if item.payload["id"] == candidate_id), None)


def _static_root() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "review"


def resolve_beneath(root: Path, relative: str, *, require_file: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise ReviewUIError("path must be a non-empty POSIX relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReviewUIError("absolute paths and traversal are rejected")
    root_resolved = root.resolve(strict=True)
    candidate = root_resolved.joinpath(*pure.parts).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ReviewUIError("path escapes the authorized root") from exc
    if require_file and not candidate.is_file():
        raise ReviewUIError("authorized file does not exist")
    return candidate


def _read_manifests(root: Path) -> list[Manifest]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ReviewUIError("manifest root must be a directory")
    manifests: list[Manifest] = []
    diagnostics: list[str] = []
    for path in sorted(root.rglob("*.json")):
        try:
            if path.is_symlink():
                raise ReviewUIError("symlinked manifest files are rejected")
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise ReviewUIError("manifest path is not a regular file")
            manifest = load_manifest(resolved)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ReviewUIError) as exc:
            diagnostics.append(f"{path}: {exc}")
            continue
        if manifest.kind not in {"character-spec", "generation-request", "candidate-asset", "review-decision"}:
            continue
        errors = validate_document(manifest)
        if errors:
            diagnostics.extend(f"{item.document}:{item.field}:{item.code}" for item in errors)
        else:
            manifests.append(manifest)
    if diagnostics:
        raise ReviewUIError("invalid review manifests: " + "; ".join(diagnostics))
    return manifests


def _verified_asset_bytes(asset_root: Path, candidate: dict[str, Any]) -> bytes | None:
    try:
        path = resolve_beneath(asset_root, candidate["path"], require_file=True)
        payload = path.read_bytes()
        if path.suffix.lower() != ".png" or hashlib.sha256(payload).hexdigest() != candidate["sha256"]:
            return None
        info = _parse_png(payload)
        if info.width != candidate["width"] or info.height != candidate["height"] or not info.has_alpha or not info.has_srgb:
            return None
        return payload
    except (KeyError, OSError, ReviewUIError, ValueError):
        return None


def _validated_prior_reviews(
    candidate_id: str,
    candidate: dict[str, Any],
    request_id: str,
    reviews: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    prior = reviews.get(candidate_id, [])
    for review in prior:
        if review.get("candidate_request_ref") != request_id:
            raise ReviewUIError(f"review {review.get('id', '')} does not bind the current source request")
        if review.get("candidate_sha256") != candidate.get("sha256"):
            raise ReviewUIError(f"review {review.get('id', '')} does not bind the current candidate checksum")
        if review.get("decision") in {"accept", "shortlist"} and candidate.get("status") != "technically_valid":
            raise ReviewUIError(f"review {review.get('id', '')} approves a candidate that is not technically valid")
    return sorted(prior, key=lambda item: (str(item.get("timestamp", "")), str(item.get("id", ""))))


def load_review_data(manifest_root: Path, asset_root: Path) -> ReviewData:
    manifests = _read_manifests(manifest_root)
    asset_root = asset_root.resolve(strict=True)
    if not asset_root.is_dir():
        raise ReviewUIError("asset root must be a directory")
    indexes: dict[str, dict[str, Manifest]] = {
        kind: {} for kind in ("character-spec", "generation-request", "candidate-asset")
    }
    reviews: dict[str, list[dict[str, Any]]] = {}
    for manifest in manifests:
        if manifest.kind == "review-decision":
            reviews.setdefault(str(manifest.data["candidate_ref"]), []).append(manifest.data)
            continue
        if manifest.manifest_id in indexes[manifest.kind]:
            raise ReviewUIError(f"duplicate {manifest.kind} id: {manifest.manifest_id}")
        indexes[manifest.kind][manifest.manifest_id] = manifest

    candidates: list[CandidateView] = []
    for candidate_id, manifest in indexes["candidate-asset"].items():
        candidate = manifest.data
        request_id = str(candidate.get("request_ref", ""))
        request = indexes["generation-request"].get(request_id)
        if request is None:
            raise ReviewUIError(f"candidate {candidate_id} has no source request")
        character_ref = str(request.data.get("character_ref", ""))
        ref_parts = character_ref.rsplit("@", 1)
        if len(ref_parts) != 2 or not all(ref_parts):
            raise ReviewUIError(f"request {request.manifest_id} has an invalid character reference")
        character_id, character_version = ref_parts
        character = indexes["character-spec"].get(character_id)
        if character is None:
            raise ReviewUIError(f"request {request.manifest_id} has no character specification")
        if character.data.get("version") != character_version:
            raise ReviewUIError(f"request {request.manifest_id} references a mismatched character version")
        prior = _validated_prior_reviews(candidate_id, candidate, request_id, reviews)
        available = _verified_asset_bytes(asset_root, candidate) is not None
        payload = {
            "id": candidate_id, "request_id": request.manifest_id, "character_id": character_id,
            "character_version": character_version, "role": character.data.get("role"),
            "pose": request.data.get("pose"), "expression": request.data.get("expression"),
            "crop": request.data.get("crop"), "facing": request.data.get("facing"),
            "tool_id": request.data.get("tool_id"), "model_id": request.data.get("model_id"),
            "license_status": request.data.get("license_status"), "candidate_status": candidate.get("status"),
            "sha256": candidate.get("sha256"), "width": candidate.get("width"), "height": candidate.get("height"),
            "color_space": candidate.get("color_space"), "has_alpha": candidate.get("has_alpha"),
            "provenance": candidate.get("provenance"), "image_available": available,
            "image_url": f"/assets/{candidate_id}" if available else None, "reviews": prior,
            "review_state": prior[-1]["decision"] if prior else "unreviewed",
        }
        candidates.append(CandidateView(payload, asset_root, dict(candidate)))
    candidates.sort(key=lambda item: item.payload["id"])
    return ReviewData(tuple(candidates))


def make_review_decision(
    candidate: dict[str, Any], *, decision: str, reviewer: str, categories: list[str],
    notes: str = "", timestamp: str | None = None, verified_image_sha256: str | None = None,
) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise ReviewUIError("unsupported review decision")
    if decision in {"accept", "shortlist"} and (
        candidate.get("candidate_status") != "technically_valid"
        or candidate.get("image_available") is not True
        or verified_image_sha256 != candidate.get("sha256")
    ):
        raise ReviewUIError("accept and shortlist require a live verified image matching the candidate checksum")
    if not reviewer or not reviewer.strip():
        raise ReviewUIError("reviewer is required")
    if any(item not in REVIEW_CATEGORIES for item in categories):
        raise ReviewUIError("unknown review category")
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not UTC_RE.fullmatch(timestamp):
        raise ReviewUIError("timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ")
    candidate_id, request_id, checksum = str(candidate["id"]), str(candidate["request_id"]), str(candidate["sha256"])
    if not ID_RE.fullmatch(candidate_id) or not ID_RE.fullmatch(request_id):
        raise ReviewUIError("candidate/request ids are invalid")
    suffix = hashlib.sha256(f"{candidate_id}\n{decision}\n{timestamp}\n{checksum}".encode()).hexdigest()[:12]
    output: dict[str, Any] = {
        "kind": "review-decision", "schema_version": "1.0", "id": f"review-{candidate_id}-{suffix}",
        "candidate_ref": candidate_id, "candidate_request_ref": request_id, "candidate_sha256": checksum,
        "decision": decision, "reviewer": reviewer.strip(), "timestamp": timestamp,
        "categories": sorted(set(categories)),
    }
    if notes.strip():
        output["notes"] = notes.strip()
    if validate_document(Manifest(Path("download.json"), output)):
        raise ReviewUIError("generated review decision did not validate")
    return output


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], data: ReviewData, static_root: Path):
        if server_address[0] != "127.0.0.1":
            raise ReviewUIError("review server may bind only to 127.0.0.1")
        self.review_data = data
        self.static_root = static_root.resolve(strict=True)
        super().__init__(server_address, ReviewRequestHandler)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _headers(self, status: int, content_type: str, length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()

    def _send(self, status: int, content_type: str, payload: bytes, *, head: bool = False) -> None:
        self._headers(status, content_type, len(payload))
        if not head:
            self.wfile.write(payload)

    def _serve(self, *, head: bool = False) -> None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain; charset=utf-8", b"query and fragment are rejected", head=head)
            return
        path = unquote(parsed.path)
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            try:
                payload = resolve_beneath(self.server.static_root, filename, require_file=True).read_bytes()
            except (ReviewUIError, OSError):
                self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found", head=head)
                return
            self._send(HTTPStatus.OK, content_type, payload, head=head)
            return
        if path == "/api/candidates":
            payload = json.dumps(self.server.review_data.public_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", payload, head=head)
            return
        if path.startswith("/assets/"):
            candidate_id = path[len("/assets/"):]
            candidate = self.server.review_data.by_id(candidate_id) if ID_RE.fullmatch(candidate_id) else None
            payload = candidate.read_verified_asset() if candidate else None
            if payload is None:
                self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found", head=head)
                return
            self._send(HTTPStatus.OK, "image/png", payload, head=head)
            return
        self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found", head=head)

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed


def create_server(manifest_root: Path, asset_root: Path, port: int = 8765) -> ReviewHTTPServer:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ReviewUIError("port must be between 0 and 65535")
    return ReviewHTTPServer(("127.0.0.1", port), load_review_data(manifest_root, asset_root), _static_root())


def run_review_ui(manifest_root: Path, asset_root: Path, port: int = 8765) -> None:
    server = create_server(manifest_root, asset_root, port)
    print(f"Review UI: http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
