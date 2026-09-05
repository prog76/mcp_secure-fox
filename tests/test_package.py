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
    """Tab tools expose a `target` arg (plain tab ID) rather than tab_id."""
    tools = SecureFoxMCPTools.__new__(SecureFoxMCPTools)
    # Do not start any network: just confirm the hook methods exist.
    assert hasattr(tools, "_validate_target")
    assert hasattr(tools, "_setup_tab_tools")
    assert hasattr(tools, "_setup_navigation_tools")
    assert hasattr(tools, "_setup_content_tools")


def test_content_capture_element_is_registered_and_gated():
    """content_capture_element exists, takes `target`, and denies non-MCP tabs.

    Element screenshots read page pixels, which is exactly the capability the
    tab access control exists to scope — so the gate must cover it too.
    """
    import asyncio
    from unittest.mock import Mock, AsyncMock

    from securefox.server import SecureFoxMCPTools

    mock_ws = Mock()
    mock_ws.send_request_and_wait = AsyncMock(return_value={"type": "response", "data": {}})
    tools = SecureFoxMCPTools(mock_ws)

    async def run():
        registered = {t.name: t for t in await tools.mcp.list_tools()}
        if "content_capture_element" not in registered:
            return None  # tool not present — caller asserts on that
        capture = registered["content_capture_element"]
        schema = capture.parameters or {}
        assert "target" in schema.get("properties", {}), schema
        return capture.fn

    fn = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run())
    assert fn is not None, "content_capture_element is not registered by _setup_content_tools"

    # A tab nobody created through MCP must be refused before any request is
    # sent to the extension.
    saved_tabs = set(SecureFoxMCPTools._mcp_created_tabs)
    try:
        SecureFoxMCPTools._mcp_created_tabs.clear()
        loop = asyncio.get_event_loop_policy().new_event_loop()
        result = loop.run_until_complete(fn(target="999", xpath="//button"))
        assert "ACCESS DENIED" in result

        # And a tab MCP did create goes through, as a content.captureElement
        # request carrying the XPath.
        SecureFoxMCPTools._mcp_created_tabs.add(7)
        mock_ws.send_request_and_wait.reset_mock()
        mock_ws.send_request_and_wait.return_value = {
            "type": "response",
            "data": {
                "dataUrl": "data:image/png;base64,aGk=",
                "format": "png",
                "quality": 90,
                "rect": {"x": 0, "y": 0, "width": 10, "height": 10},
                "element": {"tag": "DIV", "id": "", "className": ""},
                "url": "https://example.com",
                "dpr": 1,
                "clipped": {"top": 0, "left": 0, "bottom": 0, "right": 0},
            },
        }
        result = loop.run_until_complete(fn(target="7", xpath="//div"))
        sent = mock_ws.send_request_and_wait.call_args[0][0]
        assert sent["action"] == "content.captureElement"
        assert sent["data"]["tabId"] == 7
        assert sent["data"]["xpath"] == "//div"
        assert "Element screenshot captured successfully" in result
    finally:
        SecureFoxMCPTools._mcp_created_tabs.clear()
        SecureFoxMCPTools._mcp_created_tabs |= saved_tabs
