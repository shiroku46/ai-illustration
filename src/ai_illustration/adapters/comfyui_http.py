"""Bounded standard-library HTTP client for strict loopback ComfyUI execution."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .base import AdapterError
from .comfyui import canonical_json_bytes, sanitize_loopback_endpoint


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise AdapterError("HTTP_REDIRECT", "redirects are forbidden", req.full_url)


@dataclass(frozen=True)
class HttpLimits:
    queue_response_bytes: int
    history_response_bytes: int
    png_bytes: int
    request_timeout_seconds: int


class ComfyUIHttpClient:
    """HTTP client constrained to one sanitized loopback origin and three routes."""

    def __init__(self, endpoint: str, limits: HttpLimits) -> None:
        sanitized = sanitize_loopback_endpoint(endpoint)
        parsed = urlsplit(sanitized)
        if parsed.hostname == "localhost":
            netloc = "127.0.0.1"
            if parsed.port is not None:
                netloc += f":{parsed.port}"
            self.endpoint = urlunsplit(("http", netloc, "", "", ""))
        else:
            self.endpoint = sanitized
        self.limits = limits
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())

    @staticmethod
    def _parse_json(payload: bytes, field: str) -> dict[str, Any]:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise AdapterError("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}", field)
                result[key] = value
            return result

        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
        except AdapterError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdapterError("INVALID_HTTP_JSON", str(exc), field) from exc
        if not isinstance(value, dict):
            raise AdapterError("INVALID_HTTP_JSON", "response root must be an object", field)
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: bytes | None = None,
        maximum: int,
        expected_content_type: str,
        timeout_seconds: float | None = None,
    ) -> bytes:
        if path not in {"/prompt", "/view"} and not path.startswith("/history/"):
            raise AdapterError("HTTP_ROUTE", "route is not authorized", path)
        url = self.endpoint + path
        if query:
            url += "?" + urlencode(sorted(query.items()))
        headers = {"Accept": expected_content_type}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=timeout_seconds or self.limits.request_timeout_seconds)
        except AdapterError:
            raise
        except HTTPError as exc:
            raise AdapterError("HTTP_STATUS", f"HTTP {exc.code}", path) from exc
        except URLError as exc:
            raise AdapterError("HTTP_ERROR", str(exc.reason), path) from exc
        except TimeoutError as exc:
            raise AdapterError("HTTP_TIMEOUT", "request timed out", path) from exc
        with response:
            if response.geturl() != url:
                raise AdapterError("HTTP_REDIRECT", "redirected response is forbidden", path)
            status = getattr(response, "status", 200)
            if status != 200:
                raise AdapterError("HTTP_STATUS", f"HTTP {status}", path)
            content_type = response.headers.get_content_type().lower()
            if content_type != expected_content_type:
                raise AdapterError(
                    "HTTP_CONTENT_TYPE",
                    f"expected {expected_content_type}, received {content_type}",
                    path,
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise AdapterError("HTTP_LENGTH", "invalid Content-Length", path) from exc
                if declared < 0 or declared > maximum:
                    raise AdapterError("HTTP_RESPONSE_TOO_LARGE", "response exceeds configured limit", path)
            payload = response.read(maximum + 1)
            if len(payload) > maximum:
                raise AdapterError("HTTP_RESPONSE_TOO_LARGE", "response exceeds configured limit", path)
            return payload

    def queue_prompt(self, bound_workflow: Mapping[str, Any], *, timeout_seconds: float | None = None) -> str:
        payload = canonical_json_bytes({"prompt": bound_workflow})
        value = self._parse_json(
            self._request(
                "POST",
                "/prompt",
                body=payload,
                maximum=self.limits.queue_response_bytes,
                expected_content_type="application/json",
                timeout_seconds=timeout_seconds,
            ),
            "queue_response",
        )
        if set(value) - {"prompt_id", "number", "node_errors"}:
            raise AdapterError("QUEUE_RESPONSE_SCHEMA", "queue response has unknown fields", "queue_response")
        errors = value.get("node_errors")
        if errors not in (None, {}, []):
            raise AdapterError("QUEUE_NODE_ERRORS", "ComfyUI rejected one or more nodes", "queue_response.node_errors")
        prompt_id = value.get("prompt_id")
        if (
            not isinstance(prompt_id, str)
            or not prompt_id
            or len(prompt_id) > 128
            or any(not (character.isalnum() or character in "_-") for character in prompt_id)
        ):
            raise AdapterError("PROMPT_ID", "queue response prompt_id is invalid", "queue_response.prompt_id")
        return prompt_id

    def history(self, prompt_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/history/{prompt_id}",
            maximum=self.limits.history_response_bytes,
            expected_content_type="application/json",
            timeout_seconds=timeout_seconds,
        )
        return self._parse_json(payload, "history_response")

    def image(self, filename: str, subfolder: str, *, timeout_seconds: float | None = None) -> bytes:
        return self._request(
            "GET",
            "/view",
            query={"filename": filename, "subfolder": subfolder, "type": "output"},
            maximum=self.limits.png_bytes,
            expected_content_type="image/png",
            timeout_seconds=timeout_seconds,
        )
