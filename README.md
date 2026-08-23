# mcp_secure-fox

Secure FoxMCP browser-control MCP server.

Extracts `SecureFoxMCPServer` / `SecureFoxMCPTools` out of the gateway into a
standalone, installable Python package (`secure-fox`). It wraps the vendored
upstream FoxMCP server with domain validation on tab-specific operations.

Upstream compatibility: only `_setup_tab_tools`, `_setup_navigation_tools`,
`_setup_content_tools` are overridden. All other tools (windows, history,
bookmarks, request monitoring) are inherited unchanged from `FoxMCPTools`.

## Relationship to the gateway

`secure-fox` is intentionally decoupled from the gateway package:

- The gateway **does not import** `securefox` — it proxies to it just like any
  other backend (k8s, netbox, grafana).
- `secure-fox` is installed into the gateway/deploy image and launched by its
  console script `securefox-mcp-server`.
- The gateway's policy file `deploy/config/policy/real/browser.yaml` references
  it as `name: browser`, `url: http://localhost:9005/mcp`.

## Console script

```
securefox-mcp-server --mcp-port 9005 --ws-port 8765
```

The Firefox extension connects via WebSocket (port 8765). The MCP server runs
on port 9005 for the policy proxy to discover.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

## Releasing

Manual release flow (workflows build/test/publish on tag):

1. Bump `version` in `pyproject.toml` and `__init__.py` (keep them matching).
2. Commit, `git tag vX.Y.Z`, `git push && git push --tags`.
3. The workflow tests, publishes the wheel/sdist to PyPI (OIDC), and pushes
   `ghcr.io/prog76/mcp-secure-fox:vX.Y.Z`.
4. Manually pin the new version where it is consumed (deploy Dockerfile).