"""Typer CLI: every command prints a JSON Envelope on stdout and exits with an ExitCode."""

from __future__ import annotations

import asyncio
import getpass
import inspect
import io
import json
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from pydantic import WithJsonSchema
from typer.core import TyperGroup

from . import __version__
from .blob_download import download_blob
from .change_spec import CHANGE_SPECIFICATION_SCHEMA, parse_change_specification
from .commands import CLI_ONLY_ATTR, TOOL_ATTR, Call, Command, McpParam, declare, surface_text
from .credentials import build_environment, collect_status, login, login_fix_values, logout
from .environment import (
    ENV_NAME,
    EnvironmentConfigStore,
    check_apply_allowed,
    invalid_environment_name,
)
from .errors import (
    AuthenticationError,
    BenchlingAgentError,
    ExitCode,
    UnsupportedError,
    UpstreamError,
    UsageError,
    ValidationError,
)
from .export import export_dataset
from .issues import REPOSITORY, build_issue_draft
from .keychain import Keychain, bind_login_fix
from .models import Envelope, PlanFamily
from .redact import redact_text
from .runtime import Runtime
from .services import part_content
from .skill import find_skill_md
from .stale import annotate_rows, check_result_tables

# Typer may vendor click, so take click's exception classes from one typer exports.
_CLICK_USAGE_ERROR: Any = typer.BadParameter.__mro__[1]
_CLICK_EXCEPTION: Any = _CLICK_USAGE_ERROR.__mro__[1]


class RootGroup(TyperGroup):
    """The root command. A click usage error prints a usage_error envelope on stdout.

    An unknown command or option, a missing or invalid argument, and a missing
    subcommand exit with ExitCode.USAGE, not the validation code. A bare
    ``benchlingtk`` still prints the help, and exits with the same code.
    """

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        if not standalone_mode:
            return super().main(args, prog_name, complete_var, False, windows_expand_args, **extra)
        try:
            code = super().main(args, prog_name, complete_var, False, windows_expand_args, **extra)
        except _CLICK_USAGE_ERROR as exc:
            code = _usage_failure(exc)
        except _CLICK_EXCEPTION as exc:
            exc.show()
            code = exc.exit_code
        except typer.Abort:
            typer.echo("Aborted!", err=True)
            code = 1
        sys.exit(code if isinstance(code, int) else 0)


def _usage_failure(exc: Any) -> int:
    """Print the usage_error envelope for a click usage error and return its exit code."""
    ctx = exc.ctx
    if type(exc).__name__ == "NoArgsIsHelpError" and ctx is not None:
        typer.echo(ctx.get_help())
        return int(ExitCode.USAGE)
    details: dict[str, Any] = {}
    if ctx is not None:
        details = {
            "command": ctx.command_path,
            "usage": ctx.get_usage(),
            "help": f"{ctx.command_path} --help",
        }
        if ctx.parent is None and getattr(exc, "option_name", None) in ("--env", "-e"):
            details["hint"] = "--env is a command option: benchlingtk entry get REF --env NAME"
    error = UsageError(exc.format_message(), details=details)
    _emit(Envelope.failure(error))
    return int(error.exit_code)


app = typer.Typer(
    name="benchlingtk",
    cls=RootGroup,
    help="Safe, auditable tools for structured Benchling entry data.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
auth_app = typer.Typer(help="Log in, log out, and inspect saved environments.")
entry_app = typer.Typer(help="Resolve, list, fetch, and inspect entries.")
entry_fields_app = typer.Typer(help="Read entry schema fields.")
template_app = typer.Typer(help="List and inspect entry templates.")
folder_app = typer.Typer(help="Read notebook folders.")
section_app = typer.Typer(help="Read entry sections.")
part_app = typer.Typer(help="Read document parts.")
table_app = typer.Typer(help="Read unstructured tables.")
schema_app = typer.Typer(help="List and read entry, result, and run schemas.")
results_app = typer.Typer(help="Read result records and results-table rows.")
blob_app = typer.Typer(help="Read blob or attachment metadata and download files.")
container_app = typer.Typer(help="Read and search inventory containers.")
container_schema_app = typer.Typer(help="List container schemas.")
changes_app = typer.Typer(help="Preview and explicitly apply safe changes.")
mcp_app = typer.Typer(help="Run the local MCP server.")
skill_app = typer.Typer(help="Locate and print the SKILL.md installed with this package.")
issue_app = typer.Typer(
    help="Draft a bug report or feature request for this toolkit. Drafts are local by default."
)

app.add_typer(auth_app, name="auth")
app.add_typer(entry_app, name="entry")
entry_app.add_typer(entry_fields_app, name="fields")
app.add_typer(template_app, name="template")
app.add_typer(folder_app, name="folder")
app.add_typer(section_app, name="section")
app.add_typer(part_app, name="part")
app.add_typer(table_app, name="table")
app.add_typer(schema_app, name="schema")
app.add_typer(results_app, name="results")
app.add_typer(blob_app, name="blob")
app.add_typer(container_app, name="container")
container_app.add_typer(container_schema_app, name="schema")
app.add_typer(changes_app, name="changes")
app.add_typer(mcp_app, name="mcp")
app.add_typer(issue_app, name="issue")
app.add_typer(skill_app, name="skill")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"benchlingtk {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Safe, auditable tools for structured Benchling entry data."""


def _env_on_command_line(ctx: typer.Context, env: str | None) -> bool:
    """True only when --env was typed, not filled from a default or a variable."""
    # Typer may vendor click, so compare the ParameterSource member by name.
    source = getattr(ctx.get_parameter_source("env"), "name", None)
    return bool(env) and source == "COMMANDLINE"


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _emit(envelope: Envelope[Any]) -> None:
    typer.echo(_json(envelope))


class CliCall(Call):
    """A command run from the command line. The runtime is built from --env."""

    def __init__(self, env: str | None, *, env_on_command_line: bool = False) -> None:
        super().__init__(env)
        self.env_on_command_line = env_on_command_line

    def _open(self) -> Runtime:
        return Runtime(self.env)

    def authorize_apply(self, confirm_env: str | None) -> None:
        """Refuse a protected env unless --env and --confirm-env both name it.

        The saved environment is checked before the keychain or the network is
        touched, then the runtime's environment is checked again.
        """
        environment = EnvironmentConfigStore().resolve(self.env)
        check_apply_allowed(
            environment,
            env_on_command_line=self.env_on_command_line,
            confirm_env=confirm_env,
        )
        check_apply_allowed(
            self.runtime.environment,
            env_on_command_line=self.env_on_command_line,
            confirm_env=confirm_env,
        )

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()


def _execute(call: Call, declared: Command, arguments: dict[str, Any]) -> None:
    """Run a declared command and print its envelope; failures exit with their code."""

    async def run() -> Any:
        try:
            return await declared.function(call, **arguments)
        finally:
            await call.close()

    try:
        result = asyncio.run(run())
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc, **call.envelope_meta()))
        raise typer.Exit(int(exc.exit_code)) from None
    except Exception as exc:
        error = UpstreamError("Unexpected local failure", details={"type": type(exc).__name__})
        _emit(Envelope.failure(error, **call.envelope_meta()))
        raise typer.Exit(int(ExitCode.UPSTREAM)) from None
    _emit(Envelope.success(result, **call.envelope_meta()))


