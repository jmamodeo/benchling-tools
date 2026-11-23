"""Pydantic models shared by the CLI, MCP server and planner: the JSON envelope,
environment config, entry snapshots and change-plan/journal records."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_serializer, field_validator
from pydantic import ValidationError as ModelValidationError

from .errors import BenchlingAgentError, ValidationError
from .redact import redact, redact_text

T = TypeVar("T")


class ErrorDetail(BaseModel):
    """Serializable form of a BenchlingAgentError carried in a failed Envelope."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class Envelope(BaseModel, Generic[T]):
    """Stable JSON contract returned by both interfaces."""

    schema_version: Literal["1.0"] = "1.0"
    ok: bool
    data: T | None = None
    error: ErrorDetail | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, data: T, **meta: Any) -> Envelope[T]:
        """Build an ok=True envelope wrapping data; extra kwargs become meta."""
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(cls, error: BenchlingAgentError, **meta: Any) -> Envelope[dict[str, Any]]:
        """Build an ok=False envelope from an error; no data is included."""
        details = redact(error.details)
        if not isinstance(details, dict):
            details = {}
        redacted_meta = redact(meta)
        return cast(
            Envelope[dict[str, Any]],
            cls(
                ok=False,
                error=ErrorDetail(
                    code=error.code,
                    message=redact_text(error.message),
                    retryable=error.retryable,
                    details=details,
                ),
                meta=redacted_meta if isinstance(redacted_meta, dict) else {},
            ),
        )


class AuthMethod(str, Enum):
    """Supported ways of obtaining an API token."""

    CLIENT_CREDENTIALS = "client_credentials"


_API_SUFFIXES = ("/api/v2", "/api/v3")


def prepare_tenant_url(value: object) -> object:
    """Normalize a tenant URL string before it is parsed.

    A bare host gets https://. A trailing /api/v2 or /api/v3 and a trailing
    slash are removed. http:// is rejected with a message that does not echo
    the input, which may contain credentials.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        raise ValueError("Tenant URL is required")
    if text.lower().startswith("http://"):
        raise ValueError("Tenant URL must use https://, not http://")
    if "://" not in text:
        text = "https://" + text
    elif not text.lower().startswith("https://"):
        raise ValueError("Tenant URL must use https://")
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    folded = path.casefold()
    for suffix in _API_SUFFIXES:
        if folded.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit(("https", parts.netloc, path, parts.query, parts.fragment))


def explain_environment_error(exc: ModelValidationError, *, env: str) -> ValidationError:
    """Turn a model failure into a field-specific error that does not echo the input."""
    url_errors = [err for err in exc.errors() if "tenant_url" in err.get("loc", ())]
    id_errors = [err for err in exc.errors() if "client_id" in err.get("loc", ())]
    if url_errors:
        return ValidationError(
            _tenant_url_message(str(url_errors[0].get("msg", ""))),
            details={"env": env, "field": "tenant_url"},
        )
    if id_errors:
        return ValidationError("Invalid client ID", details={"env": env, "field": "client_id"})
    return ValidationError(
        "Invalid tenant URL. Expected https://host, a bare host, or a host ending in /api/v2 or /api/v3.",
        details={"env": env, "field": "tenant_url"},
    )


def _tenant_url_message(msg: str) -> str:
    folded = msg.casefold()
    if "not http://" in folded:
        return "Tenant URL must use https://, not http://."
    if "must use https" in folded:
        return "Tenant URL must use https://."
    if "credential" in folded:
        return "Invalid tenant URL: user and password are not allowed."
    if "only scheme and host" in folded or "path" in folded:
        return (
            "Invalid tenant URL: expected a host only. A trailing /api/v2 or /api/v3 "
            "is removed; other paths are not allowed."
        )
    if "required" in folded:
        return "Tenant URL is required."
    return "Invalid tenant URL. Expected https://host, a bare host, or a host ending in /api/v2 or /api/v3."


class Environment(BaseModel):
    """A named tenant configuration; holds no secrets, only the tenant URL and auth method."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    tenant_url: HttpUrl
    auth_method: AuthMethod
    client_id: str | None = None
    protected: bool = False

    @field_validator("tenant_url", mode="before")
    @classmethod
    def normalize_tenant_url(cls, value: object) -> object:
        """Accept a bare host and a trailing /api/v2 or /api/v3; reject http://."""
        return prepare_tenant_url(value)

    @field_validator("tenant_url")
    @classmethod
    def strip_url_path(cls, value: HttpUrl) -> HttpUrl:
        """Require a bare https://host URL; reject credentials, paths, queries and fragments."""
        if value.scheme != "https":
            raise ValueError("tenant_url must use HTTPS")
        if value.username or value.password:
            raise ValueError("tenant_url must not contain credentials")
        if value.path not in {"", "/"} or value.query or value.fragment:
            raise ValueError("tenant_url must contain only scheme and host")
        return value

    @field_validator("client_id")
    @classmethod
    def strip_client_id(cls, value: str | None) -> str | None:
        """Trim whitespace; blank values become None."""
        return value.strip() if value else None

    @field_serializer("tenant_url")
    def serialize_tenant_url(self, value: HttpUrl) -> str:
        """Display and persist the host without a trailing slash."""
        return str(value).rstrip("/")

    @property
    def base_url(self) -> str:
        """Tenant URL without a trailing slash."""
        return str(self.tenant_url).rstrip("/")


class TokenBundle(BaseModel):
    """An access token with its expiry."""

    access_token: str
    expires_at: datetime
    token_type: str = "Bearer"

    def usable(self, skew_seconds: int = 60) -> bool:
        """True if the token stays valid for more than skew_seconds; naive expiries are treated as UTC."""
        now = datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return (expires - now).total_seconds() > skew_seconds


class CompletenessIssue(BaseModel):
    """A reason part of an entry snapshot could not be retrieved."""

    scope: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class EntrySnapshot(BaseModel):
    """Structured entry content pinned to one version_id.

    completeness is "partial" when issues lists anything that was not retrieved.
    """

    model_config = ConfigDict(populate_by_name=True)

    entry: dict[str, Any]
    version_id: str
    parts: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    tables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    entry_schema: dict[str, Any] | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )
    fields: dict[str, Any] | None = None
    result_tables: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    result_retrieval: dict[str, Any] = Field(default_factory=dict)
    completeness: Literal["complete", "partial"] = "complete"
    issues: list[CompletenessIssue] = Field(default_factory=list)


class PlanFamily(str, Enum):
    """Kinds of change a plan can describe; each has its own planner."""

    ENTRY = "entry"
    DOCUMENT = "document"
    TABLE = "table"
    FIELDS = "fields"
    RESULTS = "results"
    CONTAINERS = "containers"


class PlanStatus(str, Enum):
    """Lifecycle of a plan; UNKNOWN means the outcome of an apply could not be confirmed."""

    PENDING = "pending"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ChangePlan(BaseModel):
    """Immutable preview of a change: before/after/diff plus preconditions checked at apply time.

    checksum guards against tampering with a stored plan.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str
    created_at: datetime
    env: str
    tenant_url: str
    entry_id: str | None = None
    family: PlanFamily
    request: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    diff: dict[str, Any]
    preconditions: dict[str, Any]
    guarantees: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checksum: str


class JournalEvent(BaseModel):
    """One timestamped stage transition recorded while applying a plan."""

    at: datetime
    stage: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ChangeJournal(BaseModel):
    """Append-only apply history for a plan, with its overall status and final result."""

    plan_id: str
    status: PlanStatus = PlanStatus.PENDING
    events: list[JournalEvent] = Field(default_factory=list)
    result: dict[str, Any] | None = None
