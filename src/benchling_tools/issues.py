"""Draft a GitHub issue for something the toolkit cannot do yet.

An agent that hits a limit explains it, asks the user whether to log a bug or a
feature request, and only after an explicit yes files the issue. This module
builds the draft. It never contacts Benchling or GitHub. The draft must cite
official Benchling documentation for the proposed solution, and it is rejected
when any field looks like a tenant URL, a private record ID, a secret, or an
email address.
"""

from __future__ import annotations

import platform
import re
from enum import Enum
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from . import __version__
from .errors import ValidationError
from .redact import registered_secrets

REPOSITORY = "jmamodeo/benchling-tools"
NEW_ISSUE_URL = f"https://github.com/{REPOSITORY}/issues/new"
# GitHub rejects very long prefilled URLs. Above this length the URL carries the
# title and label only, and the body is pasted or filed with `gh`.
MAX_URL_LENGTH = 8000

DOCS_HOSTS = frozenset({"docs.benchling.com", "help.benchling.com"})
# The API reference and the published OpenAPI files live under /api/ on these hosts.
API_REFERENCE_HOSTS = frozenset({"benchling.com", "www.benchling.com"})
# Placeholder tenants used in this repository's docs are not real tenants.
PLACEHOLDER_TENANT_HOSTS = frozenset({"your-tenant.benchling.com", "your-tenant-dev.benchling.com"})
PUBLIC_BENCHLING_HOSTS = DOCS_HOSTS | API_REFERENCE_HOSTS | PLACEHOLDER_TENANT_HOSTS

_MIN_QUOTE = 12
_MAX_QUOTE = 1000

_HOST = re.compile(r"\b((?:[a-z0-9-]+\.)+benchling\.com)\b", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Benchling API IDs are a short lowercase prefix, an underscore, and a random tail,
# for example an entry, document part, folder, or field definition.
_ENTITY_ID = re.compile(
    r"(?<![A-Za-z0-9_])[a-z][a-z0-9]{1,15}_(?=[A-Za-z0-9]*[A-Z0-9])[A-Za-z0-9]{6,}(?![A-Za-z0-9_])"
)
# Display IDs such as an entry's notebook number.
_DISPLAY_ID = re.compile(r"\b[A-Z]{2,6}-?\d{5,}\b")
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("benchling_api_key", re.compile(r"\bsk_[A-Za-z0-9]{16,}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:client_secret|secret|password|passwd|api_key|apikey|access_token|token)"
            r"\s*[:=]\s*['\"]?[^\s'\"]{6,}",
            re.IGNORECASE,
        ),
    ),
    (
        "long_random_token",
        re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b"),
    ),
)
_PLACEHOLDER_TAIL = re.compile(r"^(?:x+|X+|REPLACE_ME|EXAMPLE|example|abc123|ABC123)$")


class IssueKind(str, Enum):
    """Bug or feature request; each maps to a repository label."""

    BUG = "bug"
    FEATURE = "feature"


LABELS = {IssueKind.BUG: "bug", IssueKind.FEATURE: "enhancement"}


class Interface(str, Enum):
    """Where the user hit the limit."""

    CLI = "cli"
    MCP = "mcp"


class DocsCitation(BaseModel):
    """An official Benchling docs link and the line that supports the proposal."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(
        description="Link on docs.benchling.com, help.benchling.com, or the API reference."
    )
    quote: str = Field(description="The line on that page that shows the solution is possible.")


class IssueDraftRequest(BaseModel):
    """Structured fields for an issue draft."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: IssueKind
    summary: str = Field(min_length=8, max_length=120)
    attempted_action: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    actual: str = Field(min_length=1)
    interface: Interface
    command_or_tool: str = Field(min_length=1)
    description: str = Field(min_length=40)
    proposed_solution: str = Field(min_length=20)
    citations: list[DocsCitation] = Field(min_length=1)