_F = TypeVar("_F", bound=Callable[..., Any])

_ENV_PARAMETER = inspect.Parameter(
    "env",
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    annotation=str | None,
    default=typer.Option(None, "--env", "-e", help="Environment name."),
)


def command(
    *,
    read_only: bool,
    destructive: bool = False,
    needs_env: bool = True,
    cli_only: tuple[str, ...] = (),
    mcp_params: dict[str, McpParam] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., None]]:
    """Declare a command once for the CLI and its MCP tool.

    The decorated function is async and takes a Call first. Typer gets a sync
    callback with the same options; the MCP server reads the declaration.
    read_only and destructive become the tool's hints. needs_env adds the
    required MCP env argument. cli_only parameters are not offered as tool
    arguments, and mcp_params present a parameter differently to MCP.
    """

    def decorate(function: Callable[..., Awaitable[Any]]) -> Callable[..., None]:
        declared = declare(
            function,
            read_only=read_only,
            destructive=destructive,
            needs_env=needs_env,
            cli_only=cli_only,
            mcp_params=mcp_params,
        )

        def callback(ctx: typer.Context, **arguments: Any) -> None:
            env = arguments.pop("env") if needs_env else None
            call = CliCall(env, env_on_command_line=_env_on_command_line(ctx, env))
            _execute(call, declared, arguments)

        signature = declared.cli_signature(typer.Context)
        if needs_env:
            params = list(signature.parameters.values())
            params.insert(1, _ENV_PARAMETER)
            signature = signature.replace(parameters=params)
        callback.__name__ = function.__name__
        callback.__qualname__ = function.__qualname__
        callback.__doc__ = surface_text(declared.doc, "cli")
        callback.__annotations__ = {
            name: param.annotation for name, param in signature.parameters.items()
        }
        callback.__signature__ = signature  # type: ignore[attr-defined]
        setattr(callback, TOOL_ATTR, declared)
        return callback

    return decorate


def cli_only(reason: str) -> Callable[[_F], _F]:
    """Mark a command that has no MCP tool, with the reason."""

    def mark(function: _F) -> _F:
        setattr(function, CLI_ONLY_ATTR, reason)
        return function

    return mark


def _prompt(label: str, *, tty: bool) -> str:
    """Read one non-secret line. --secret-stdin keeps stdin free for the secret."""
    if tty:
        line = _read_terminal(label)
    else:
        typer.echo(f"{label}: ", nl=False, err=True)
        line = sys.stdin.readline()
    if not line.strip():
        raise ValidationError(f"{label} is required")
    return line.strip()


def _read_terminal(label: str) -> str:
    """Prompt on the controlling terminal so a piped secret is not consumed.

    Builtin open(..., "r+") is rejected on Linux and macOS: a terminal is not
    seekable, and Python's text wrapper tries to seek. os.open plus FileIO
    is the mode the standard library uses for /dev/tty.
    """
    prompt = f"{label}: "
    try:
        if os.name == "nt":
            with (
                open("CONOUT$", "w", encoding="utf-8") as output,
                open("CONIN$", encoding="utf-8") as incoming,
            ):
                output.write(prompt)
                output.flush()
                return incoming.readline()
        return _read_posix_tty("/dev/tty", prompt)
    except OSError as exc:
        raise ValidationError(
            "A terminal is required for the tenant URL and client ID when --secret-stdin is set"
        ) from exc


