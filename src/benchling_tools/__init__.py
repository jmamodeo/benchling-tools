"""Safe, auditable Benchling tools shared by the CLI and MCP server.

benchling-tools has no public Python API. Every module in this package is internal
and may change without notice; use the ``benchling-tools`` command or its MCP
server. To script Benchling from Python, use the official Benchling SDK:
https://docs.benchling.com/docs/getting-started-with-the-sdk
"""

from .models import Envelope

__all__ = ["Envelope"]
__version__ = "0.7.0"
