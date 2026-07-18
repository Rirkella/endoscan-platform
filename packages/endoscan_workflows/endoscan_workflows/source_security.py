"""Bounded HTTPS client for official NCBI/GEO metadata sources."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import threading
import time
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

APPROVED_SOURCE_HOSTS = frozenset(
    {
        "eutils.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
    }
)
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "text/xml",
        "text/plain",
    }
)
INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"reveal\s+(the\s+)?(api\s+key|secret|system\s+prompt)", re.I),
    re.compile(r"(call|invoke|use)\s+(this\s+)?tool", re.I),
    re.compile(r"override\s+(the\s+)?system", re.I),
)


class SourcePolicyError(RuntimeError):
    retryable = False


class SourceUnavailableError(RuntimeError):
    retryable = True


class SourceTimeoutError(SourceUnavailableError):
    pass


class SourceRateLimitError(SourceUnavailableError):
    pass


class SourceResponseError(SourceUnavailableError):
    retryable = False


@dataclass(frozen=True)
class ScientificResponse:
    url: str
    content: bytes
    content_type: str
    status_code: int
    headers: dict[str, str]
    retrieved_at: float

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def sanitize_untrusted_text(value: str, *, source_id: str, maximum: int = 8_000) -> dict[str, Any]:
    without_active = re.sub(
        r"<(script|style|iframe)\b[^>]*>.*?</\1>", " ", value, flags=re.I | re.S
    )
    plain = re.sub(r"<[^>]+>", " ", without_active)
    plain = re.sub(r"\s+", " ", unescape(plain)).strip()[:maximum]
    warnings = [
        f"instruction-like content detected by pattern {index + 1}"
        for index, pattern in enumerate(INJECTION_PATTERNS)
        if pattern.search(plain)
    ]
    return {
        "source_id": source_id,
        "untrusted_text": plain,
        "prompt_injection_warnings": warnings,
        "truncated": len(plain) >= maximum,
    }


class ScientificSourceClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        maximum_bytes: int = 1_500_000,
        requests_per_second: float = 2.5,
        user_agent: str = "EndoScan/phase1-live-discovery (scientific metadata only)",
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ):
        self.timeout_seconds = timeout_seconds
        self.maximum_bytes = maximum_bytes
        self.minimum_interval = 1.0 / requests_per_second
        self.sleep = sleep
        self._last_request = 0.0
        self._lock = threading.Lock()
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": "application/json, text/plain"},
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        accepted_types: set[str] | frozenset[str] = ALLOWED_CONTENT_TYPES,
    ) -> ScientificResponse:
        self._validate_url(url)
        last_error: Exception | None = None
        for attempt in range(3):
            self._rate_limit()
            try:
                response = self.client.get(url, params=params)
            except httpx.TimeoutException:
                last_error = SourceTimeoutError("Official scientific source timed out.")
            except httpx.HTTPError:
                last_error = SourceUnavailableError("Official scientific source is unavailable.")
            else:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise SourcePolicyError("Scientific source returned an invalid redirect.")
                    self._validate_url(str(response.url.join(location)))
                    raise SourcePolicyError("Redirects are disabled for scientific source tools.")
                if response.status_code == 429:
                    last_error = SourceRateLimitError(
                        "Official scientific source rate limit reached."
                    )
                elif 500 <= response.status_code < 600:
                    last_error = SourceUnavailableError(
                        "Official scientific source returned a server error."
                    )
                elif response.status_code >= 400:
                    raise SourceResponseError("Official scientific source rejected the request.")
                else:
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in accepted_types:
                        raise SourcePolicyError(
                            "Scientific source returned an unexpected content type."
                        )
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > self.maximum_bytes or len(response.content) > self.maximum_bytes:
                        raise SourcePolicyError(
                            "Scientific source response exceeded the configured limit."
                        )
                    return ScientificResponse(
                        url=str(response.url),
                        content=response.content,
                        content_type=content_type,
                        status_code=response.status_code,
                        headers={
                            key.lower(): value
                            for key, value in response.headers.items()
                            if key.lower()
                            in {"content-type", "content-length", "etag", "last-modified"}
                        },
                        retrieved_at=time.time(),
                    )
            if attempt < 2:
                self.sleep(min(0.25 * (2**attempt), 1.0))
        if last_error:
            raise last_error
        raise SourceUnavailableError("Official scientific source is unavailable.")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SourcePolicyError("Scientific source tools require HTTPS.")
        host = parsed.hostname.lower().rstrip(".")
        if host not in APPROVED_SOURCE_HOSTS:
            raise SourcePolicyError("Scientific source domain is not allowlisted.")
        if parsed.username or parsed.password:
            raise SourcePolicyError("Scientific source URL contains forbidden credentials.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise SourcePolicyError("Private-network scientific source access is prohibited.")

    def _rate_limit(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.minimum_interval - (now - self._last_request)
            if wait > 0:
                self.sleep(wait)
            self._last_request = time.monotonic()