def is_benchling_docs_url(url: str) -> bool:
    """True for an https link to Benchling's public docs, help center, or API reference."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        return False
    host = (parts.hostname or "").lower()
    if host in DOCS_HOSTS:
        return True
    return host in API_REFERENCE_HOSTS and parts.path.startswith("/api/")


def validate_citations(citations: list[DocsCitation]) -> None:
    """Require at least one citation, each an official docs URL with a real quote."""
    if not citations:
        raise ValidationError(
            "The proposed solution needs at least one Benchling docs citation",
            details={"field": "citations"},
        )
    problems: list[dict[str, Any]] = []
    for index, citation in enumerate(citations):
        if not is_benchling_docs_url(citation.url):
            problems.append(
                {
                    "index": index,
                    "reason": "url must be https on docs.benchling.com, help.benchling.com, "
                    "or the API reference under benchling.com/api/",
                }
            )
        quote = citation.quote.strip().strip('"').strip()
        if len(quote) < _MIN_QUOTE:
            problems.append(
                {"index": index, "reason": f"quote must be at least {_MIN_QUOTE} characters"}
            )
        elif len(quote) > _MAX_QUOTE:
            problems.append(
                {"index": index, "reason": f"quote must be at most {_MAX_QUOTE} characters"}
            )
    if problems:
        raise ValidationError(
            "A Benchling docs citation is invalid", details={"citations": problems}
        )


def _without_public_docs_urls(text: str) -> str:
    """Drop public Benchling docs links so their paths are not mistaken for IDs."""
    kept: list[str] = []
    for token in re.split(r"(\s+)", text):
        bare = token.strip("<>()[]`'\".,;")
        if bare.startswith("https://") and is_benchling_docs_url(bare):
            continue
        kept.append(token)
    return "".join(kept)


def _is_placeholder_id(value: str) -> bool:
    return bool(_PLACEHOLDER_TAIL.match(value.split("_", 1)[1]))


def find_sensitive(fields: dict[str, str]) -> list[dict[str, str]]:
    """List fields that look like they hold a tenant URL, private ID, secret, or email.

    Findings name the field and the kind of problem, never the matched value.
    """
    registered = registered_secrets()
    findings: list[dict[str, str]] = []

    def add(field: str, kind: str) -> None:
        item = {"field": field, "kind": kind}
        if item not in findings:
            findings.append(item)

    for field, raw in fields.items():
        for secret in registered:
            if secret in raw:
                add(field, "known_secret")
        for match in _HOST.finditer(raw):
            if match.group(1).lower() not in PUBLIC_BENCHLING_HOSTS:
                add(field, "tenant_url")
        text = _without_public_docs_urls(raw)
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                add(field, kind)
        if _EMAIL.search(text):
            add(field, "email_address")
        for match in _ENTITY_ID.finditer(text):
            if not _is_placeholder_id(match.group(0)):
                add(field, "entity_id")
        if _DISPLAY_ID.search(text):
            add(field, "display_id")
    return findings


def _block(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def render_body(request: IssueDraftRequest, environment: dict[str, str]) -> str:
    """Markdown body in the same section order as the repository issue forms."""
    kind = "Bug" if request.kind is IssueKind.BUG else "Feature request"
    sources = []
    for citation in request.citations:
        sources.append(f"- {citation.url}\n\n{_block(citation.quote)}")
    lines = [
        f"**Type:** {kind}",
        "",
        "## What the user tried",
        "",
        request.attempted_action,
        "",
        "## Expected behaviour",
        "",
        request.expected,
        "",
        "## Actual behaviour",
        "",
        request.actual,
        "",
        "## Environment",
        "",
        f"- benchling-tools version: {environment['toolkit_version']}",
        f"- Interface: {request.interface.value.upper()}",
        f"- Command or MCP tool: `{request.command_or_tool}`",
        f"- Python version: {environment['python_version']}",
        f"- OS: {environment['os']}",
        "",
        "## Description",
        "",
        request.description,
        "",
        "## Proposed solution",
        "",
        request.proposed_solution,
        "",
        "## Benchling documentation",
        "",
        "These official Benchling docs show the proposed solution is possible:",
        "",
        "\n\n".join(sources),
        "",
        "---",
        "",
        "_Drafted with the benchling-tools issue draft tool after the user agreed to log it. "
        "No tenant URLs, record IDs, secrets, or personal data are included._",
    ]
    return "\n".join(lines) + "\n"


def build_issue_draft(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate structured fields and return a ready-to-file title, body, and URL.

    Raises ValidationError when a field is missing, a citation is not an official
    Benchling docs link, or any field looks sensitive.
    """
    try:
        request = IssueDraftRequest.model_validate(spec)
    except PydanticValidationError as exc:
        problems = [
            {"field": ".".join(str(part) for part in error["loc"]), "reason": error["msg"]}
            for error in exc.errors(include_url=False, include_input=False)
        ]
        if any(problem["field"] == "citations" for problem in problems):
            raise ValidationError(
                "The proposed solution needs at least one Benchling docs citation",
                details={"problems": problems},
            ) from None
        raise ValidationError(
            "Issue draft fields are invalid", details={"problems": problems}
        ) from None
    validate_citations(request.citations)

    fields = {
        "summary": request.summary,
        "attempted_action": request.attempted_action,
        "expected": request.expected,
        "actual": request.actual,
        "command_or_tool": request.command_or_tool,
        "description": request.description,
        "proposed_solution": request.proposed_solution,
    }
    for index, citation in enumerate(request.citations):
        fields[f"citations[{index}].url"] = citation.url
        fields[f"citations[{index}].quote"] = citation.quote
    findings = find_sensitive(fields)
    if findings:
        raise ValidationError(
            "The draft looks like it contains tenant data, a secret, or personal data. "
            "Replace it with a placeholder such as your-tenant.benchling.com or etr_XXXX.",
            details={"findings": findings},
        )

    environment = {
        "toolkit_version": __version__,
        "python_version": platform.python_version(),
        "os": platform.system() or "unknown",
    }
    prefix = "Bug" if request.kind is IssueKind.BUG else "Feature"
    title = f"{prefix}: {request.summary}"
    body = render_body(request, environment)
    label = LABELS[request.kind]
    url = f"{NEW_ISSUE_URL}?{urlencode({'title': title, 'body': body, 'labels': label})}"
    body_in_url = len(url) <= MAX_URL_LENGTH
    if not body_in_url:
        url = f"{NEW_ISSUE_URL}?{urlencode({'title': title, 'labels': label})}"
    return {
        "repository": REPOSITORY,
        "kind": request.kind.value,
        "labels": [label],
        "title": title,
        "body": body,
        "new_issue_url": url,
        "body_in_url": body_in_url,
        "environment": environment,
        "filed": False,
        "next_step": (
            "Show the user this title and body. File it only after they say yes: open "
            "new_issue_url, or run `benchling-tools issue draft --spec FILE --file` with gh "
            "signed in."
        ),
    }
