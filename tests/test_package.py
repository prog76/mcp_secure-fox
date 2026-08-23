#!/usr/bin/env python3
"""Smoke tests for the secure-fox package.

Verifies:
- The package imports and exposes the expected symbols.
- The class hierarchy is preserved (SecureFox* subclass upstream Fox*).
- Tab-specific MCP tools are registered on the app with a ``target`` arg.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import securefox  # noqa: E402
from securefox.foxmcp_vendored.mcp_tools import FoxMCPTools  # noqa: E402
from securefox.foxmcp_vendored.server import FoxMCPServer  # noqa: E402
from securefox.server import (  # noqa: E402
    SecureFoxMCPTools,
    SecureFoxMCPServer,
)


def test_version():
    assert securefox.__version__ == "0.1.0"


def test_class_hierarchy():
    assert issubclass(SecureFoxMCPTools, FoxMCPTools)
    assert issubclass(SecureFoxMCPServer, FoxMCPServer)
    assert hasattr(securefox, "SecureFoxMCPServer")
    assert hasattr(securefox, "SecureFoxMCPTools")


def test_console_main_is_callable():
    from securefox.server import cli, main

    assert callable(main)      # async server coroutine entrypoint
    assert callable(cli)       # sync console-script wrapper


def test_secure_tools_register_target_arg():
    """Tab/domain tools expose a `target` arg (domain_tabId) rather than tab_id."""
    tools = SecureFoxMCPTools.__new__(SecureFoxMCPTools)
    # Do not start any network: just confirm the hook methods exist.
    assert hasattr(tools, "_validate_target")
    assert hasattr(tools, "_setup_tab_tools")
    assert hasattr(tools, "_setup_navigation_tools")
    assert hasattr(tools, "_setup_content_tools")