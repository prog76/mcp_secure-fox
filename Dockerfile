# secure-fox — standalone Secure FoxMCP browser-control server.
FROM python:3.12-slim

# System deps for outbound TLS + subprocess-based predefined scripts.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install the secure-fox package (pulls fastmcp, uvicorn, websockets, ...).
COPY . /build/secure-fox
RUN pip install --no-cache-dir /build/secure-fox \
    && rm -rf /build

# WebSocket for the Firefox extension; MCP on 9005.
EXPOSE 8765 9005

ENTRYPOINT ["securefox-mcp-server"]
# Bind 0.0.0.0 (default) so the gateway/proxy can reach this backend across the
# Docker network; loopback-only binding causes ECONNREFUSED during discovery.
CMD ["--host", "0.0.0.0", "--mcp-port", "9005", "--ws-port", "8765"]