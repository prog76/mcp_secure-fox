# AGENTS.md

Guidance for coding agents working in this repository.

## What this is
`secure-fox` — standalone browser-control MCP server (Firefox extension over
WebSocket :8765, MCP over HTTP :9005). Wraps vendored upstream FoxMCP with
domain validation on tab-specific operations (`target: "domain_tabId"`).

It is intentionally decoupled from the gateway: no imports either way — the
gateway proxies to it via policy. In deploy it runs as its own container
reusing the gateway image with an `entrypoint: securefox-mcp-server` override.

## Layout
- `src/securefox/server.py` — `SecureFoxMCPServer` / `SecureFoxMCPTools`;
  `main()` is async, `cli()` is the console-script wrapper (do not swap them)
- `src/securefox/foxmcp_vendored/` — vendored upstream; override only
  `_setup_tab_tools`, `_setup_navigation_tools`, `_setup_content_tools`
- `tests/` — pytest smoke tests
- `.github/workflows/test-publish.yml` — CI: tests on push; on `v*` tags pushes
  `ghcr.io/prog76/mcp-secure-fox:<tag>`

## Commands
```bash
pip install -e ".[dev]"
python3 -m pytest -v          # from repo root
securefox-mcp-server --help   # console script smoke check
```

## Releasing
1. Bump `version` in `pyproject.toml` and `__init__.py` (keep equal).
2. Commit, `git tag vX.Y.Z`, `git push && git push --tags`.
3. CI publishes the image; deploy consumes it via the gateway image build arg
   `SECUREFOX_VERSION`.