"""secure-fox — Secure FoxMCP browser-control server.

Wraps the vendored upstream FoxMCP server with domain validation on
tab-specific operations. Designed as a standalone package that is installed
into the gateway image and launched by its console script
``securefox-mcp-server``. It is referenced by the gateway only via proxy
policy (see deploy/config/policy/real/browser.yaml) — no import coupling.
"""

from securefox.server import SecureFoxMCPServer, SecureFoxMCPTools  # noqa: F401

__version__ = "0.1.0"

__all__ = [
    "SecureFoxMCPServer",
    "SecureFoxMCPTools",
    "__version__",
]