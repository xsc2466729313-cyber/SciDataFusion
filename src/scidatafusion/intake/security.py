"""SSRF-resistant URL preflight with an injectable asynchronous DNS resolver."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scidatafusion.contracts.base import RunId, SemanticVersion, TaskId
from scidatafusion.contracts.task import (
    IntakeProblem,
    IntakeProblemCode,
    IntakeStatus,
    ProblemDetail,
    ProblemSeverity,
    SecurityDecision,
    UrlSecurityCheck,
)

IpAddress = IPv4Address | IPv6Address
METADATA_HOSTS = frozenset(
    {
        "instance-data",
        "metadata",
        "metadata.azure.internal",
        "metadata.google.internal",
    }
)
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api-key",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "signature",
        "token",
    }
)


class DNSResolver(Protocol):
    """Controlled DNS boundary; production and tests provide their own implementation."""

    async def resolve(self, hostname: str) -> Sequence[str]:
        """Resolve a hostname without making any HTTP request."""


class SecurityPreflight:
    """Validate URL syntax, host allowlist, DNS answers, and non-public addresses."""

    def __init__(
        self,
        *,
        resolver: DNSResolver,
        allowed_hosts: Sequence[str],
        policy_version: SemanticVersion = "1.0.0",
    ) -> None:
        normalized = tuple(sorted({self._normalize_rule(rule) for rule in allowed_hosts}))
        if not normalized:
            msg = "allowed_hosts must contain at least one explicit host rule"
            raise ValueError(msg)
        self._resolver = resolver
        self._allowed_hosts = normalized
        self._policy_version = policy_version

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """Return the immutable host allowlist used by this preflight."""

        return self._allowed_hosts

    async def evaluate(
        self,
        urls: tuple[str, ...],
        *,
        task_id: TaskId,
        run_id: RunId,
        contract_version: SemanticVersion,
        producer_version: SemanticVersion,
        created_at: datetime,
        external_model_allowed: bool,
        additional_problems: tuple[IntakeProblem, ...] = (),
    ) -> SecurityDecision:
        """Run preflight over each URL and combine all deterministic M00 problems."""

        checks = tuple([await self.check_url(url) for url in urls])
        problems = (
            *additional_problems,
            *(problem for check in checks for problem in check.problems),
        )
        has_error = any(problem.severity is ProblemSeverity.ERROR for problem in problems)
        error_codes = {
            problem.code for problem in problems if problem.severity is ProblemSeverity.ERROR
        }
        if has_error and error_codes == {IntakeProblemCode.GOAL_NEEDS_CLARIFICATION}:
            outcome = IntakeStatus.NEEDS_CLARIFICATION
        elif has_error:
            outcome = IntakeStatus.REJECTED
        else:
            outcome = IntakeStatus.ACCEPTED
        return SecurityDecision(
            task_id=task_id,
            run_id=run_id,
            contract_version=contract_version,
            producer_version=producer_version,
            created_at=created_at,
            outcome=outcome,
            url_checks=checks,
            external_model_allowed=external_model_allowed,
            problems=problems,
            policy_version=self._policy_version,
        )

    async def check_url(self, url: str) -> UrlSecurityCheck:
        """Validate a URL before every request or redirect hop."""

        problems: list[IntakeProblem] = []
        hostname: str | None = None
        resolved: tuple[str, ...] = ()
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError:
            problems.append(self._problem(IntakeProblemCode.URL_INVALID, "URL is malformed", url))
            return self._check(url, hostname, resolved, problems)

        if parsed.scheme.lower() not in {"http", "https"}:
            problems.append(
                self._problem(
                    IntakeProblemCode.URL_SCHEME_BLOCKED,
                    "Only HTTP and HTTPS URLs are permitted",
                    url,
                )
            )
        if parsed.username is not None or parsed.password is not None:
            problems.append(
                self._problem(
                    IntakeProblemCode.URL_CREDENTIALS_BLOCKED,
                    "Credentials embedded in URLs are not permitted",
                    url,
                )
            )
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if query_keys & SENSITIVE_QUERY_KEYS:
            problems.append(
                self._problem(
                    IntakeProblemCode.URL_CREDENTIALS_BLOCKED,
                    "Credentials and access tokens must not be embedded in URL query parameters",
                    url,
                )
            )

        raw_hostname = parsed.hostname
        if raw_hostname is None:
            problems.append(
                self._problem(IntakeProblemCode.URL_INVALID, "URL has no hostname", url)
            )
            return self._check(url, hostname, resolved, problems)
        try:
            hostname = raw_hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            problems.append(
                self._problem(IntakeProblemCode.URL_INVALID, "URL hostname is invalid", url)
            )
            return self._check(url, hostname, resolved, problems)

        if self._is_metadata_hostname(hostname):
            problems.append(
                self._problem(
                    IntakeProblemCode.SSRF_BLOCKED,
                    "Cloud metadata and local service hosts are blocked",
                    url,
                    hostname=hostname,
                )
            )
        if not self._host_allowed(hostname):
            problems.append(
                self._problem(
                    IntakeProblemCode.URL_HOST_NOT_ALLOWED,
                    "URL hostname is not present in the configured allowlist",
                    url,
                    hostname=hostname,
                )
            )
        if problems:
            return self._check(url, hostname, resolved, problems)

        literal_address = self._parse_address(hostname)
        addresses: tuple[IpAddress, ...]
        if literal_address is not None:
            addresses = (literal_address,)
        else:
            try:
                answers = await self._resolver.resolve(hostname)
                addresses = tuple(self._parse_required_address(answer) for answer in answers)
            except (OSError, TimeoutError, ValueError):
                problems.append(
                    self._problem(
                        IntakeProblemCode.DNS_RESOLUTION_FAILED,
                        "Hostname could not be resolved to validated IP addresses",
                        url,
                        hostname=hostname,
                    )
                )
                return self._check(url, hostname, resolved, problems)
            if not addresses:
                problems.append(
                    self._problem(
                        IntakeProblemCode.DNS_RESOLUTION_FAILED,
                        "Hostname resolved to no IP addresses",
                        url,
                        hostname=hostname,
                    )
                )
                return self._check(url, hostname, resolved, problems)

        resolved = tuple(sorted({str(address) for address in addresses}))
        blocked = tuple(address for address in addresses if not address.is_global)
        if blocked:
            problems.append(
                self._problem(
                    IntakeProblemCode.SSRF_BLOCKED,
                    "URL resolves to a non-public IP address",
                    url,
                    hostname=hostname,
                    blocked_addresses=",".join(sorted({str(address) for address in blocked})),
                )
            )
        return self._check(url, hostname, resolved, problems)

    @staticmethod
    def _check(
        url: str,
        hostname: str | None,
        resolved: tuple[str, ...],
        problems: list[IntakeProblem],
    ) -> UrlSecurityCheck:
        return UrlSecurityCheck(
            url=SecurityPreflight._redact_url(url),
            hostname=hostname,
            resolved_addresses=resolved,
            allowed=not problems,
            problems=tuple(problems),
        )

    def _host_allowed(self, hostname: str) -> bool:
        for rule in self._allowed_hosts:
            if rule == "*":
                return True
            if rule.startswith("*."):
                suffix = rule[1:]
                if hostname.endswith(suffix) and hostname != suffix[1:]:
                    return True
            elif hostname == rule:
                return True
        return False

    @staticmethod
    def _normalize_rule(rule: str) -> str:
        normalized = rule.strip().rstrip(".").lower()
        if not normalized:
            msg = "host allowlist rules cannot be empty"
            raise ValueError(msg)
        if normalized == "*":
            return normalized
        if SecurityPreflight._parse_address(normalized) is not None:
            return normalized
        if normalized.startswith("*."):
            normalized = f"*.{normalized[2:].encode('idna').decode('ascii')}"
        else:
            normalized = normalized.encode("idna").decode("ascii")
        if "/" in normalized or ":" in normalized:
            msg = "host allowlist rules must contain hostnames, not URLs or ports"
            raise ValueError(msg)
        return normalized

    @staticmethod
    def _is_metadata_hostname(hostname: str) -> bool:
        return (
            hostname in METADATA_HOSTS
            or hostname == "localhost"
            or hostname.endswith(".localhost")
            or hostname.endswith(".local")
            or hostname.endswith(".internal")
        )

    @staticmethod
    def _parse_address(value: str) -> IpAddress | None:
        try:
            return ip_address(value)
        except ValueError:
            return None

    @staticmethod
    def _parse_required_address(value: str) -> IpAddress:
        return ip_address(value)

    @staticmethod
    def _problem(
        code: IntakeProblemCode,
        message: str,
        url: str,
        **details: str,
    ) -> IntakeProblem:
        return IntakeProblem(
            code=code,
            message=message,
            field="source_urls",
            details=(
                ProblemDetail(key="url", value=SecurityPreflight._redact_url(url)),
                *(ProblemDetail(key=key, value=value) for key, value in sorted(details.items())),
            ),
        )

    @staticmethod
    def _redact_url(url: str) -> str:
        """Remove embedded credentials and sensitive query values before audit storage."""

        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            if hostname is None:
                return "<invalid-url>"
            rendered_host = f"[{hostname}]" if ":" in hostname else hostname
            try:
                port = parsed.port
            except ValueError:
                port = None
            netloc = f"{rendered_host}:{port}" if port is not None else rendered_host
            query = urlencode(
                [
                    (
                        key,
                        "[REDACTED]" if key.casefold() in SENSITIVE_QUERY_KEYS else value,
                    )
                    for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                ]
            )
            return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
        except (UnicodeError, ValueError):
            return "<invalid-url>"
