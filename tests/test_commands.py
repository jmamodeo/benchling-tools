"""Command declarations: the rules every CLI command and generated tool follow."""

from __future__ import annotations

import pytest
import typer

from benchlingtk.commands import (
    REQUIRED,
    Call,
    McpParam,
    Param,
    declare,
    rename_flags,
    surface_text,
)
from benchlingtk.errors import ValidationError
from benchlingtk.mcp_server import McpCall
from benchlingtk.runtime import RuntimePool


async def _documented(
    call: Call,
    name: str = typer.Argument(..., help="A name."),
    count: int = typer.Option(1, "--count", min=1, max=5, help="How many."),
) -> None:
    """Do one thing."""


def test_declare_reads_help_defaults_and_limits() -> None:
    declared = declare(_documented, read_only=True)
    name, count = declared.params
    assert (name.name, name.help, name.default) == ("name", "A name.", REQUIRED)
    assert (count.default, count.min, count.max) == (1, 1, 5)
    assert declared.doc == "Do one thing."


async def _no_help(call: Call, name: str = typer.Option("x", "--name")) -> None:
    """Missing help."""


async def _no_option(call: Call, name: str = "x") -> None:
    """Plain default."""


def test_declare_refuses_undocumented_or_unknown_parameters() -> None:
    with pytest.raises(TypeError, match="needs help text"):
        declare(_no_help, read_only=True)
    with pytest.raises(TypeError, match="must use typer.Option"):
        declare(_no_option, read_only=True)
    with pytest.raises(TypeError, match="no parameters"):
        declare(_documented, read_only=True, cli_only=("missing",))
    override = McpParam(annotation=str, description="Overridden.")
    assert declare(_no_help, read_only=True, mcp_params={"name": override}).mcp_params == {
        "name": override
    }


@pytest.mark.parametrize(
    ("low", "high", "value", "message"),
    [
        (1, 5, 9, "count must be from 1 to 5"),
        (1, None, 0, "count must be at least 1"),
        (None, 5, 9, "count must be at most 5"),
    ],
)
def test_range_errors_name_the_parameter(low, high, value, message) -> None:
    param = Param("count", int, 1, "How many.", min=low, max=high)
    with pytest.raises(ValidationError, match=message):
        param.check_range(value)
    param.check_range(True)
    param.check_range("text")


def test_surface_text_and_flag_names() -> None:
    doc = "Summary.\n\nCLI: Pass --count.\n\nMCP: Pass count.\n\nShared --count text."
    assert surface_text(doc, "cli") == "Summary.\n\nPass --count.\n\nShared --count text."
    assert surface_text(doc, "mcp") == "Summary.\n\nPass count.\n\nShared --count text."
    assert rename_flags("Use --count, not --counter.", {"--count": "count"}) == (
        "Use count, not --counter."
    )


def test_base_call_has_no_runtime_or_apply_rule() -> None:
    call = Call("sandbox")
    assert call.writable_envs is None
    assert call.envelope_meta() == {}
    with pytest.raises(NotImplementedError):
        call.runtime  # noqa: B018
    with pytest.raises(NotImplementedError):
        call.authorize_apply(None)


def test_mcp_call_without_env_is_a_validation_error(benchling_env) -> None:
    call = McpCall(RuntimePool(), None, frozenset())
    with pytest.raises(ValidationError, match="env is required"):
        call.runtime  # noqa: B018
