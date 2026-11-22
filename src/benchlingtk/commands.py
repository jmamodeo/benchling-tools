"""Command declarations shared by the CLI and the MCP server.

Each CLI command is written once, as an async function whose first argument is a
Call and whose other arguments are Typer options. The CLI runs it through Typer;
the MCP server turns the same declaration into one tool with the same parameters,
help text, and code path. Nothing about a tool is written by hand in the server.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_type_hints

from typer.models import ParameterInfo

from .errors import ValidationError

if TYPE_CHECKING:
    from .runtime import Runtime

TOOL_ATTR = "__benchlingtk_command__"
CLI_ONLY_ATTR = "__benchlingtk_cli_only__"
REQUIRED = inspect.Parameter.empty


class Call:
    """One invocation of a command, from the CLI or from an MCP tool.

    The runtime is opened on first use, so a command can validate its input
    before the keychain or the network is touched. ``meta`` is added to the
    envelope after the env and tenant of the runtime, when one was opened.
    """

    surface = "cli"

    def __init__(self, env: str | None) -> None:
        self.env = env
        self.meta: dict[str, Any] = {}
        self._runtime: Runtime | None = None

    @property
    def runtime(self) -> Runtime:
        """The runtime for the selected environment, opened on first use."""
        if self._runtime is None:
            self._runtime = self._open()
        return self._runtime

    def _open(self) -> Runtime:
        raise NotImplementedError

    @property
    def writable_envs(self) -> frozenset[str] | None:
        """Environments MCP writes are enabled for; None outside the MCP server."""
        return None

    def authorize_apply(self, confirm_env: str | None) -> None:
        """Raise unless this surface allows applying a plan in the selected env."""
        raise NotImplementedError

    def envelope_meta(self) -> dict[str, Any]:
        """Envelope metadata: the resolved env and tenant, then command meta."""
        meta: dict[str, Any] = {}
        if self._runtime is not None:
            meta = {
                "env": self._runtime.environment.name,
                "tenant_url": self._runtime.environment.base_url,
            }
        meta.update(self.meta)
        return meta

    async def close(self) -> None:
        """Release what this call opened."""


@dataclass(frozen=True)
class McpParam:
    """How one CLI parameter is presented as an MCP tool argument instead.

    ``convert`` turns the MCP value into what the command expects.
    """

    annotation: Any
    description: str
    convert: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class Param:
    """One command parameter, read from its Typer declaration."""

    name: str
    annotation: Any
    default: Any
    help: str
    min: float | None = None
    max: float | None = None

    def check_range(self, value: Any) -> None:
        """Apply the CLI's min and max to a value that did not come through click."""
        if not isinstance(value, int | float) or isinstance(value, bool):
            return
        low = self.min is not None and value < self.min
        high = self.max is not None and value > self.max
        if low or high:
            raise ValidationError(
                f"{self.name} {_range_text(self.min, self.max)}",
                details={"parameter": self.name, "received": value},
            )


def _range_text(low: float | None, high: float | None) -> str:
    if low is not None and high is not None:
        return f"must be from {low} to {high}"
    if low is not None:
        return f"must be at least {low}"
    return f"must be at most {high}"


@dataclass(frozen=True)
class Command:
    """A declared command: its implementation, parameters, and tool hints."""

    function: Callable[..., Awaitable[Any]]
    doc: str
    params: tuple[Param, ...]
    read_only: bool
    destructive: bool = False
    needs_env: bool = True
    cli_only: frozenset[str] = frozenset()
    mcp_params: Mapping[str, McpParam] = field(default_factory=dict)

    def cli_signature(self, context_type: type) -> inspect.Signature:
        """Typer signature: the click context, then every declared parameter."""
        signature = inspect.signature(self.function)
        hints = get_type_hints(self.function)
        params = [
            inspect.Parameter(
                "ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=context_type
            )
        ]
        for param in list(signature.parameters.values())[1:]:
            params.append(param.replace(annotation=hints[param.name]))
        return inspect.Signature(params, return_annotation=None)


def declare(
    function: Callable[..., Awaitable[Any]],
    *,
    read_only: bool,
    destructive: bool = False,
    needs_env: bool = True,
    cli_only: Iterable[str] = (),
    mcp_params: Mapping[str, McpParam] | None = None,
) -> Command:
    """Read a command's parameters from its Typer options. Every one needs help text."""
    hints = get_type_hints(function)
    params: list[Param] = []
    for param in list(inspect.signature(function).parameters.values())[1:]:
        info = param.default
        if not isinstance(info, ParameterInfo):
            raise TypeError(f"{function.__name__}.{param.name} must use typer.Option or Argument")
        help_text = (info.help or "").strip()
        overridden = (mcp_params or {}).get(param.name)
        if not help_text and overridden is None:
            raise TypeError(f"{function.__name__}.{param.name} needs help text")
        params.append(
            Param(
                name=param.name,
                annotation=hints[param.name],
                default=REQUIRED if info.default is ... else info.default,
                help=help_text,
                min=getattr(info, "min", None),
                max=getattr(info, "max", None),
            )
        )
    names = {param.name for param in params}
    unknown = (set(cli_only) | set(mcp_params or {})) - names
    if unknown:
        raise TypeError(f"{function.__name__} has no parameters {sorted(unknown)}")
    return Command(
        function=function,
        doc=inspect.getdoc(function) or "",
        params=tuple(params),
        read_only=read_only,
        destructive=destructive,
        needs_env=needs_env,
        cli_only=frozenset(cli_only),
        mcp_params=dict(mcp_params or {}),
    )


def surface_text(doc: str, surface: str) -> str:
    """Keep the paragraphs for one surface. "CLI:" and "MCP:" paragraphs are surface-only."""
    kept: list[str] = []
    for paragraph in re.split(r"\n\s*\n", doc.strip()):
        prefix, _, rest = paragraph.partition(": ")
        if prefix in ("CLI", "MCP"):
            if prefix.lower() != surface:
                continue
            paragraph = rest
        kept.append(paragraph)
    return "\n\n".join(kept)


_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def rename_flags(text: str, flags: Mapping[str, str]) -> str:
    """Replace this command's ``--flag`` names with the matching tool argument names."""
    return _FLAG.sub(lambda match: flags.get(match.group(0), match.group(0)), text)
