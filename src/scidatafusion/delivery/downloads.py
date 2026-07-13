"""Short-lived, content-bound download tickets for M20 artifacts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Callable

from scidatafusion.contracts.delivery import DeliveryArtifact
from scidatafusion.errors import AppError, ErrorCode


class DownloadTicketSigner:
    """Issue and verify HMAC tickets without persisting a signing secret."""

    def __init__(
        self,
        secret: bytes,
        *,
        lifetime_seconds: int = 120,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(secret) < 32:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M20 download signing secret must contain at least 32 bytes",
            )
        if not 10 <= lifetime_seconds <= 300:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M20 ticket lifetime")
        self._secret = secret
        self._lifetime = lifetime_seconds
        self._clock = clock

    def issue(self, artifact: DeliveryArtifact) -> tuple[str, int]:
        """Return a URL-safe signature and bounded Unix expiry."""

        expires_at = int(self._clock()) + self._lifetime
        return self._signature(artifact, expires_at), expires_at

    def verify(self, artifact: DeliveryArtifact, token: str, expires_at: int) -> None:
        """Reject expired, overlong, malformed, or content-mismatched tickets."""

        now = int(self._clock())
        if expires_at < now or expires_at > now + self._lifetime:
            raise AppError(ErrorCode.SECURITY_POLICY_VIOLATION, "M20 download ticket expired")
        expected = self._signature(artifact, expires_at)
        if len(token) > 128 or not hmac.compare_digest(token, expected):
            raise AppError(ErrorCode.SECURITY_POLICY_VIOLATION, "invalid M20 download ticket")

    def _signature(self, artifact: DeliveryArtifact, expires_at: int) -> str:
        message = f"{artifact.filename}\n{artifact.sha256}\n{expires_at}".encode("ascii")
        digest = hmac.new(self._secret, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
