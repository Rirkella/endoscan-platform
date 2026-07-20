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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .contracts import SourceToolDiagnostic

APPROVED_SOURCE_HOSTS = frozenset(
    {
        "eutils.ncbi.nlm.nih.gov",
        "comptox.epa.gov",
        "ftp.ncbi.nlm.nih.gov",
        "api.clue.io",
        "clue.io",
        "lincsproject.org",
        "pubchem.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
    }
)
OFFICIAL_GEO_HOST = "www.ncbi.nlm.nih.gov"
OFFICIAL_GEO_ACCESSION_PATH = "/geo/query/acc.cgi"
OFFICIAL_GEO_TEXT_MIME = "geo/text"
OFFICIAL_EPA_HEALTH_HOST = "comptox.epa.gov"
OFFICIAL_EPA_HEALTH_PATH = "/ctx-api/bioactivity/health"
TECHNICAL_HEALTH_STATUS_MIME = "health/status-only"
GSE_ACCESSION = re.compile(r"^GSE[1-9][0-9]{1,8}$", re.I)
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/vnd.spring-boot.actuator.v2+json",
        "application/vnd.spring-boot.actuator.v3+json",
        "application/xml",
        "text/xml",
        "text/plain",
        "text/csv",
        "application/csv",
    }
)
INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"reveal\s+(the\s+)?(api\s+key|secret|system\s+prompt)", re.I),
    re.compile(r"(call|invoke|use)\s+(this\s+)?tool", re.I),
    re.compile(r"override\s+(the\s+)?system", re.I),
)


class SourceToolError(RuntimeError):
    retryable = False

    def __init__(
        self, safe_message: str, *, diagnostic: SourceToolDiagnostic | None = None
    ) -> None:
        super().__init__(safe_message)
        self.diagnostic = diagnostic
        self.attempt_diagnostics: tuple[SourceToolDiagnostic, ...] = ()


class SourcePolicyError(SourceToolError):
    retryable = False


class SourceUnavailableError(SourceToolError):
    retryable = True


class SourceTimeoutError(SourceUnavailableError):
    pass


class SourceRateLimitError(SourceUnavailableError):
    pass


class SourceResponseError(SourceUnavailableError):
    retryable = False


class SourceFormatError(SourceResponseError):
    pass


