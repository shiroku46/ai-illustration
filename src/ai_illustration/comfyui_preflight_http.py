"""Bounded non-generating HTTP client for local ComfyUI readiness checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .adapters.base import AdapterError
from .adapters.comfyui import sanitize_loopback_endpoint


MAX_NODE_CLASS_CHARS = 256
MAX_ENCODED_SEGMENT_CHARS = 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[no-untyped-def]
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        raise AdapterError(
            "HTTP_REDIRECT",
            "redirects are forbidden",
            req.full_url,
        )


@dataclass(frozen=True)
class PreflightHttpLimits:
    system_response_bytes: int = 1024 * 1024
    models_response_bytes: int = 4 * 1024 * 1024
    node_response_bytes: int = 1024 * 1024
    request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        for field, value, minimum, maximum in (
            (
                "system_response_bytes",
                self.system_response_bytes,
                128,
                4 * 1024 * 1024,
            ),
            (
                "models_response_bytes",
                self.models_response_bytes,
                128,
                16 * 1024 * 1024,
            ),
            (
                "node_response_bytes",
                self.node_response_bytes,
                128,
                4 * 1024 * 1024,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise AdapterError(
                    "PREFLIGHT_LIMIT",
                    f"{field} is outside its safe range",
                    field,
                )
        timeout = self.request_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.05 <= timeout <= 300
        ):
            raise AdapterError(
                "PREFLIGHT_LIMIT",
                "request_timeout_seconds must be from 0.05 to 300",
                "request_timeout_seconds",
            )


def encode_node_class(node_class: str) -> str:
    if (
        not isinstance(node_class, str)
        or not node_class
        or len(node_class) > MAX_NODE_CLASS_CHARS
        or node_class != node_class.strip()
        or not node_class.isprintable()
        or any(
            character in node_class
            for character in ("/", "\\", "?", "#", "\x00")
        )
    ):
        raise AdapterError(
            "NODE_CLASS_ROUTE",
            "node class is not safe for an individual object-info route",
            "node_class",
        )
    encoded = quote(node_class, safe="")
    if (
        not encoded
        or len(encoded) > MAX_ENCODED_SEGMENT_CHARS
        or "/" in encoded
    ):
        raise AdapterError(
            "NODE_CLASS_ROUTE",
            "encoded node class exceeds the route limit",
            "node_class",
        )
    return encoded


def _json_value(payload: bytes, field: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise AdapterError(
                    "DUPLICATE_JSON_KEY",
                    f"duplicate JSON key: {key}",
                    field,
                )
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
        )
    except AdapterError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "INVALID_HTTP_JSON",
            str(exc),
            field,
        ) from exc


class ComfyUIPreflightHttpClient:
    """Read-only client limited to one loopback origin and three route families."""

    def __init__(
        self,
        endpoint: str,
        limits: PreflightHttpLimits | None = None,
    ) -> None:
        sanitized = sanitize_loopback_endpoint(endpoint)
        parsed = urlsplit(sanitized)
        if parsed.hostname == "localhost":
            netloc = "127.0.0.1"
            if parsed.port is not None:
                netloc += f":{parsed.port}"
            self.endpoint = urlunsplit(("http", netloc, "", "", ""))
        else:
            self.endpoint = sanitized
        self.limits = limits or PreflightHttpLimits()
        self._opener = build_opener(
            ProxyHandler({}),
            _RejectRedirects(),
        )
        self.requested_routes: list[str] = []

    @staticmethod
    def _authorized_path(path: str) -> bool:
        if path in {"/system_stats", "/models/checkpoints"}:
            return True
        if not path.startswith("/object_info/"):
            return False
        suffix = path[len("/object_info/") :]
        return (
            bool(suffix)
            and "/" not in suffix
            and "?" not in suffix
            and "#" not in suffix
        )

    def _request(self, path: str, *, maximum: int) -> bytes:
        if not self._authorized_path(path):
            raise AdapterError(
                "HTTP_ROUTE",
                "route is not authorized for preflight",
                path,
            )
        url = self.endpoint + path
        request = Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        self.requested_routes.append(path)
        try:
            response = self._opener.open(
                request,
                timeout=float(self.limits.request_timeout_seconds),
            )
        except AdapterError:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise AdapterError(
                "HTTP_STATUS",
                f"HTTP {status}",
                path,
            ) from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise AdapterError(
                    "HTTP_TIMEOUT",
                    "request timed out",
                    path,
                ) from exc
            raise AdapterError(
                "HTTP_ERROR",
                str(reason),
                path,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise AdapterError(
                "HTTP_TIMEOUT",
                "request timed out",
                path,
            ) from exc
        with response:
            if response.geturl() != url:
                raise AdapterError(
                    "HTTP_REDIRECT",
                    "redirected response is forbidden",
                    path,
                )
            status = getattr(response, "status", 200)
            if status != 200:
                raise AdapterError(
                    "HTTP_STATUS",
                    f"HTTP {status}",
                    path,
                )
            content_type = response.headers.get_content_type().lower()
            if content_type != "application/json":
                raise AdapterError(
                    "HTTP_CONTENT_TYPE",
                    f"expected application/json, received {content_type}",
                    path,
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise AdapterError(
                        "HTTP_LENGTH",
                        "invalid Content-Length",
                        path,
                    ) from exc
                if declared < 0 or declared > maximum:
                    raise AdapterError(
                        "HTTP_RESPONSE_TOO_LARGE",
                        "response exceeds configured limit",
                        path,
                    )
            payload = response.read(maximum + 1)
            if len(payload) > maximum:
                raise AdapterError(
                    "HTTP_RESPONSE_TOO_LARGE",
                    "response exceeds configured limit",
                    path,
                )
            return payload

    def system_stats(self) -> dict[str, Any]:
        value = _json_value(
            self._request(
                "/system_stats",
                maximum=self.limits.system_response_bytes,
            ),
            "system_stats",
        )
        if not isinstance(value, dict):
            raise AdapterError(
                "SYSTEM_STATS_SCHEMA",
                "system_stats root must be an object",
                "system_stats",
            )
        return value

    def checkpoints(self) -> list[Any]:
        value = _json_value(
            self._request(
                "/models/checkpoints",
                maximum=self.limits.models_response_bytes,
            ),
            "checkpoints",
        )
        if not isinstance(value, list):
            raise AdapterError(
                "CHECKPOINTS_SCHEMA",
                "checkpoint response root must be an array",
                "checkpoints",
            )
        return value

    def object_info(self, node_class: str) -> dict[str, Any]:
        encoded = encode_node_class(node_class)
        value = _json_value(
            self._request(
                f"/object_info/{encoded}",
                maximum=self.limits.node_response_bytes,
            ),
            f"object_info.{node_class}",
        )
        if not isinstance(value, dict):
            raise AdapterError(
                "OBJECT_INFO_SCHEMA",
                "object-info root must be an object",
                node_class,
            )
        return value
