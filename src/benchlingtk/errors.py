"""Error taxonomy: each error carries a stable code and the CLI exit code it maps to."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Process exit codes; 0 is success and each failure class has a distinct value."""

    SUCCESS = 0
    VALIDATION = 2
    AUTHENTICATION = 3
    ACCESS_OR_NOT_FOUND = 4
    CONFLICT = 5
    UNSUPPORTED = 6
    UPSTREAM = 7
    PARTIAL_OR_UNKNOWN = 8
    USAGE = 9


class BenchlingAgentError(Exception):
    """Base for all expected failures; subclasses set code and exit_code.

    details must be JSON-safe and free of secrets since it is emitted in the envelope.
    """

    code = "benchling_error"
    exit_code = ExitCode.UPSTREAM

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retryable = retryable


class ValidationError(BenchlingAgentError):
    """Caller input or a change specification is invalid."""

    code = "validation_error"
    exit_code = ExitCode.VALIDATION


class UsageError(BenchlingAgentError):
    """The command line does not parse: an unknown command or option, or a missing argument."""

    code = "usage_error"
    exit_code = ExitCode.USAGE


class AuthenticationError(BenchlingAgentError):
    """Credentials are missing, rejected or could not be exchanged for a token."""

    code = "authentication_error"
    exit_code = ExitCode.AUTHENTICATION


class AccessError(BenchlingAgentError):
    """Authenticated but not permitted to access the resource."""

    code = "access_denied"
    exit_code = ExitCode.ACCESS_OR_NOT_FOUND


class NotFoundError(BenchlingAgentError):
    """The requested resource does not exist; shares an exit code with AccessError."""

    code = "not_found"
    exit_code = ExitCode.ACCESS_OR_NOT_FOUND


class ConflictError(BenchlingAgentError):
    """A plan precondition failed, e.g. the entry changed since the plan was made."""

    code = "conflict"
    exit_code = ExitCode.CONFLICT


class ChecksumMismatchError(ConflictError):
    """Downloaded bytes do not match an expected checksum."""

    code = "checksum_mismatch"


class SizeMismatchError(ConflictError):
    """Downloaded bytes do not match an expected size."""

    code = "size_mismatch"


class UnsupportedError(BenchlingAgentError):
    """The operation or content type is not supported, or is unavailable on this tenant."""

    code = "unsupported_capability"
    exit_code = ExitCode.UNSUPPORTED


class UpstreamError(BenchlingAgentError):
    """The remote API failed or returned an unexpected response; may be retryable."""

    code = "upstream_error"
    exit_code = ExitCode.UPSTREAM


class ApiDriftError(UpstreamError):
    """Benchling rejected a method this client still sends. The published API has moved."""

    code = "api_drift"
    exit_code = ExitCode.UPSTREAM


class PartialCompletionError(BenchlingAgentError):
    """An apply finished only in part, or its outcome cannot be confirmed; inspect the journal."""

    code = "partial_or_unknown_completion"
    exit_code = ExitCode.PARTIAL_OR_UNKNOWN