@dataclass(frozen=True)
class ScientificResponse:
    url: str
    content: bytes
    content_type: str
    status_code: int
    headers: dict[str, str]
    retrieved_at: float
    diagnostic: SourceToolDiagnostic | None = None
    attempt_diagnostics: tuple[SourceToolDiagnostic, ...] = ()

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
        maximum_bytes: int = 5_000_000,
        requests_per_second: float = 2.5,
        maximum_attempts: int = 3,
        maximum_redirects: int = 2,
        approved_hosts: frozenset[str] = APPROVED_SOURCE_HOSTS,
        user_agent: str = "EndoScan/phase1-live-discovery (scientific metadata only)",
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ):
        self.timeout_seconds = timeout_seconds
        self.maximum_bytes = maximum_bytes
        self.minimum_interval = 1.0 / requests_per_second
        if maximum_attempts < 1 or maximum_attempts > 3:
            raise ValueError("maximum_attempts must be between 1 and 3")
        if maximum_redirects < 0 or maximum_redirects > 3:
            raise ValueError("maximum_redirects must be between 0 and 3")
        self.maximum_attempts = maximum_attempts
        self.maximum_redirects = maximum_redirects
        self.approved_hosts = frozenset(item.lower().rstrip(".") for item in approved_hosts)
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

    def __enter__(self) -> ScientificSourceClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        tool_name: str = "scientific_source",
        params: dict[str, str | int] | None = None,
        accepted_types: set[str] | frozenset[str] = ALLOWED_CONTENT_TYPES,
        allow_official_geo_text: bool = False,
        maximum_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        allow_empty_health_response: bool = False,
        approved_request_hosts: frozenset[str] | None = None,
        approved_redirect_hosts: frozenset[str] | None = None,
        maximum_attempts: int | None = None,
    ) -> ScientificResponse:
        response_limit = self.maximum_bytes if maximum_bytes is None else maximum_bytes
        if response_limit < 1 or response_limit > self.maximum_bytes:
            raise SourcePolicyError("Scientific source response limit is outside policy.")
        try:
            self._validate_url(url, additional_approved_hosts=approved_request_hosts)
        except SourcePolicyError as exc:
            raise SourcePolicyError(
                str(exc),
                diagnostic=self._diagnostic(
                    tool_name=tool_name,
                    url=url,
                    category="source_url_policy",
                    exception_class=type(exc).__name__,
                    developer_message=str(exc),
                ),
            ) from exc
        allowed_attempts = self.maximum_attempts if maximum_attempts is None else maximum_attempts
        if allowed_attempts < 1 or allowed_attempts > self.maximum_attempts:
            raise SourcePolicyError("Scientific source attempt count is outside policy.")
        last_error: SourceToolError | None = None
        attempt_diagnostics: list[SourceToolDiagnostic] = []
        for attempt_index in range(allowed_attempts):
            attempt_number = attempt_index + 1
            self._rate_limit()
            started = time.monotonic()
            try:
                response, redirect_count = self._get_with_approved_redirects(
                    url,
                    params=params,
                    headers=headers,
                    tool_name=tool_name,
                    attempt_number=attempt_number,
                    started=started,
                    approved_redirect_hosts=approved_redirect_hosts,
                )
            except httpx.TimeoutException as exc:
                diagnostic = self._diagnostic(
                    tool_name=tool_name,
                    url=url,
                    category="timeout",
                    retryable=True,
                    attempt_number=attempt_number,
                    duration_ms=self._elapsed_ms(started),
                    exception_class=type(exc).__name__,
                )
                attempt_diagnostics.append(diagnostic)
                last_error = SourceTimeoutError(
                    "Official scientific source timed out.",
                    diagnostic=diagnostic,
                )
            except httpx.HTTPError as exc:
                diagnostic = self._diagnostic(
                    tool_name=tool_name,
                    url=url,
                    category="network_unavailable",
                    retryable=True,
                    attempt_number=attempt_number,
                    duration_ms=self._elapsed_ms(started),
                    exception_class=type(exc).__name__,
                )
                attempt_diagnostics.append(diagnostic)
                last_error = SourceUnavailableError(
                    "Official scientific source is unavailable.",
                    diagnostic=diagnostic,
                )
            except SourceToolError:
                raise
            else:
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                response_bytes = len(response.content)
                final_host = (response.url.host or "").lower().rstrip(".")
                if self._is_approved_empty_health_response(
                    requested_url=url,
                    final_url=str(response.url),
                    params=params,
                    redirect_count=redirect_count,
                    content_type=content_type,
                    response_bytes=response_bytes,
                    allow_empty_health_response=allow_empty_health_response,
                ):
                    content_type = TECHNICAL_HEALTH_STATUS_MIME
                base_diagnostic = dict(
                    tool_name=tool_name,
                    url=str(response.url),
                    http_status=response.status_code,
                    final_host=final_host,
                    redirect_count=redirect_count,
                    content_type=content_type or None,
                    response_byte_count=response_bytes,
                    attempt_number=attempt_number,
                    duration_ms=self._elapsed_ms(started),
                )
                if response.status_code == 429:
                    diagnostic = self._diagnostic(
                        **base_diagnostic,
                        category="rate_limited",
                        retryable=False,
                        exception_class="SourceRateLimitError",
                    )
                    attempt_diagnostics.append(diagnostic)
                    raise SourceRateLimitError(
                        "Official scientific source rate limit reached.",
                        diagnostic=diagnostic,
                    )
                elif 500 <= response.status_code < 600:
                    diagnostic = self._diagnostic(
                        **base_diagnostic,
                        category="source_server_error",
                        retryable=True,
                        exception_class="SourceUnavailableError",
                    )
                    attempt_diagnostics.append(diagnostic)
                    last_error = SourceUnavailableError(
                        "Official scientific source returned a server error.",
                        diagnostic=diagnostic,
                    )
                elif response.status_code >= 400:
                    raise SourceResponseError(
                        "Official scientific source rejected the request.",
                        diagnostic=self._diagnostic(
                            **base_diagnostic,
                            category="source_client_error",
                            retryable=False,
                            exception_class="SourceResponseError",
                        ),
                    )
                else:
                    if not self._content_type_allowed(
                        content_type,
                        accepted_types=accepted_types,
                        requested_url=url,
                        final_url=str(response.url),
                        params=params,
                        allow_official_geo_text=allow_official_geo_text,
                    ):
                        raise SourceFormatError(
                            "Scientific source returned an unexpected content type.",
                            diagnostic=self._diagnostic(
                                **base_diagnostic,
                                category="unexpected_content_type",
                                retryable=False,
                                exception_class="SourceFormatError",
                                developer_message=(
                                    "Expected a machine-readable GEO/NCBI response content type."
                                ),
                            ),
                        )
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > response_limit or response_bytes > response_limit:
                        raise SourcePolicyError(
                            "Scientific source response exceeded the configured limit.",
                            diagnostic=self._diagnostic(
                                **base_diagnostic,
                                category="response_too_large",
                                retryable=False,
                                exception_class="SourcePolicyError",
                            ),
                        )
                    diagnostic = self._diagnostic(
                        **base_diagnostic,
                        category="none",
                        retryable=False,
                    )
                    attempt_diagnostics.append(diagnostic)
                    return ScientificResponse(
                        url=self._safe_source_url(str(response.url)),
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
                        diagnostic=diagnostic,
                        attempt_diagnostics=tuple(attempt_diagnostics),
                    )
            if attempt_index + 1 < allowed_attempts:
                self.sleep(min(0.25 * (2**attempt_index), 1.0))
        if last_error:
            last_error.attempt_diagnostics = tuple(attempt_diagnostics)
            raise last_error
        raise SourceUnavailableError("Official scientific source is unavailable.")

    @staticmethod
    def _is_approved_empty_health_response(
        *,
        requested_url: str,
        final_url: str,
        params: dict[str, str | int] | None,
        redirect_count: int,
        content_type: str,
        response_bytes: int,
        allow_empty_health_response: bool,
    ) -> bool:
        if (
            not allow_empty_health_response
            or content_type
            or response_bytes != 0
            or redirect_count != 0
            or params
        ):
            return False
        requested = urlparse(requested_url)
        final = urlparse(final_url)
        return all(
            parsed.scheme == "https"
            and (parsed.hostname or "").lower().rstrip(".") == OFFICIAL_EPA_HEALTH_HOST
            and parsed.port in {None, 443}
            and parsed.path == OFFICIAL_EPA_HEALTH_PATH
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
            for parsed in (requested, final)
        )

    @staticmethod
    def _content_type_allowed(
        content_type: str,
        *,
        accepted_types: set[str] | frozenset[str],
        requested_url: str,
        final_url: str,
        params: dict[str, str | int] | None,
        allow_official_geo_text: bool,
    ) -> bool:
        # ``geo/text`` is not a generic safe MIME. It is accepted only for the exact
        # internally constructed, validated GEO Accession Display contract.
        if content_type != OFFICIAL_GEO_TEXT_MIME:
            return content_type in accepted_types
        if not allow_official_geo_text or params is None:
            return False
        requested = urlparse(requested_url)
        final = urlparse(final_url)
        if (
            requested.scheme != "https"
            or (requested.hostname or "").lower().rstrip(".") != OFFICIAL_GEO_HOST
            or requested.port not in {None, 443}
            or requested.path != OFFICIAL_GEO_ACCESSION_PATH
            or requested.query
            or final.scheme != "https"
            or (final.hostname or "").lower().rstrip(".") != OFFICIAL_GEO_HOST
            or final.port not in {None, 443}
            or final.path != OFFICIAL_GEO_ACCESSION_PATH
        ):
            return False
        if set(params) != {"acc", "targ", "view", "form"}:
            return False
        accession = str(params.get("acc", "")).upper()
        return (
            bool(GSE_ACCESSION.fullmatch(accession))
            and str(params.get("targ", "")) == "self"
            and str(params.get("form", "")) == "text"
            and str(params.get("view", "")) in {"brief", "full"}
        )

    def _get_with_approved_redirects(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None,
        headers: dict[str, str] | None,
        tool_name: str,
        attempt_number: int,
        started: float,
        approved_redirect_hosts: frozenset[str] | None,
    ) -> tuple[httpx.Response, int]:
        current_url = url
        current_params = params
        current_headers = headers
        credential_host = (urlparse(url).hostname or "").lower().rstrip(".")
        for redirect_count in range(self.maximum_redirects + 1):
            response = self.client.get(
                current_url,
                params=current_params,
                headers=current_headers,
            )
            if not response.is_redirect:
                return response, redirect_count
            location = response.headers.get("location", "")
            if not location:
                raise SourcePolicyError(
                    "Scientific source returned an invalid redirect.",
                    diagnostic=self._diagnostic(
                        tool_name=tool_name,
                        url=str(response.url),
                        category="invalid_redirect",
                        retryable=False,
                        attempt_number=attempt_number,
                        duration_ms=self._elapsed_ms(started),
                        http_status=response.status_code,
                        final_host=(response.url.host or "").lower().rstrip(".") or None,
                        redirect_count=redirect_count + 1,
                        exception_class="SourcePolicyError",
                    ),
                )
            redirected = str(response.url.join(location))
            try:
                self._validate_url(
                    redirected,
                    additional_approved_hosts=approved_redirect_hosts,
                )
            except SourcePolicyError as exc:
                raise SourcePolicyError(
                    "Scientific source redirected to a prohibited destination.",
                    diagnostic=self._diagnostic(
                        tool_name=tool_name,
                        url=str(response.url),
                        category="redirect_not_approved",
                        retryable=False,
                        attempt_number=attempt_number,
                        duration_ms=self._elapsed_ms(started),
                        http_status=response.status_code,
                        final_host=(response.url.host or "").lower().rstrip(".") or None,
                        redirect_count=redirect_count + 1,
                        exception_class=type(exc).__name__,
                        developer_message=(
                            "Redirect target host failed the scientific-source allowlist."
                        ),
                    ),
                ) from exc
            redirected_host = (urlparse(redirected).hostname or "").lower().rstrip(".")
            if (
                approved_redirect_hosts is not None
                and redirected_host not in approved_redirect_hosts
            ):
                raise SourcePolicyError(
                    "Scientific source redirected outside the operation allowlist.",
                    diagnostic=self._diagnostic(
                        tool_name=tool_name,
                        url=str(response.url),
                        category="redirect_not_approved",
                        retryable=False,
                        attempt_number=attempt_number,
                        duration_ms=self._elapsed_ms(started),
                        http_status=response.status_code,
                        final_host=(response.url.host or "").lower().rstrip(".") or None,
                        redirect_count=redirect_count + 1,
                        exception_class="SourcePolicyError",
                        developer_message=(
                            "Redirect target host failed the operation-specific allowlist."
                        ),
                    ),
                )
            current_url = redirected
            current_params = None
            if redirected_host != credential_host:
                current_headers = {
                    name: value
                    for name, value in (current_headers or {}).items()
                    if name.casefold() == "accept"
                } or None
        raise SourcePolicyError(
            "Scientific source exceeded the approved redirect limit.",
            diagnostic=self._diagnostic(
                tool_name=tool_name,
                url=current_url,
                category="redirect_limit_exceeded",
                retryable=False,
                attempt_number=attempt_number,
                duration_ms=self._elapsed_ms(started),
                redirect_count=self.maximum_redirects + 1,
                exception_class="SourcePolicyError",
            ),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    @staticmethod
    def _safe_source_url(url: str) -> str:
        """Retain reproducible public parameters while dropping credentials and PII."""
        parsed = urlparse(url)
        sensitive = {"api_key", "access_token", "token", "key", "email"}
        query = urlencode(
            [
                (name, value)
                for name, value in parse_qsl(parsed.query)
                if name.casefold() not in sensitive
            ]
        )
        return urlunparse(parsed._replace(query=query, fragment=""))

    @staticmethod
    def _diagnostic(
        *,
        tool_name: str,
        url: str,
        category: str,
        retryable: bool = False,
        attempt_number: int = 1,
        duration_ms: int = 0,
        http_status: int | None = None,
        final_host: str | None = None,
        redirect_count: int = 0,
        content_type: str | None = None,
        response_byte_count: int | None = None,
        exception_class: str | None = None,
        developer_message: str | None = None,
    ) -> SourceToolDiagnostic:
        parsed = urlparse(url)
        return SourceToolDiagnostic(
            tool_name=tool_name,
            source_host=(parsed.hostname or "unknown").lower().rstrip("."),
            safe_url_path=parsed.path or "/",
            http_status=http_status,
            final_approved_host=final_host,
            redirect_count=redirect_count,
            content_type=content_type,
            response_byte_count=response_byte_count,
            exception_class=exception_class,
            source_error_category=category,
            retryable=retryable,
            attempt_number=attempt_number,
            request_duration_ms=duration_ms,
            developer_message=developer_message,
        )

    def _validate_url(
        self,
        url: str,
        *,
        additional_approved_hosts: frozenset[str] | None = None,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SourcePolicyError("Scientific source tools require HTTPS.")
        host = parsed.hostname.lower().rstrip(".")
        operation_hosts = frozenset(
            item.lower().rstrip(".") for item in (additional_approved_hosts or ())
        )
        if host not in self.approved_hosts and host not in operation_hosts:
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
