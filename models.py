from dataclasses import dataclass
from enum import Enum


class ErrorKind(str, Enum):
    """Categories used to classify recognition failures."""

    NO_MATCH = "no_match"
    INPUT_INVALID = "input_invalid"
    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    TEMPORARY_NETWORK = "temporary_network"
    PROTOCOL_ERROR = "protocol_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class SongInfo:
    """Provider-neutral song recognition result."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    cover_url: str | None = None
    audio_url: str | None = None
    provider: str = ""
    mode: str = ""
    score: float | None = None
    acrid: str | None = None
    netease_id: str | None = None
    qq_songmid: str | None = None

    def is_valid(self) -> bool:
        """Return whether the result has a non-empty title."""

        return bool(self.title and self.title.strip())


@dataclass(slots=True)
class RecognitionError(Exception):
    """Structured failure reported by a recognition provider."""

    kind: ErrorKind
    provider: str
    mode: str
    message: str
    code: str | int | None = None
    retryable: bool = False

    def __str__(self) -> str:
        """Return a credential-safe diagnostic description."""

        suffix = f" (code={self.code})" if self.code is not None else ""
        return f"{self.provider}/{self.mode}: {self.message}{suffix}"
