#!/usr/bin/env python3
"""
Secure FoxMCP Server — wraps FoxMCP with tab access control for tab operations.

Subclasses FoxMCPTools to replace tab_id: int with target: str (a plain tab ID)
in all tab-specific tools, allowing access only to tabs created by MCP.

The Firefox extension connects via WebSocket (port 8765) unchanged.
The MCP server runs on port 9005 for policy proxy discovery.

Upstream compatibility: only _setup_tab_tools, _setup_navigation_tools,
_setup_content_tools are overridden. All other tools (windows, history,
bookmarks, request monitoring) are inherited unchanged from FoxMCPTools.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List, Set, Tuple
from urllib.parse import urlparse

# Import FoxMCP upstream (vendored at securefox/foxmcp_vendored/)
# The source is cloned from https://github.com/ThinkerYzu/foxmcp/tree/master/server
# and vendored as foxmcp_vendored/ to avoid namespace conflict with the 'mcp' package.
from securefox.foxmcp_vendored.server import FoxMCPServer
from securefox.foxmcp_vendored.mcp_tools import FoxMCPTools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tab URL cache: tab_id -> {url: str, timestamp: float}
# ---------------------------------------------------------------------------
_tab_cache: Dict[int, Dict[str, Any]] = {}
CACHE_TTL = 5.0  # seconds — how long before a cache entry is considered stale


class SecureFoxMCPTools(FoxMCPTools):
    """Extends FoxMCPTools with tab access control on tab-specific operations.

    Overrides _setup_tab_tools, _setup_navigation_tools, _setup_content_tools
    to replace tab_id: int with target: str (a plain tab ID, e.g. "123").
    Only tabs created by MCP can be targeted by other tools — this is
    enforced by tracking created tab IDs in ``_mcp_created_tabs``.
    All other tools (windows, history, bookmarks, request monitoring) are
    inherited unchanged from FoxMCPTools.
    """

    # ------------------------------------------------------------------
    # MCP-created tab tracking.  Only these tabs may be targeted by
    # other tab tools (tabs_close, tabs_switch, navigation_*, etc.).
    # ------------------------------------------------------------------
    _mcp_created_tabs: Set[int] = set()

    # ------------------------------------------------------------------
    # Tab URL cache helpers
    # ------------------------------------------------------------------

    async def _refresh_tab_cache(self) -> None:
        """Fetch all tabs from the extension and update the URL cache."""
        request = {
            "id": str(uuid.uuid4()),
            "type": "request",
            "action": "tabs.list",
            "data": {},
            "timestamp": datetime.now().isoformat(),
        }
        response = await self.websocket_server.send_request_and_wait(request)
        if response.get("type") == "response" and "data" in response:
            tabs = response["data"].get("tabs", [])
            now = time.monotonic()
            for tab in tabs:
                tab_id = tab.get("id")
                url = tab.get("url", "")
                if tab_id is not None:
                    _tab_cache[tab_id] = {"url": url, "timestamp": now}

    async def _get_tab_url(self, tab_id: int) -> Optional[str]:
        """Get the current URL of a tab, from cache or fresh fetch."""
        now = time.monotonic()
        entry = _tab_cache.get(tab_id)
        if entry and (now - entry["timestamp"]) < CACHE_TTL:
            return entry["url"]
        # Cache miss or stale — refresh from extension
        await self._refresh_tab_cache()
        entry = _tab_cache.get(tab_id)
        return entry["url"] if entry else None

    async def _validate_target(self, target: str) -> Tuple[Optional[int], Optional[str]]:
        """Validate that *target* is a tab ID created by MCP.

        Returns:
            (tab_id, None) on success
            (None, error_message) on failure
        """
        try:
            tab_id = int(target)
        except (ValueError, TypeError):
            return (
                None,
                f"Invalid tab ID: {target!r} (must be a number)",
            )

        if tab_id not in self._mcp_created_tabs:
            return (
                None,
                f"Tab {tab_id} was not created by MCP — access denied",
            )

        return tab_id, None

    # ------------------------------------------------------------------
    # Override: tab management tools
    # ------------------------------------------------------------------

    def _setup_tab_tools(self):
        """Override tabs_list, tabs_create, tabs_close, tabs_switch, tabs_capture_screenshot."""

        @self.mcp.tool()
        async def tabs_list() -> str:
            """List all open browser tabs

            Returns:
                Formatted string with tab information:
                "Open tabs ({count} found):
                - ID {tab_id}: {title} - {url}{status_indicators}"

                Status indicators include:
                - (active) - for the currently active tab
                - (pinned) - for pinned tabs
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.list",
                "data": {},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting tabs: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                tabs = response["data"].get("tabs", [])
                if not tabs:
                    return f"No tabs found. Extension response: {response.get('data', {})}"

                result = f"Open tabs ({len(tabs)} found):\n"
                for tab in tabs:
                    active = " (active)" if tab.get("active") else ""
                    pinned = " (pinned)" if tab.get("pinned") else ""
                    result += (
                        f"- ID {tab.get('id')}: {tab.get('title', 'No title')} "
                        f"- {tab.get('url', 'No URL')}{active}{pinned}\n"
                    )
                return result

            return "Unable to retrieve tabs"

        @self.mcp.tool()
        async def tabs_create(
            url: str,
            active: bool = True,
            pinned: bool = False,
            window_id: Optional[int] = None,
        ) -> str:
            """Create a new browser tab.

            Args:
                url: URL to open in the new tab
                active: Whether the tab should be active (default: True)
                pinned: Whether the tab should be pinned (default: False)
                window_id: Window ID to create tab in (optional)

            Returns:
                JSON string with keys: ``ok``, ``tab_id``, ``url``, ``title``.
                The ``tab_id`` can be used as the ``target`` argument for
                tabs_close, tabs_switch, navigation_go_to_url, etc.
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.create",
                "data": {
                    "url": url,
                    "active": active,
                    "pinned": pinned,
                    **({"windowId": window_id} if window_id else {}),
                },
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return json.dumps({"ok": False, "error": f"Error creating tab: {response['error']}"})

            if response.get("type") == "response" and "data" in response:
                tab = response["data"].get("tab", {})
                tab_id = tab.get('id')
                if tab_id is None:
                    return json.dumps({"ok": False, "error": "Unable to create tab"})
                # Track this tab so other tools can target it.
                SecureFoxMCPTools._mcp_created_tabs.add(tab_id)
                return json.dumps({
                    "ok": True,
                    "tab_id": tab_id,
                    "url": tab.get('url', url),
                    "title": tab.get('title', 'Loading...'),
                })

            return json.dumps({"ok": False, "error": "Unable to create tab"})

        @self.mcp.tool()
        async def tabs_close(target: str) -> str:
            """Close a browser tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.close",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error closing tab: {response['error']}"
            if response.get("type") == "response":
                SecureFoxMCPTools._mcp_created_tabs.discard(tab_id)
                return f"Successfully closed tab {tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to close tab: {error_msg}"
            return f"Unable to close tab {tab_id}"

        @self.mcp.tool()
        async def tabs_switch(target: str) -> str:
            """Switch to a specific browser tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.switch",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error switching to tab: {response['error']}"
            if response.get("type") == "response":
                return f"Successfully switched to tab {tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to switch to tab: {error_msg}"
            return f"Unable to switch to tab {tab_id}"

        # tabs_capture_screenshot — unchanged from upstream (no tab_id param)
        @self.mcp.tool()
        async def tabs_capture_screenshot(
            filename: Optional[str] = None,
            window_id: Optional[int] = None,
            format: str = "png",
            quality: int = 90,
        ) -> str:
            """Capture a screenshot of the visible tab

            Args:
                filename: Name of the file to save the screenshot (optional, if not provided returns base64)
                window_id: ID of the window to capture (optional, defaults to current window)
                format: Image format ('png' or 'jpeg', default: 'png')
                quality: Image quality for JPEG format (1-100, default: 90)

            Returns:
                Success message with file path if filename provided, otherwise base64 encoded image data URL
            """
            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "tabs.captureVisibleTab",
                "data": {
                    **({"windowId": window_id} if window_id else {}),
                    "format": format,
                    "quality": quality,
                },
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error capturing screenshot: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                data_url = response["data"].get("dataUrl", "")
                captured_format = response["data"].get("format", format)
                captured_quality = response["data"].get("quality", quality)
                captured_window_id = response["data"].get("windowId", "current")

                if not data_url:
                    return "No screenshot data received"

                data_prefix = f"data:image/{captured_format};base64,"
                if not data_url.startswith(data_prefix):
                    return f"Screenshot captured but unexpected format: {data_url[:100]}..."

                base64_data = data_url[len(data_prefix):]

                if filename:
                    try:
                        if not filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                            filename = f"{filename}.{captured_format}"
                        image_data = base64.b64decode(base64_data)
                        with open(filename, 'wb') as f:
                            f.write(image_data)
                        file_size = len(image_data)
                        return (
                            f"Screenshot saved to '{filename}' "
                            f"(window {captured_window_id}, {captured_format}, "
                            f"quality: {captured_quality}, size: {file_size} bytes)"
                        )
                    except Exception as e:
                        return f"Error saving screenshot to file '{filename}': {str(e)}"
                else:
                    data_size = len(base64_data)
                    return (
                        f"Screenshot captured successfully from window {captured_window_id} "
                        f"({captured_format}, quality: {captured_quality}):\n"
                        f"{data_url[:100]}...\n\nBase64 data size: {data_size} characters"
                    )

            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to capture screenshot: {error_msg}"

            return "Unable to capture screenshot"

    # ------------------------------------------------------------------
    # Override: navigation tools
    # ------------------------------------------------------------------

    def _setup_navigation_tools(self):
        """Override: navigation tools with target validation."""

        @self.mcp.tool()
        async def navigation_back(target: str) -> str:
            """Navigate back in browser history for a tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.back",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error navigating back: {response['error']}"
            if response.get("type") == "response":
                return f"Successfully navigated back in tab {tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to navigate back: {error_msg}"
            return f"Unable to navigate back in tab {tab_id}"

        @self.mcp.tool()
        async def navigation_forward(target: str) -> str:
            """Navigate forward in browser history for a tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.forward",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error navigating forward: {response['error']}"
            if response.get("type") == "response":
                return f"Successfully navigated forward in tab {tab_id}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to navigate forward: {error_msg}"
            return f"Unable to navigate forward in tab {tab_id}"

        @self.mcp.tool()
        async def navigation_reload(target: str, bypass_cache: bool = False) -> str:
            """Reload a page in a tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
                bypass_cache: Whether to bypass cache when reloading (default: False)
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.reload",
                "data": {"tabId": tab_id, "bypassCache": bypass_cache},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error reloading page: {response['error']}"
            if response.get("type") == "response":
                cache_text = " (bypassing cache)" if bypass_cache else ""
                return f"Successfully reloaded tab {tab_id}{cache_text}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to reload page: {error_msg}"
            return f"Unable to reload tab {tab_id}"

        @self.mcp.tool()
        async def navigation_go_to_url(target: str, url: str) -> str:
            """Navigate to a specific URL in a tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
                url: URL to navigate to
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "navigation.go_to_url",
                "data": {"tabId": tab_id, "url": url},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error navigating to URL: {response['error']}"
            if response.get("type") == "response":
                return f"Successfully navigated tab {tab_id} to {url}"
            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to navigate to URL: {error_msg}"
            return f"Unable to navigate tab {tab_id} to {url}"

    # ------------------------------------------------------------------
    # Override: content access tools
    # ------------------------------------------------------------------

    def _setup_content_tools(self):
        """Override: content tools with target validation."""

        @self.mcp.tool()
        async def content_get_text(target: str, max_length: Optional[int] = None) -> str:
            """Get text content from a tab's page.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
                max_length: Optional maximum length of text to return
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.get_text",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting page text: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                text = response["data"].get("text", "")
                url = response["data"].get("url", "Unknown URL")
                title = response["data"].get("title", "Unknown Title")

                if not text:
                    return f"No text content found in tab {tab_id} ({title})"

                if max_length is not None and len(text) > max_length:
                    return f"Text content from {title} ({url}):\n\n{text[:max_length]}..."
                return f"Text content from {title} ({url}):\n\n{text}"

            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to get page text: {error_msg}"

            return f"Unable to get text content from tab {tab_id}"

        @self.mcp.tool()
        async def content_get_html(target: str) -> str:
            """Get HTML content from a tab's page.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.get_html",
                "data": {"tabId": tab_id},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error getting page HTML: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                html = response["data"].get("html", "")
                url = response["data"].get("url", "Unknown URL")
                title = response["data"].get("title", "Unknown Title")

                if not html:
                    return f"No HTML content found in tab {tab_id} ({title})"

                return (
                    f"HTML content from {title} ({url}):\n\n"
                    f"{html[:2000]}{'...' if len(html) > 2000 else ''}"
                )

            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to get page HTML: {error_msg}"

            return f"Unable to get HTML content from tab {tab_id}"

        @self.mcp.tool()
        async def content_execute_script(target: str, code: str) -> str:
            """Execute JavaScript code in a tab.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
                code: JavaScript code to execute
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.execute_script",
                "data": {"tabId": tab_id, "script": code},
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error executing script: {response['error']}"

            if response.get("type") == "response" and "data" in response:
                result = response["data"].get("result")
                url = response["data"].get("url", "Unknown URL")

                if result is None:
                    return (
                        f"Script executed successfully in tab {tab_id} ({url}) "
                        f"- no return value"
                    )
                return f"Script result from tab {tab_id} ({url}):\n{result}"

            elif response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                return f"Failed to execute script: {error_msg}"

            return f"Unable to execute script in tab {tab_id}"

        @self.mcp.tool()
        async def content_capture_element(
            target: str,
            xpath: str,
            filename: Optional[str] = None,
            format: str = "png",
            quality: int = 90,
            scroll_into_view: bool = True,
            padding: int = 0,
        ) -> str:
            """Capture a screenshot of a single element, selected by XPath.

            Locates the element in the tab, scrolls it into view, captures the
            tab and crops the image to the element's box. The tab does not have
            to be the active tab — background tabs are captured too.

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
                xpath: XPath expression selecting the element (first match is used)
                filename: Save the image to this file (optional)
                format: Image format ('png' or 'jpeg', default: 'png')
                quality: Image quality for JPEG format (1-100, default: 90)
                scroll_into_view: Scroll the element into view before capturing (default: true)
                padding: Extra CSS pixels captured around the element (default: 0)
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            request = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "action": "content.captureElement",
                "data": {
                    "tabId": tab_id,
                    "xpath": xpath,
                    "format": format,
                    "quality": quality,
                    "scrollIntoView": scroll_into_view,
                    "padding": padding,
                },
                "timestamp": datetime.now().isoformat(),
            }

            response = await self.websocket_server.send_request_and_wait(request)

            if "error" in response:
                return f"Error capturing element: {response['error']}"

            if response.get("type") == "error":
                error_msg = response.get("data", {}).get("message", "Unknown error")
                error_code = response.get("data", {}).get("code", "CAPTURE_ERROR")
                return f"Failed to capture element ({error_code}): {error_msg}"

            if response.get("type") == "response" and "data" in response:
                data_url = response["data"].get("dataUrl", "")
                captured_format = response["data"].get("format", format)
                element = response["data"].get("element", {})
                rect = response["data"].get("rect", {})
                clipped = response["data"].get("clipped", {})

                if not data_url:
                    return "No element screenshot data received"

                element_desc = element.get("tag", "element")
                if element.get("id"):
                    element_desc += f"#{element['id']}"

                clip_amounts = {side: amount for side, amount in clipped.items() if amount}
                clip_desc = ""
                if clip_amounts:
                    clip_parts = [f"{amount}px {side}" for side, amount in clip_amounts.items()]
                    clip_desc = f" (clipped: {', '.join(clip_parts)})"

                data_prefix = f"data:image/{captured_format};base64,"
                if not data_url.startswith(data_prefix):
                    return f"Element screenshot captured but unexpected format: {data_url[:100]}..."

                base64_data = data_url[len(data_prefix):]

                if filename:
                    try:
                        if not filename.lower().endswith((".png", ".jpg", ".jpeg")):
                            filename = f"{filename}.{captured_format}"

                        image_data = base64.b64decode(base64_data)
                        with open(filename, "wb") as f:
                            f.write(image_data)

                        file_size = len(image_data)
                        return (
                            f"Element screenshot saved to '{filename}' "
                            f"({element_desc} in tab {tab_id}, "
                            f"{rect.get('width', '?')}x{rect.get('height', '?')} CSS px, "
                            f"{captured_format}, size: {file_size} bytes){clip_desc}"
                        )

                    except Exception as e:
                        return f"Error saving element screenshot to file '{filename}': {str(e)}"

                data_size = len(base64_data)
                return (
                    f"Element screenshot captured successfully: {element_desc} in tab {tab_id}{clip_desc}:"
                    f"\n{data_url[:100]}...\n\nBase64 data size: {data_size} characters"
                )

            return f"Unable to capture element in tab {tab_id}"

        @self.mcp.tool()
        async def content_execute_predefined(
            target: str,
            script_name: str,
            script_args: str = "",
        ) -> str:
            """Execute a predefined external script and run its JavaScript output in a tab

            Args:
                target: Tab ID (e.g., "123") — must be a tab created by MCP.
                script_name: Name of the external script to run
                script_args: JSON array of strings to pass to the external script
                            (e.g., '["arg1", "arg2"]') or empty string for no arguments
            """
            tab_id, error = await self._validate_target(target)
            if error:
                return f"ACCESS DENIED: {error}"

            # Get the scripts directory from environment variable
            scripts_dir = os.environ.get("FOXMCP_EXT_SCRIPTS")
            if not scripts_dir:
                return "Error: FOXMCP_EXT_SCRIPTS environment variable not set"

            # Validate script name to prevent path traversal attacks
            if not script_name or ".." in script_name or "/" in script_name or "\\" in script_name:
                return (
                    f"Error: Invalid script name '{script_name}'. "
                    f"Script names cannot contain path separators or '..' sequences"
                )

            if not re.match(r"^[a-zA-Z0-9._-]+$", script_name):
                return (
                    f"Error: Invalid script name '{script_name}'. "
                    f"Only alphanumeric characters, underscore, dash, and dot are allowed"
                )

            scripts_dir = os.path.abspath(scripts_dir)
            script_path = os.path.abspath(os.path.join(scripts_dir, script_name))

            if not script_path.startswith(scripts_dir + os.sep) and script_path != scripts_dir:
                return f"Error: Script path '{script_name}' escapes the allowed directory"

            if not os.path.exists(script_path):
                return f"Error: Script '{script_name}' not found in {scripts_dir}"

            if not os.access(script_path, os.X_OK):
                return f"Error: Script '{script_name}' is not executable"

            try:
                # Parse JSON arguments
                try:
                    if script_args.strip() == "":
                        args_list = []
                    else:
                        args_list = json.loads(script_args)
                        if not isinstance(args_list, list):
                            return (
                                f"Error: script_args must be a JSON array of strings "
                                f"or empty string, got: {type(args_list).__name__}"
                            )
                        for i, arg in enumerate(args_list):
                            if not isinstance(arg, str):
                                return (
                                    f"Error: All arguments must be strings. "
                                    f"Argument {i} is {type(arg).__name__}: {arg}"
                                )
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON in script_args: {e}"

                # Execute the external script with arguments
                result = subprocess.run(
                    [script_path] + args_list,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if result.returncode != 0:
                    return (
                        f"Error: Script '{script_name}' failed with exit code "
                        f"{result.returncode}. stderr: {result.stderr}"
                    )

                javascript_code = result.stdout.strip()
                if not javascript_code:
                    return f"Error: Script '{script_name}' produced no output"

                # Execute the generated JavaScript in the tab
                request = {
                    "id": str(uuid.uuid4()),
                    "type": "request",
                    "action": "content.execute_script",
                    "data": {"tabId": tab_id, "script": javascript_code},
                    "timestamp": datetime.now().isoformat(),
                }

                response = await self.websocket_server.send_request_and_wait(request)

                if "error" in response:
                    return f"Error executing generated script: {response['error']}"

                if response.get("type") == "response" and "data" in response:
                    result_data = response["data"].get("result")
                    url = response["data"].get("url", "Unknown URL")

                    if result_data is None:
                        return (
                            f"Predefined script '{script_name}' executed successfully "
                            f"in tab {tab_id} ({url}) - no return value"
                        )
                    return (
                        f"Predefined script '{script_name}' result from tab "
                        f"{tab_id} ({url}):\n{result_data}"
                    )

                elif response.get("type") == "error":
                    error_msg = response.get("data", {}).get("message", "Unknown error")
                    return f"Failed to execute generated script: {error_msg}"

                return f"Unable to execute generated script in tab {tab_id}"

            except subprocess.TimeoutExpired:
                return f"Error: Script '{script_name}' timed out after 30 seconds"
            except subprocess.SubprocessError as e:
                return f"Error executing script '{script_name}': {e}"
            except Exception as e:
                return f"Unexpected error running script '{script_name}': {e}"


class SecureFoxMCPServer(FoxMCPServer):
    """Extends FoxMCPServer to use SecureFoxMCPTools instead of FoxMCPTools.

    All WebSocket handling, extension protocol, and server lifecycle
    are inherited unchanged from FoxMCPServer.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Replace the tools instance with our secure version
        self.mcp_tools = SecureFoxMCPTools(self)
        self.mcp_app = self.mcp_tools.get_mcp_app()