def _read_posix_tty(path: str, prompt: str) -> str:
    """Read one line from a terminal device. ``path`` is /dev/tty in production."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    stream = io.TextIOWrapper(
        io.FileIO(fd, "w+"),
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
        write_through=True,
    )
    try:
        stream.write(prompt)
        stream.flush()
        return stream.readline()
    finally:
        stream.close()


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _controlling_tty_available() -> bool:
    if os.name == "nt":
        try:
            with open("CONIN$", encoding="utf-8"):
                return True
        except OSError:
            return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def _prompts_available(*, secret_on_stdin: bool) -> bool:
    """True when a human can be prompted. A piped secret must not use stdin for that."""
    if secret_on_stdin:
        return _controlling_tty_available()
    return _stdin_is_tty() or _controlling_tty_available()


def _noninteractive_login_error(*, url: bool, client: bool, secret: bool) -> ValidationError:
    flags: list[str] = []
    if url:
        flags.append("--tenant-url")
    if client:
        flags.append("--client-id")
    if secret:
        flags.append("--secret-stdin")
    if len(flags) == 1:
        listed = flags[0]
    elif len(flags) == 2:
        listed = f"{flags[0]} and {flags[1]}"
    else:
        listed = ", ".join(flags[:-1]) + ", and " + flags[-1]
    return ValidationError(
        f"No terminal is available to prompt for login. Pass {listed}.",
        details={"missing_flags": flags},
    )


def _read_secret(secret_stdin: bool) -> str:
    """Read the client secret from stdin or a hidden prompt. Never from argv."""
    if secret_stdin:
        value = sys.stdin.read()
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if value.strip() == "":
            raise ValidationError("No client secret was received on stdin")
        return value
    try:
        secret = getpass.getpass("Client secret (not echoed): ")
    except EOFError as exc:
        raise ValidationError("Client secret is required") from exc
    if secret == "":
        raise ValidationError("Client secret is required")
    return secret


def _auth_failure(exc: BenchlingAgentError) -> NoReturn:
    _emit(Envelope.failure(exc))
    raise typer.Exit(int(exc.exit_code)) from None


def _require_env_name(name: str | None, store: EnvironmentConfigStore) -> str:
    names = store.configured_names() if store.environments_path.exists() else []
    if not name:
        raise ValidationError(
            "An environment is required; pass --env NAME",
            details={"available_envs": names},
        )
    if not ENV_NAME.match(name):
        raise invalid_environment_name(name, available=names)
    return name


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@auth_app.command("login")
@cli_only("Reads a client secret; only a person at a terminal logs in.")
def auth_login(
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to save."),
    tenant_url: str | None = typer.Option(
        None,
        "--tenant-url",
        help="Tenant URL or bare host. https:// is added when omitted; a trailing /api/v2 or /api/v3 is removed.",
    ),
    client_id: str | None = typer.Option(None, "--client-id", help="App client ID."),
    secret_stdin: bool = typer.Option(
        False,
        "--secret-stdin",
        help="Read the client secret from stdin. It is never accepted as an argument.",
    ),
    protected: bool | None = typer.Option(
        None,
        "--protected/--unprotected",
        help=(
            "Mark the environment protected: changes apply then needs --confirm-env NAME. "
            "Omit to keep the saved setting (new environments are not protected)."
        ),
    ),
) -> None:
    """Save a verified client secret for one environment.

    With --tenant-url, --client-id, and --secret-stdin, login does not need a
    terminal. Otherwise the missing values are prompted when a terminal exists.
    The secret is never accepted as an argument. Nothing is saved unless a
    client-credentials token request succeeds.
    """
    store = EnvironmentConfigStore()
    try:
        name = _require_env_name(env, store)
        url = _blank_to_none(tenant_url)
        client = _blank_to_none(client_id)
        fix_url, fix_client = login_fix_values(store, name, url, client)
        with bind_login_fix(env=name, tenant_url=fix_url, client_id=fix_client):
            Keychain().require()
            missing_url = url is None
            missing_client = client is None
            missing_secret = not secret_stdin
            if (missing_url or missing_client or missing_secret) and not _prompts_available(
                secret_on_stdin=secret_stdin
            ):
                raise _noninteractive_login_error(
                    url=missing_url, client=missing_client, secret=missing_secret
                )
            use_terminal = secret_stdin or not _stdin_is_tty()
            if missing_url:
                url = _prompt("Tenant URL", tty=use_terminal)
            if missing_client:
                client = _prompt("Client ID", tty=use_terminal)
            assert url is not None and client is not None
            build_environment(name, url, client)
            secret = _read_secret(secret_stdin)
            environment = login(
                store,
                Keychain(),
                name=name,
                tenant_url=url,
                client_id=client,
                secret=secret,
                protected=protected,
            )
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    _emit(
        Envelope.success(
            {
                "env": environment.name,
                "tenant_url": environment.base_url,
                "client_id": environment.client_id,
                "credential_source": "keyring",
                "protected": environment.protected,
                "saved": True,
            },
            env=environment.name,
            tenant_url=environment.base_url,
        )
    )


@auth_app.command("logout")
@cli_only("Deletes a saved environment and its secret; a person decides that.")
def auth_logout(
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to remove."),
) -> None:
    """Remove an environment from environments.json and delete its keychain secret."""
    store = EnvironmentConfigStore()
    try:
        name = _require_env_name(env, store)
        logout(store, Keychain(), name)
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    _emit(Envelope.success({"env": name, "removed": True}, env=name))


def _set_protection(env: str | None, protected: bool) -> None:
    store = EnvironmentConfigStore()
    try:
        name = _require_env_name(env, store)
        environment = store.set_protected(name, protected)
    except BenchlingAgentError as exc:
        _auth_failure(exc)
    _emit(
        Envelope.success(
            {"env": environment.name, "protected": environment.protected},
            env=environment.name,
            tenant_url=environment.base_url,
        )
    )


@auth_app.command("protect")
@cli_only("Protection is set by a person, never by the agent it restricts.")
def auth_protect(
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to protect."),
) -> None:
    """Mark a saved environment protected.

    `changes apply` then refuses it unless --env names it on the command line and
    --confirm-env repeats the name. Reads are unaffected. No secret is read.
    """
    _set_protection(env, True)


@auth_app.command("unprotect")
@cli_only("Protection is set by a person, never by the agent it restricts.")
def auth_unprotect(
    env: str | None = typer.Option(None, "--env", "-e", help="Environment name to unprotect."),
) -> None:
    """Remove the protected mark from a saved environment."""
    _set_protection(env, False)


@auth_app.command("status")
@command(read_only=True, needs_env=False)
async def auth_status(
    call: Call,
    env: str | None = typer.Option(
        None, "--env", "-e", help="Limit the report to one environment."
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Request an access token and report the result. The token is not printed or saved.",
    ),
) -> dict[str, Any]:
    """Show configured environments and whether a keychain secret is present.

    The secret is never printed. Without --env, every saved environment is listed.
    --check performs a live token request and does not print or save the token.

    MCP: Call this first to see the environment names every other tool takes.
    Each environment also reports writes_allowed: whether this server was started
    with --allow-writes for it.
    """
    report = await asyncio.to_thread(
        collect_status, EnvironmentConfigStore(), Keychain(), name=env, check=check
    )
    writable = call.writable_envs
    if writable is not None:
        for name, row in report.environments.items():
            row["writes_allowed"] = name in writable
    payload = {"environments": report.environments}
    if report.failed_envs:
        messages = [
            str(report.environments[name]["token_check"]["message"]) for name in report.failed_envs
        ]
        message = (
            messages[0] if len(messages) == 1 else "Token check failed: " + "; ".join(messages)
        )
        raise AuthenticationError(
            message,
            details={"environments": payload["environments"], "failed_envs": report.failed_envs},
        )
    if env and env in report.environments:
        call.meta.update(env=env, tenant_url=report.environments[env]["tenant_url"])
    return payload


def _doctor_failure_message(result: dict[str, Any]) -> str:
    failed = [check for check in result.get("checks", []) if not check.get("ok")]
    if not failed:
        return "Doctor checks failed"
    summary = str(failed[0].get("error") or "Doctor checks failed")
    hint = str(failed[0].get("hint") or "")
    if hint and hint not in summary:
        return f"{summary}. {hint}"
    return summary


@app.command()
@command(read_only=True)
async def doctor(call: Call) -> Any:
    """Check configuration, authentication, API connectivity, and supported edit types.

    A failed check is an authentication_error whose details hold every check.
    """
    result = await call.runtime.service.doctor()
    if isinstance(result, dict) and result.get("ok") is False:
        raise AuthenticationError(_doctor_failure_message(result), details=result)
    return result


_ENTRY_REF_HELP = "Entry ID (etr_…), display ID, or same-tenant entry URL."
_CURSOR_HELP = "next_token from the previous page. Omit it to read the first page."
_LIMIT_HELP = "Page size, from 1 to 100."
_PART_ID_HELP = "v3_document_part id (docpart_…), as listed by entry inspect."


def _pagination(offset: int, returned: int, total: int) -> dict[str, Any]:
    end = offset + returned
    return {
        "offset": offset,
        "returned": returned,
        "total": total,
        "next_offset": end if end < total else None,
    }


def _page_table(table: dict[str, Any], row_offset: int, row_limit: int) -> dict[str, Any]:
    content = part_content(table)
    rows = content.get("rows", [])
    page = rows[row_offset : row_offset + row_limit]
    content["rows"] = page
    table["pagination"] = _pagination(row_offset, len(page), len(rows))
    return table


@entry_app.command("resolve")
@command(read_only=True)
async def entry_resolve(
    call: Call, reference: str = typer.Argument(..., help=_ENTRY_REF_HELP)
) -> Any:
    """Resolve an entry ID, display ID, or same-tenant URL to its canonical entry.

    web_url is the entry's link in the Benchling web app, or null when Benchling
    returns none.
    """
    return await call.runtime.service.resolve_entry(reference)


@entry_app.command("get")
@command(read_only=True)
async def entry_get(
    call: Call,
    reference: str = typer.Argument(..., help=_ENTRY_REF_HELP),
    full: bool = typer.Option(
        False,
        "--full",
        help=(
            "Return a paged snapshot of parts, tables, and results. "
            "Without it, return entry metadata only."
        ),
    ),
    part_offset: int = typer.Option(
        0, "--part-offset", min=0, help="Document parts to skip with --full. Zero is the first."
    ),
    part_limit: int = typer.Option(
        100, "--part-limit", min=1, help="Most document parts to return with --full."
    ),
    result_offset: int = typer.Option(
        0, "--result-offset", min=0, help="Result records to skip with --full. Zero is the first."
    ),
    result_limit: int = typer.Option(
        100, "--result-limit", min=1, help="Most result records to return with --full."
    ),
    table_row_offset: int = typer.Option(
        0, "--table-row-offset", min=0, help="Rows to skip in each unstructured table on the page."
    ),
    table_row_limit: int = typer.Option(
        250, "--table-row-limit", min=1, help="Most rows to return from each unstructured table."
    ),
) -> Any:
    """Fetch entry metadata, or with --full a paged snapshot of its structured content.

    web_url is the entry's link in the Benchling web app (V3 webUrl), or null.
    """
    data = await call.runtime.service.get_entry(reference, full=full)
    if not full:
        return data
    all_parts = data["parts"]
    all_results = data["results"]
    parts = all_parts[part_offset : part_offset + part_limit]
    results = all_results[result_offset : result_offset + result_limit]
    part_ids = {part.get("id") for part in parts}
    data["parts"] = parts
    data["tables"] = {
        key: _page_table(value, table_row_offset, table_row_limit)
        for key, value in data["tables"].items()
        if key in part_ids
    }
    data["results"] = results
    data["pagination"] = {
        "parts": _pagination(part_offset, len(parts), len(all_parts)),
        "results": _pagination(result_offset, len(results), len(all_results)),
        "table_rows": {"offset": table_row_offset, "limit_per_table": table_row_limit},
    }
    return data


@entry_app.command("bulk-get")
@command(read_only=True)
async def entry_bulk_get(
    call: Call,
    entry_ids: list[str] | None = typer.Option(
        None,
        "--id",
        help="Entry ID (etr_…). Repeat for several. Use this or --display-id, not both.",
    ),
    display_ids: list[str] | None = typer.Option(
        None,
        "--display-id",
        help="Entry display ID. Repeat for several. Use this or --id, not both.",
    ),
) -> Any:
    """Fetch entries by ID or display ID, chunking requests at 100 identifiers.

    Each item adds web_url, the entry's Benchling web-app link (V2 webURL), or null.
    """
    return await call.runtime.service.get_entries(entry_ids=entry_ids, display_ids=display_ids)


@entry_app.command("inspect")
@command(read_only=True)
async def entry_inspect(
    call: Call, reference: str = typer.Argument(..., help=_ENTRY_REF_HELP)
) -> Any:
    """Summarize an entry's sections, part IDs, schemas, and tables without full content.

    Part IDs are v3_document_part ids (docpart_…), the ids section get, part get,
    table get, and results rows take. They are not the v2_structured_table ids
    (strtbl_…) that results tables returns.
    """
    return await call.runtime.service.inspect_entry(reference)


@template_app.command("get")
@command(read_only=True)
async def template_get(
    call: Call, template_id: str = typer.Argument(..., help="Entry template ID (tmpl_…).")
) -> Any:
    """Get an entry template and summarize its structure (from V2; V3 has metadata only)."""
    return await call.runtime.service.get_template(template_id)


@template_app.command("list")
@command(read_only=True)
async def template_list(
    call: Call,
    name: str | None = typer.Option(None, "--name", help="Exact entry-template name."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help=_LIMIT_HELP),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """List one page of entry templates. Pass next_token as --cursor to continue."""
    return await call.runtime.service.list_templates(name=name, limit=limit, cursor=cursor)


@folder_app.command("get")
@command(read_only=True)
async def folder_get(
    call: Call, folder_id: str = typer.Argument(..., help="Folder ID (lib_…).")
) -> Any:
    """Get a folder by folder ID (lib_…)."""
    return await call.runtime.service.get_folder(folder_id)


@entry_app.command("list")
@command(read_only=True)
async def entry_list(
    call: Call,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project ID (src_…). Omit to list entries from every readable project.",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help="Folder ID (lib_…). The query then uses V2, because V3 entry listing ignores folder.",
    ),
    schema_id: str | None = typer.Option(
        None, "--schema", help="Entry schema ID. Omit to include every schema."
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Exact entry name, not a substring. A comma in the name uses the V2 name parameter.",
    ),
    modified_after: str | None = typer.Option(
        None,
        "--modified-after",
        help=(
            "Inclusive lower bound. A bare YYYY-MM-DD is the start of that UTC day; "
            "a timestamp must include a time zone."
        ),
    ),
    modified_before: str | None = typer.Option(
        None,
        "--modified-before",
        help=(
            "Inclusive upper bound. A bare YYYY-MM-DD is the end of that UTC day; "
            "a timestamp must include a time zone."
        ),
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help=_LIMIT_HELP),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """List one page of entries. --folder uses V2; every filter is applied by the server.

    Each row includes web_url, the entry's Benchling web-app link, or null if none.
    """
    return await call.runtime.service.list_entries(
        project=project,
        folder=folder,
        schema_id=schema_id,
        name=name,
        modified_after=modified_after,
        modified_before=modified_before,
        limit=limit,
        cursor=cursor,
    )


@entry_fields_app.command("get")
@command(read_only=True)
async def entry_fields_get(
    call: Call, reference: str = typer.Argument(..., help=_ENTRY_REF_HELP)
) -> Any:
    """Get the schema field values of an entry, by system name."""
    return await call.runtime.service.get_entry_fields(reference)


@section_app.command("get")
@command(read_only=True)
async def section_get(
    call: Call,
    reference: str = typer.Argument(..., help=_ENTRY_REF_HELP),
    section: str = typer.Argument(
        ..., help="Section part id (docpart_…) from entry inspect, or a unique section title."
    ),
    part_offset: int = typer.Option(
        0, "--part-offset", min=0, help="Parts in the section to skip. Zero is the first."
    ),
    part_limit: int = typer.Option(
        100, "--part-limit", min=1, help="Most parts in the section to return."
    ),
    table_row_offset: int = typer.Option(
        0, "--table-row-offset", min=0, help="Rows to skip in each table in the section."
    ),
    table_row_limit: int = typer.Option(
        250, "--table-row-limit", min=1, help="Most rows to return from each table in the section."
    ),
) -> Any:
    """Get one section of an entry by stable part ID or a unique section title, paged."""
    data = await call.runtime.service.get_section(reference, section)
    parts = data["parts"]
    page = parts[part_offset : part_offset + part_limit]
    part_ids = {part.get("id") for part in page}
    data["parts"] = page
    data["tables"] = {
        key: _page_table(value, table_row_offset, table_row_limit)
        for key, value in data["tables"].items()
        if key in part_ids
    }
    data["pagination"] = {
        "parts": _pagination(part_offset, len(page), len(parts)),
        "table_rows": {"offset": table_row_offset, "limit_per_table": table_row_limit},
    }
    return data


@part_app.command("get")
@command(read_only=True)
async def part_get(
    call: Call,
    reference: str = typer.Argument(..., help=_ENTRY_REF_HELP),
    part_id: str = typer.Argument(..., help=_PART_ID_HELP),
    row_offset: int = typer.Option(
        0, "--row-offset", min=0, help="Table rows to skip when the part is a table."
    ),
    row_limit: int = typer.Option(
        250, "--row-limit", min=1, help="Most table rows to return when the part is a table."
    ),
) -> Any:
    """Get one document part of an entry by part ID, with its available detail."""
    data = await call.runtime.service.get_part(reference, part_id)
    if data.get("detail"):
        data["detail"] = _page_table(data["detail"], row_offset, row_limit)
    return data


@table_app.command("get")
@command(read_only=True)
async def table_get(
    call: Call,
    part_id: str = typer.Argument(
        ...,
        help=(
            "v3_document_part id (docpart_…) of an unstructured table, from entry inspect. "
            "Not a strtbl_ id."
        ),
    ),
    row_offset: int = typer.Option(
        0, "--row-offset", min=0, help="Table rows to skip. Zero is the first row."
    ),
    row_limit: int = typer.Option(250, "--row-limit", min=1, help="Most table rows to return."),
) -> Any:
    """Get a page of an unstructured table by its v3_document_part id (docpart_…)."""
    table = await call.runtime.service.get_table(part_id)
    return _page_table(table, row_offset, row_limit)


@schema_app.command("get")
@command(read_only=True)
async def schema_get(
    call: Call,
    schema_id: str = typer.Argument(
        ...,
        help=(
            "Schema ID. es_… is read as an entity schema, assaysch_… as a result "
            "schema, runsch_… as a run schema, and any other id as an entry schema."
        ),
    ),
    field_offset: int = typer.Option(
        0, "--field-offset", min=0, help="Field definitions to skip. Zero is the first."
    ),
    field_limit: int = typer.Option(
        250, "--field-limit", min=1, help="Most field definitions to return."
    ),
) -> Any:
    """Get an entry, entity, result, or run schema and a page of its field definitions.

    Dropdown fields include the dropdown and its options.
    """
    data = await call.runtime.service.get_schema(schema_id)
    fields = data["field_definitions"]
    page = fields[field_offset : field_offset + field_limit]
    data["field_definitions"] = page
    data["pagination"] = _pagination(field_offset, len(page), len(fields))
    return data


@schema_app.command("find")
@command(read_only=True)
async def schema_find(
    call: Call,
    name: str = typer.Argument(..., help="Exact, case-sensitive schema name."),
    kind: str | None = typer.Option(
        None, "--kind", help="entry, entity, result, or run. Omit to search all kinds."
    ),
) -> Any:
    """Find one schema by exact name and return its field definitions and options."""
    return await call.runtime.service.find_schema(name, kind=kind)


@schema_app.command("list")
@command(read_only=True)
async def schema_list(
    call: Call,
    kind: str | None = typer.Option(
        None, "--kind", help="entry, entity, result, or run. Omit to list all four, in that order."
    ),
    name: str | None = typer.Option(None, "--name", help="Exact, case-sensitive schema name."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help=_LIMIT_HELP),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """List entry, entity, result, or run schemas. Follow next_token to see the rest."""
    return await call.runtime.service.list_schemas(kind=kind, name=name, limit=limit, cursor=cursor)


@container_app.command("get")
@command(read_only=True)
async def container_get(
    call: Call, container_id: str = typer.Argument(..., help="Inventory container ID (con_…).")
) -> Any:
    """Get an inventory container by container ID (con_…)."""
    return await call.runtime.service.get_container(container_id)


@container_app.command("find")
@command(read_only=True)
async def container_find(
    call: Call,
    barcode: str | None = typer.Option(None, "--barcode", help="Exact container barcode."),
    name: str | None = typer.Option(None, "--name", help="Exact container name."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help=_LIMIT_HELP),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """Find one page of containers by exact barcode or exact name."""
    return await call.runtime.service.find_containers(
        barcode=barcode, name=name, limit=limit, cursor=cursor
    )


@container_schema_app.command("list")
@command(read_only=True)
async def container_schema_list(
    call: Call,
    limit: int = typer.Option(50, "--limit", min=1, max=100, help=_LIMIT_HELP),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """List one page of container schemas and their field definitions."""
    return await call.runtime.service.list_container_schemas(limit=limit, cursor=cursor)


@results_app.command("list")
@command(read_only=True)
async def results_list(
    call: Call,
    entry: str | None = typer.Option(
        None,
        "--entry",
        help="Entry ID (etr_…), display ID, or URL. Omit to list results from every entry.",
    ),
    schema_id: str | None = typer.Option(
        None,
        "--schema",
        help="Result schema ID (assaysch_…). Omit to include every result schema.",
    ),
    result_ids: list[str] | None = typer.Option(
        None,
        "--id",
        help=(
            "Result id to list: a V3 result id (result_…) or a V2 assay result UUID. "
            "Repeat for several, up to 100."
        ),
    ),
    page_size: int = typer.Option(
        100, "--page-size", min=1, max=100, help="Result records per page, from 1 to 100."
    ),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """List one explicit page of result records, optionally filtered by entry, schema, or id.

    Records come from V3 GET /result/items. Each item's id is its V3 result id
    (result_…); v3_id repeats it and v2_id is its V2 assay result UUID, or null when
    Benchling does not return it. --id takes either namespace; a V2 UUID that cannot
    be mapped is listed in not_found with id_namespace and lookup. results get and
    results pending --id take either id too.
    """
    return await call.runtime.service.list_results_by_any_id(
        entry=entry,
        schema_id=schema_id,
        result_ids=result_ids or None,
        page_size=page_size,
        cursor=cursor,
    )


@results_app.command("get")
@command(read_only=True)
async def results_get(
    call: Call,
    result_id: str = typer.Argument(
        ...,
        help=(
            "Result id: a V3 result id (result_…) or a V2 assay result UUID, "
            "as results list, results pending, and changes status return."
        ),
    ),
) -> Any:
    """Get one result record by V3 result id (result_…) or V2 assay result UUID.

    Reads V3 GET /result/{id}; a V2 UUID is first mapped to its V3 id with V2
    GET /assay-results:bulk-get. The record adds v3_id (result_…) and v2_id (the
    V2 UUID, or null when Benchling does not return it). An id that cannot be read
    is a not_found error whose details give id_namespace (the namespace passed),
    expected_id_namespace, and lookup (the path used). Pending results are included.
    The record also adds table_check: the results-table row on its entry that links
    it, and whether that row's cells match the record (in_sync or stale; not_compared
    when the row is archived, UNSUBMITTED or HAS_UNSUBMITTED_CHANGES). A stale
    cell adds warnings; ask the person to use Re-sync table in the Benchling UI,
    which the API cannot do. Reads only.
    """
    service = call.runtime.service
    return await check_result_tables(service, await service.get_result_by_any_id(result_id))


@blob_app.command("get")
@command(read_only=True)
async def blob_get(
    call: Call,
    blob_id: str = typer.Argument(
        ...,
        help=("V2 blob UUID or V3 attachment_… ID, not a docpart_… attachment part ID."),
    ),
) -> Any:
    """Get metadata for a blob UUID or attachment_… ID. Nothing is downloaded."""
    return await call.runtime.service.get_blob(blob_id)


@blob_app.command("download")
@command(read_only=False)
async def blob_download(
    call: Call,
    blob_id: str = typer.Argument(..., help="V2 blob UUID or V3 attachment_… ID."),
    output: str = typer.Option(..., "--output", "-o", help="Local output file path."),
    expect_sha256: str | None = typer.Option(
        None, "--expect-sha256", help="Expected SHA-256 hex digest."
    ),
    expect_md5: str | None = typer.Option(None, "--expect-md5", help="Expected MD5 hex digest."),
    expect_size: int | None = typer.Option(
        None, "--expect-size", min=0, help="Expected size in bytes."
    ),
    compare: str | None = typer.Option(None, "--compare", help="Local reference file to compare."),
    force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
) -> Any:
    """Download a blob UUID or attachment_… ID to a file and report size and hashes.

    Compare against caller-supplied expectations or a local reference file.
    Without expectations the summary says verified is false. An existing output
    is kept unless --force is given. Benchling is only read.
    """
    return await download_blob(
        call.runtime.service,
        blob_id,
        output,
        expect_sha256=expect_sha256,
        expect_md5=expect_md5,
        expect_size=expect_size,
        compare=compare,
        force=force,
    )


@results_app.command("tables")
@command(read_only=True)
async def results_tables(
    call: Call,
    reference: str = typer.Argument(..., help=_ENTRY_REF_HELP),
    offset: int = typer.Option(
        0, "--offset", min=0, help="Result tables to skip. Zero is the first."
    ),
    limit: int = typer.Option(
        100, "--limit", min=1, max=100, help="Most result tables to return, from 1 to 100."
    ),
) -> Any:
    """Discover explicit V2 result-table IDs and assigned schemas on one entry.

    Each table_id is a v2_structured_table id (strtbl_…), not a v3_document_part
    id (docpart_…). results rows maps it with --entry; table get does not take it.

    CLI: Export it with ``export --kind results-table --part TABLE_ID --entry ENTRY``.

    MCP: Export it with the export tool: kind results-table, part_id TABLE_ID, and entry.
    """
    result = await call.runtime.service.list_result_tables(reference)
    items = result["items"]
    page = items[offset : offset + limit]
    result["items"] = page
    result["pagination"] = _pagination(offset, len(page), len(items))
    return result


@results_app.command("rows")
@command(read_only=True)
async def results_rows(
    call: Call,
    part_id: str = typer.Argument(
        ...,
        help=(
            "Results table id: a v3_document_part id (docpart_…) from entry inspect, "
            "or a v2_structured_table id (strtbl_…) from results tables with --entry."
        ),
    ),
    entry: str | None = typer.Option(
        None,
        "--entry",
        help=(
            "Entry ID (etr_…), display ID, or URL holding the table. "
            "Required with a strtbl_ id; not used with a docpart_ id."
        ),
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help=_LIMIT_HELP),
    cursor: str | None = typer.Option(None, "--cursor", help=_CURSOR_HELP),
) -> Any:
    """Read one page of rows from a results table.

    Rows come from GET /document-part-results-table/{id}/rows/items, which takes a
    v3_document_part id (docpart_…). A strtbl_ id from results tables is mapped
    to the matching part on the --entry entry, and the response adds table_id and
    listed_id_namespace. It is a validation_error when --entry is missing or no
    single part matches. Pass the previous next_token with --cursor.

    Each row linked to a result is compared with that result record, column by
    column through fieldDefinitionApiId. SUBMITTED and UNEVALUATED rows are
    compared; archived, UNSUBMITTED and HAS_UNSUBMITTED_CHANGES rows are skipped,
    since they may hold an unsaved edit. A row whose cell disagrees
    carries warning and stale_cells, and the page carries warnings: the notebook
    cell is stale, so ask the person to use Re-sync table in the Benchling UI,
    which the API cannot do. stale_check counts compared and stale rows. Reads only.
    """
    service = call.runtime.service
    page = await service.list_results_table_rows(part_id, entry=entry, limit=limit, cursor=cursor)
    return await annotate_rows(service, page)


@results_app.command("pending")
@command(read_only=True)
async def results_pending(
    call: Call,
    plan_id: str | None = typer.Option(
        None, "--plan", help="Applied result upload plan ID; its journal gives the record IDs."
    ),
    result_ids: list[str] | None = typer.Option(
        None,
        "--id",
        help=(
            "Result id: a V2 assay result UUID (read 200 per request) or a V3 "
            "result id (result_…). Repeat to read several; namespaces may be mixed."
        ),
    ),
    schema_id: str | None = typer.Option(
        None, "--schema", help="Result schema ID (assaysch_…). Needs --created-after."
    ),
    created_after: str | None = typer.Option(
        None,
        "--created-after",
        help="RFC 3339 time, e.g. 2026-11-19T00:00:00Z; results created at or after it. Used with --schema.",
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Rows to list, from 1 to 100."),
    cursor: str | None = typer.Option(
        None, "--cursor", help="next_token from the previous page (--schema only)."
    ),
) -> Any:
    """Read back results, including pending ones, and mark each pending, accepted, or archived.

    Pending results have no entry until a person accepts them with the plug icon
    in the results table, so listing results by entry does not find them. Pass
    exactly one of --plan, --id, or --schema with --created-after. Relay
    next_step. Reads only.

    Each item has id and v2_id (the V2 assay result UUID) and v3_id (result_…).
    A V3 id is read with V3 GET /result/{id} and matched to its V2 record; it is
    never sent to the V2 assayResultIds filter. Each id not found is listed in
    not_found, and in not_found_details with id_namespace and lookup.
    """
    return await call.runtime.planner.read_back_results(
        plan_id=plan_id,
        result_ids=result_ids or None,
        schema_id=schema_id,
        created_after=created_after,
        limit=limit,
        cursor=cursor,
    )


@results_app.command("verify")
@command(read_only=True)
async def results_verify(
    call: Call,
    plan_id: str = typer.Argument(..., help="Applied result upload plan ID in this environment."),
    sample: int = typer.Option(
        20, "--sample", min=1, max=200, help="Rows to list per category, from 1 to 200."
    ),
) -> Any:
    """Diff a result upload plan against the records Benchling created.

    Compares planned fields by system name and reports matched, mismatched,
    missing, and extra rows, and which rows still need the plug-icon accept. Reads only.
    The plan's record ids may be V2 assay result UUIDs or V3 result ids (result_…).
    Each row reports result_id and v2_id (the V2 UUID) and v3_id (result_…), so
    either id can be passed to results get or results pending --id.
    """
    return await call.runtime.planner.verify_results(plan_id, sample=sample)


@app.command("export")
@command(read_only=False)
async def export_data(
    call: Call,
    kind: str = typer.Option(
        ..., "--kind", help="What to export: table, results-table, or assay-results."
    ),
    output: str = typer.Option(
        ...,
        "--output",
        "-o",
        help="Local file path to write. An existing file is kept unless --overwrite is set.",
    ),
    format: str = typer.Option(..., "--format", help="File format: json or csv."),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace the file if it already exists."
    ),
    part_id: str | None = typer.Option(
        None,
        "--part",
        help=(
            "Table id. A v3_document_part id (docpart_…) comes from entry inspect. "
            "A v2_structured_table id (strtbl_…) comes from results tables "
            "and needs --entry with --kind results-table."
        ),
    ),
    entry: str | None = typer.Option(
        None,
        "--entry",
        help=(
            "Entry ID (etr_…), display ID, or URL. Assay-results filter. "
            "Also required with a strtbl_ id from results tables."
        ),
    ),
    schema_id: str | None = typer.Option(
        None, "--schema", help="Result schema ID (assaysch_…). Assay results only."
    ),
) -> Any:
    """Write a table, results table, or assay results to a local file and return a summary.

    Pages are read internally. The response is the row count, columns, path,
    and SHA-256 checksum, not the rows. Link and entity values are written as
    ids. An unset assay field is empty. An existing file is kept unless
    --overwrite is set. Benchling is only read.
    """
    return await export_dataset(
        call.runtime.service,
        kind=kind,
        path=output,
        file_format=format,
        overwrite=overwrite,
        part_id=part_id,
        entry=entry,
        schema_id=schema_id,
    )


def _read_spec(path: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text("utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "Could not read change spec",
            details={"path": path, "reason": redact_text(str(exc))},
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Change spec must be a JSON object")
    return parsed


def _parse_column_maps(column_map: list[str]) -> dict[str, str]:
    """Turn repeated ``CSV_COLUMN=FIELD`` options into the planner's column map."""
    mapped: dict[str, str] = {}
    for item in column_map:
        column, separator, field = item.partition("=")
        if not separator or not column.strip() or not field.strip():
            raise ValidationError(
                "Each --map must be CSV_COLUMN=FIELD",
                details={"map": item},
            )
        key = column.strip()
        if key in mapped:
            raise ValidationError(
                "A CSV column was mapped more than once",
                details={"column": key},
            )
        mapped[key] = field.strip()
    return mapped


