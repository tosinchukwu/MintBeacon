#!/usr/bin/env python3
"""MintBeacon MCP Streamable HTTP transport.

The existing stdio transport remains in mcp/server.py. This module exposes the
same JSON-RPC request handler over stateless MCP Streamable HTTP, using only
the Python standard library.

Security controls:
- bounded request body
- per-client request rate limit
- strict JSON-RPC object requests (no batches)
- Origin allowlist for browser clients
- optional Host allowlist
- no transaction signing, private keys, or state-changing RPC methods
"""
import json
import os
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import server as mcp_server


SERVICE_NAME = "MintBeacon MCP"
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
MAX_BODY_BYTES = int(os.environ.get("MCP_MAX_BODY_BYTES", "65536"))
RATE_LIMIT = int(os.environ.get("MCP_RATE_LIMIT_PER_MINUTE", "30"))
RATE_WINDOW_SECONDS = 60
ALLOWED_ORIGINS = {
    value.strip().rstrip("/")
    for value in os.environ.get(
        "MCP_ALLOWED_ORIGINS", "https://mintbeacon.vercel.app"
    ).split(",")
    if value.strip()
}
ALLOWED_HOSTS = {
    value.strip().lower()
    for value in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",")
    if value.strip()
}

_rate_lock = threading.Lock()
_rate_hits = defaultdict(list)


def configured_mintbeacon():
    address = (
        os.environ.get("MINTBEACON_ADDRESS")
        or "0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B"
    ).strip()
    if not mcp_server.ADDR_RE.match(address):
        raise RuntimeError("MINTBEACON_ADDRESS must be a valid 0x contract address")
    return address.lower()


def client_key(handler):
    # Hosting proxies should overwrite X-Forwarded-For before forwarding it.
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    return handler.client_address[0]


def rate_limited(key):
    now = time.monotonic()
    cutoff = now - RATE_WINDOW_SECONDS
    with _rate_lock:
        hits = [stamp for stamp in _rate_hits[key] if stamp > cutoff]
        if len(hits) >= RATE_LIMIT:
            _rate_hits[key] = hits
            return True, max(1, int(RATE_WINDOW_SECONDS - (now - hits[0])))
        hits.append(now)
        _rate_hits[key] = hits
        # Keep the in-memory map bounded on long-lived instances.
        if len(_rate_hits) > 2000:
            for old_key in list(_rate_hits)[:500]:
                if not _rate_hits[old_key] or _rate_hits[old_key][-1] <= cutoff:
                    _rate_hits.pop(old_key, None)
        return False, 0


class MintBeaconHTTPHandler(BaseHTTPRequestHandler):
    server_version = "MintBeaconMCP/0.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Keep routine access logs concise and avoid logging request bodies.
        sys.stderr.write("[mintbeacon-mcp] %s - %s\n" % (
            self.address_string(), fmt % args
        ))

    def _origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return None, True
        normalised = origin.rstrip("/")
        return normalised, normalised in ALLOWED_ORIGINS

    def _host_allowed(self):
        if not ALLOWED_HOSTS:
            return True
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        return host in ALLOWED_HOSTS

    def _send(self, status, body=b"", content_type=None, extra_headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin, allowed = self._origin()
        if origin and allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Accept, Authorization, MCP-Protocol-Version",
            )
            self.send_header("Access-Control-Max-Age", "600")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra_headers)

    def _reject_origin_or_host(self):
        origin, allowed = self._origin()
        if not allowed:
            self._json(403, {"error": "Origin is not allowed"})
            return True
        if not self._host_allowed():
            self._json(403, {"error": "Host is not allowed"})
            return True
        return False

    def do_OPTIONS(self):
        if self._reject_origin_or_host():
            return
        self._send(204)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            self._json(200, {
                "ok": True,
                "service": SERVICE_NAME,
                "transport": "MCP Streamable HTTP",
                "endpoint": MCP_PATH,
            })
            return
        if path == "/":
            self._json(200, {
                "service": SERVICE_NAME,
                "mcpEndpoint": MCP_PATH,
                "health": HEALTH_PATH,
                "transport": "MCP Streamable HTTP (stateless JSON responses)",
                "tools": ["mintbeacon_status", "verify_mint", "free_mints_feed"],
            })
            return
        if path == MCP_PATH:
            self._json(405, {
                "error": "GET event streams are not supported; use POST /mcp"
            }, {"Allow": "POST, OPTIONS"})
            return
        self._json(404, {"error": "Not found"})

    def do_DELETE(self):
        if urlsplit(self.path).path == MCP_PATH:
            self._json(405, {
                "error": "This stateless MCP endpoint does not create sessions"
            }, {"Allow": "POST, OPTIONS"})
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self):
        if urlsplit(self.path).path != MCP_PATH:
            self._json(404, {"error": "Not found"})
            return
        if self._reject_origin_or_host():
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept and "text/event-stream" not in accept:
            self._json(406, {"error": "Accept must include application/json or text/event-stream"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413 if length > MAX_BODY_BYTES else 400, {
                "error": f"Request body must be between 1 and {MAX_BODY_BYTES} bytes"
            })
            return

        limited, retry_after = rate_limited(client_key(self))
        if limited:
            self._json(429, {"error": "Rate limit exceeded; retry shortly"}, {
                "Retry-After": str(retry_after)
            })
            return

        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "Request body must be valid UTF-8 JSON"})
            return
        if not isinstance(request, dict):
            self._json(400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Expected one JSON-RPC request object; batches are not supported"},
            })
            return

        try:
            response = mcp_server.handle_request(request, self.server.mintbeacon_address)
        except Exception as exc:
            request_id = request.get("id")
            response = mcp_server.err_result(
                request_id, -32603, f"internal error: {type(exc).__name__}"
            )

        if response is None:
            self._send(202)
            return
        self._json(200, response)


def main():
    address = configured_mintbeacon()
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("MCP_PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), MintBeaconHTTPHandler)
    httpd.daemon_threads = True
    httpd.mintbeacon_address = address
    print(
        f"MintBeacon MCP HTTP listening on {host}:{port}; "
        f"endpoint=/mcp; contract={address}",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