async def main():
    """Main entry point for the secure FoxMCP server."""
    parser = argparse.ArgumentParser(
        description="Secure FoxMCP Server - with domain validation"
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Host to bind to. Defaults to 'localhost' for security when omitted. "
            "Pass '0.0.0.0' explicitly to accept connections from other "
            "containers (required for the gateway to discover/proxy to this "
            "backend across the Docker network)."
        ),
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=8765,
        help="WebSocket port for Firefox extension (default: 8765)",
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=9005,
        help="MCP server port (default: 9005)",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Disable MCP server",
    )

    args = parser.parse_args()

    # Bind to loopback by default for security, but honor an explicit host
    # (e.g. '0.0.0.0') so the gateway can reach this backend across the Docker
    # network. Only a *defaulted* (None) value falls back to localhost; an
    # operator-supplied --host is respected as-is.
    if args.host is None:
        args.host = "localhost"

    server = SecureFoxMCPServer(
        host=args.host,
        port=args.ws_port,
        mcp_port=args.mcp_port,
        start_mcp=not args.no_mcp,
    )
    await server.start_server()


def cli():
    """Synchronous console-script entrypoint (see pyproject.toml).

    pip console scripts invoke a plain function — ``main`` is async, so this
    wrapper runs it on the event loop and maps interrupts/errors to logs.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")


if __name__ == "__main__":
    cli()