def _parse_spec_json(text: str) -> dict[str, Any]:
    """Parse an inline --spec-json change spec."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "Could not parse --spec-json", details={"reason": redact_text(str(exc))}
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Change spec must be a JSON object")
    return parsed


def _plan_request(
    family: PlanFamily,
    spec: str | dict[str, Any] | None,
    csv_path: str | None,
    id_column: str | None,
    column_map: list[str],
    spec_json: str | None = None,
) -> dict[str, Any]:
    """Choose the spec (a file path, inline JSON, or an MCP object) or the CSV flags."""
    if sum(value is not None for value in (spec, spec_json, csv_path)) > 1:
        raise ValidationError("Pass only one of --spec, --spec-json, or --csv")
    if spec_json is not None:
        spec = _parse_spec_json(spec_json)
    if csv_path is not None:
        if family != PlanFamily.RESULTS:
            raise ValidationError("CSV result updates require the results family")
        return {
            "operation": "update",
            "csv_path": csv_path,
            "id_column": id_column,
            "column_map": _parse_column_maps(column_map),
        }
    if column_map or id_column is not None:
        raise ValidationError("--id-column and --map require --csv")
    if spec is None:
        raise ValidationError(
            "A change plan needs --spec or --spec-json, or --csv with --id-column and --map"
        )
    return spec if isinstance(spec, dict) else _read_spec(spec)


def _mcp_specification(value: Any) -> dict[str, Any] | None:
    """Check an inline MCP change specification before anything remote is read."""
    return None if value is None else parse_change_specification(value)


_SPECIFICATION = McpParam(
    annotation=Annotated[Any, WithJsonSchema(CHANGE_SPECIFICATION_SCHEMA)],
    description=(
        "Typed change for family, as a JSON object. Document and table changes set "
        "operation. Field changes set schema_fields. Result uploads set table_id and "
        'assay_results; an attachment field value may be {"file": PATH} to upload a '
        "local file as a blob at apply. Result updates set operation to update and pass "
        "csv_path, id_column, and column_map. Omit it only when csv_path is set."
    ),
    convert=_mcp_specification,
)


@changes_app.command("plan")
@command(read_only=False, cli_only=("spec_json",), mcp_params={"spec": _SPECIFICATION})
async def changes_plan(
    call: Call,
    entry: str | None = typer.Option(
        None,
        "--entry",
        help="Entry ID (etr_…), display ID, or URL. Omit for entry or container plans.",
    ),
    family: PlanFamily = typer.Option(
        ...,
        case_sensitive=False,
        help="Change family: entry, document, table, fields, results, or containers.",
    ),
    spec: str | None = typer.Option(None, "--spec", help="JSON file path, or '-' for stdin."),
    spec_json: str | None = typer.Option(
        None,
        "--spec-json",
        help="The change spec as inline JSON text, instead of --spec or --csv.",
    ),
    csv_path: str | None = typer.Option(
        None,
        "--csv",
        help="CSV file of result updates. Use with --id-column and --map instead of --spec.",
    ),
    id_column: str | None = typer.Option(
        None,
        "--id-column",
        help="CSV column that holds the V3 result id (result_…). Required with --csv.",
    ),
    column_map: list[str] = typer.Option(
        [],
        "--map",
        help="CSV column mapped to a field, as CSV_COLUMN=FIELD. Repeat for each field.",
    ),
) -> Any:
    """Create an immutable local preview of a change; nothing is written until it is applied.

    CLI: Pass the spec as a file with --spec, or inline with --spec-json.

    MCP: spec is the change spec as a JSON object.

    The spec is a typed change, checked before any remote read; an
    invalid one is a validation_error and nothing is written. Result updates can
    instead read a CSV file: --id-column names the result id, and each --map
    names a field by display name, system name, or field-definition id.

    Inline result uploads pass table_id and assay_results, or batches containing
    table_id and assay_results for each table, up to 500 records per table.
    Created results land pending until accepted in each table. On a result
    attachment field, a value of {"file": PATH} (or a CSV cell file:PATH) uploads
    that local file as a blob at apply and links its blob ID. The preview shows
    the file's base name, size, SHA-256 and MD5, never its path or contents.
    Files over 100 MiB are refused.

    Container plans support operation "create" with schema_id, or "transfer" with
    source_container_id, destination_container_id, and destination_contents.
    Entry plans omit --entry and pass entry_template_id, folder_id, and name;
    optional schema_fields are checked against the template schema. Other entry
    families require --entry.
    """
    if family == PlanFamily.ENTRY and entry is not None:
        raise ValidationError("Entry creation plans must not specify --entry")
    if family not in (PlanFamily.ENTRY, PlanFamily.CONTAINERS) and entry is None:
        raise ValidationError("This change family requires --entry")
    request = _plan_request(family, spec, csv_path, id_column, column_map, spec_json)
    return await call.runtime.planner.plan(entry, family, request)


@changes_app.command("apply")
@command(read_only=False, destructive=True)
async def changes_apply(
    call: Call,
    plan_id: str = typer.Argument(..., help="ID of a plan created in this environment."),
    confirm_env: str | None = typer.Option(
        None,
        "--confirm-env",
        help=(
            "Repeat the --env name. Required to apply to a protected environment; "
            "set it only after the person has confirmed that environment name."
        ),
    ),
) -> Any:
    """Apply a previously previewed plan, re-checking its preconditions first.

    CLI: A protected environment is refused unless --env names it on the command
    line and --confirm-env repeats the same name.

    MCP: Writes need env in the server's --allow-writes list; a protected env
    that is not listed is refused with a message saying so, and only a restart
    can enable it. A protected env also needs confirm_env equal to env. Set
    confirm_env only after the person has confirmed that environment name for
    this plan; never guess it or copy it from env.

    Entry creation returns the new entry ID and web_url (its Benchling web-app
    link, or null). A results upload also returns the entry's web_url; relay
    next_step, which links the entry, to have the person accept the pending
    results in the named table. Planned local files are uploaded as blobs first,
    once each with no retry; their blob IDs are in the journal.
    """
    call.authorize_apply(confirm_env)
    return await call.runtime.planner.apply(plan_id)


@changes_app.command("status")
@command(read_only=True)
async def changes_status(
    call: Call,
    plan_id: str = typer.Argument(..., help="ID of a plan created in this environment."),
    wait: bool = typer.Option(
        False, "--wait", help="Poll result tasks until completion or timeout."
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", min=0.1, max=3600, help="Maximum wait, in seconds (0.1-3600)."
    ),
) -> Any:
    """Show a plan journal and task status; optionally wait for submitted result tasks."""
    return await call.runtime.planner.status(plan_id, wait=wait, timeout=timeout)


def _issue_spec(
    spec: str | None,
    options: dict[str, Any],
    cite_urls: list[str],
    cite_quotes: list[str],
) -> dict[str, Any]:
    """Merge a JSON spec with flags. A flag overrides the same key in the spec."""
    merged: dict[str, Any] = _read_spec(spec) if spec is not None else {}
    for key, value in options.items():
        if value is not None:
            merged[key] = value
    if len(cite_urls) != len(cite_quotes):
        raise ValidationError(
            "Pass --cite-url and --cite-quote the same number of times, in matching order"
        )
    if cite_urls:
        merged["citations"] = [
            {"url": url, "quote": quote} for url, quote in zip(cite_urls, cite_quotes)
        ]
    return merged


def _file_with_gh(draft: dict[str, Any]) -> dict[str, Any]:
    """Create the issue with the GitHub CLI. Called only for an explicit --file."""
    import shutil
    import subprocess

    gh = shutil.which("gh")
    if gh is None:
        raise UnsupportedError(
            "The GitHub CLI (gh) is not installed. Open new_issue_url instead.",
            details={"new_issue_url": draft["new_issue_url"]},
        )
    command = [gh, "issue", "create", "--repo", REPOSITORY, "--title", draft["title"]]
    for label in draft["labels"]:
        command += ["--label", label]
    command += ["--body-file", "-"]
    completed = subprocess.run(
        command, input=draft["body"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise UpstreamError(
            "gh issue create failed; nothing was filed. Open new_issue_url instead.",
            details={
                "reason": redact_text(completed.stderr.strip())[-500:],
                "new_issue_url": draft["new_issue_url"],
            },
        )
    lines = completed.stdout.strip().splitlines()
    return {**draft, "filed": True, "issue_url": lines[-1] if lines else None, "next_step": None}


@issue_app.command("draft")
@command(read_only=True, needs_env=False, cli_only=("spec", "file"))
async def issue_draft(
    call: Call,
    spec: str | None = typer.Option(
        None, "--spec", help="JSON file with the draft fields, or '-' for stdin."
    ),
    kind: str | None = typer.Option(None, "--kind", help="bug or feature."),
    summary: str | None = typer.Option(
        None, "--summary", help="One-line issue title, 8 to 120 characters."
    ),
    attempted: str | None = typer.Option(
        None, "--attempted", help="What the user asked the agent to do, in plain words."
    ),
    expected: str | None = typer.Option(None, "--expected", help="What should have happened."),
    actual: str | None = typer.Option(
        None, "--actual", help="What the toolkit did or returned instead."
    ),
    interface: str | None = typer.Option(
        None, "--interface", help="cli or mcp. The MCP tool defaults to mcp."
    ),
    command_or_tool: str | None = typer.Option(
        None, "--command", help="The benchlingtk command or MCP tool that was used."
    ),
    description: str | None = typer.Option(
        None, "--description", help="Detailed description of the limitation and its impact."
    ),
    solution: str | None = typer.Option(
        None, "--solution", help="How the toolkit could support this, step by step."
    ),
    cite_url: list[str] = typer.Option(
        [],
        "--cite-url",
        help=(
            "https link on docs.benchling.com, help.benchling.com, or the API reference "
            "that supports the solution. Repeat with --cite-quote, in matching order."
        ),
    ),
    cite_quote: list[str] = typer.Option(
        [],
        "--cite-quote",
        help="The exact line on the matching --cite-url page that shows the fix is possible.",
    ),
    file: bool = typer.Option(
        False,
        "--file",
        help=(
            "File the issue on GitHub with `gh issue create`. Use only after the user "
            "said yes to this exact draft."
        ),
    ),
) -> Any:
    """Build a ready-to-file issue for something the toolkit cannot do.

    Call this only after you explained the limitation and the user said yes to
    logging it. Prints the title, body, labels, and a prefilled GitHub new-issue
    URL. The proposed solution must cite official Benchling docs, and drafts that
    look like they hold tenant URLs, record IDs, secrets, or email addresses are
    refused. Benchling is not contacted.

    CLI: Nothing is sent unless --file is passed.

    MCP: Nothing is filed. Show the user the draft; they open new_issue_url to file it.
    """
    options = {
        "kind": kind,
        "summary": summary,
        "attempted_action": attempted,
        "expected": expected,
        "actual": actual,
        "interface": "mcp" if interface is None and call.surface == "mcp" else interface,
        "command_or_tool": command_or_tool,
        "description": description,
        "proposed_solution": solution,
    }
    draft = build_issue_draft(_issue_spec(spec, options, cite_url, cite_quote))
    if file:
        draft = _file_with_gh(draft)
    if call.surface == "mcp":
        draft["next_step"] = (
            "Show the user this title and body. They file it by opening new_issue_url; "
            "this tool never files an issue."
        )
    call.meta["repository"] = REPOSITORY
    return draft


def _skill_path() -> Path:
    try:
        return find_skill_md()
    except BenchlingAgentError as exc:
        _emit(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None


@skill_app.command("path")
@cli_only("The MCP server publishes SKILL.md as the skill://benchlingtk/SKILL.md resource.")
def skill_path() -> None:
    """Print the absolute path of the installed SKILL.md as plain text.

    Looks in share/benchlingtk under the install prefixes, then in the source
    checkout. A missing file prints a not_found envelope. Read-only and offline.
    """
    typer.echo(str(_skill_path()))


@skill_app.command("show")
@cli_only("The MCP server publishes SKILL.md as the skill://benchlingtk/SKILL.md resource.")
def skill_show() -> None:
    """Print the contents of the installed SKILL.md as Markdown. Read-only and offline."""
    typer.echo(_skill_path().read_text(encoding="utf-8"), nl=False)


def _write_stderr_envelope(envelope: Envelope[Any]) -> None:
    """Startup failures belong on stderr. Stdout is the MCP stdio protocol."""
    sys.stderr.write(_json(envelope) + "\n")
    sys.stderr.flush()


@mcp_app.command("serve")
@cli_only("Starts the MCP server itself.")
def mcp_serve(
    allow_writes: list[str] = typer.Option(
        [],
        "--allow-writes",
        metavar="ENV",
        help="Allow changes_apply in this environment; repeatable.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Log MCP requests to stderr. Quiet by default so stdio clients stay readable.",
    ),
) -> None:
    """Run the MCP server on stdio; every tool call selects its environment."""
    from .mcp_server import run_mcp_server
    from .redact import redact_text

    try:
        run_mcp_server(writable_envs=set(allow_writes), verbose=verbose)
    except BenchlingAgentError as exc:
        _write_stderr_envelope(Envelope.failure(exc))
        raise typer.Exit(int(exc.exit_code)) from None
    except Exception as exc:
        error = UpstreamError(
            "MCP server failed to start",
            details={"type": type(exc).__name__, "reason": redact_text(str(exc))},
        )
        _write_stderr_envelope(Envelope.failure(error))
        raise typer.Exit(int(error.exit_code)) from None


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
