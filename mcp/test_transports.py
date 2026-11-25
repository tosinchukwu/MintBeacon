"""Offline smoke tests for MintBeacon's local and remote MCP transports."""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import http_server
import server as mcp_server


class StdioTransportTests(unittest.TestCase):
    def test_stdio_initialize_and_tools_list(self):
        modern_meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "offline-test", "version": "1.0"},
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "server/discover",
             "params": {"_meta": modern_meta}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list",
             "params": {"_meta": modern_meta}},
        ]
        completed = subprocess.run(
            [sys.executable, str(HERE / "server.py")],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True, capture_output=True, timeout=8,
            cwd=str(HERE),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[2]["result"]["supportedVersions"][0], "2026-07-28")
        self.assertEqual(
            responses[2]["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
            "mintbeacon",
        )
        names = {tool["name"] for tool in responses[3]["result"]["tools"]}
        self.assertEqual(names, {"mintbeacon_status", "verify_mint", "free_mints_feed"})
        self.assertEqual(responses[3]["result"]["resultType"], "complete")
        self.assertEqual(responses[3]["result"]["cacheScope"], "public")
        for tool in responses[3]["result"]["tools"]:
            self.assertTrue(tool["annotations"]["readOnlyHint"])


    def test_invalid_method_and_params_are_rejected(self):
        invalid_method = mcp_server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": [], "params": {},
        }, None)
        self.assertEqual(invalid_method["error"]["code"], -32600)
        invalid_params = mcp_server.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "initialize", "params": [],
        }, None)
        self.assertEqual(invalid_params["error"]["code"], -32602)
        invalid_arguments = mcp_server.handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "verify_mint", "arguments": []},
        }, None)
        self.assertEqual(invalid_arguments["error"]["code"], -32602)

    def test_initialize_negotiates_supported_legacy_versions(self):
        for requested, expected in (
            ("2025-11-25", "2025-11-25"),
            ("2024-11-05", "2024-11-05"),
            ("2099-01-01", "2025-11-25"),
        ):
            response = mcp_server.handle_request({
                "jsonrpc": "2.0", "id": 99, "method": "initialize",
                "params": {"protocolVersion": requested},
            }, None)
            self.assertEqual(response["result"]["protocolVersion"], expected)

class StreamableHTTPTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        http_server.ALLOWED_HOSTS.clear()
        http_server.ALLOWED_ORIGINS = {"https://mintbeacon.vercel.app"}
        http_server.RATE_LIMIT = 1000
        http_server._rate_hits.clear()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), http_server.MintBeaconHTTPHandler)
        cls.httpd.mintbeacon_address = "0x29f9AaBF15f681011334Ba131E6CBb884b39AF2B"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def post(self, payload, headers=None):
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + "/mcp", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(headers or {}),
            },
        )
        return urllib.request.urlopen(request, timeout=3)

    def test_initialize_and_tools_list_over_http(self):
        with self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }) as response:
            init = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(init["result"]["serverInfo"]["name"], "mintbeacon")

        with self.post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }) as response:
            listed = json.loads(response.read())
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertEqual(names, {"mintbeacon_status", "verify_mint", "free_mints_feed"})

    def test_modern_discover_and_tools_list_over_http(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "offline-test", "version": "1.0"},
        }
        with self.post({
            "jsonrpc": "2.0", "id": 10, "method": "server/discover",
            "params": {"_meta": meta},
        }, {
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "server/discover",
        }) as response:
            discovered = json.loads(response.read())
            self.assertEqual(discovered["result"]["supportedVersions"][0], "2026-07-28")
            self.assertEqual(discovered["result"]["resultType"], "complete")

        with self.post({
            "jsonrpc": "2.0", "id": 11, "method": "tools/list",
            "params": {"_meta": meta},
        }, {
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        }) as response:
            listed = json.loads(response.read())
            self.assertEqual(listed["result"]["cacheScope"], "public")
            self.assertEqual(listed["result"]["resultType"], "complete")
            self.assertEqual(
                listed["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
                "mintbeacon",
            )

    def test_modern_tool_call_succeeds_with_matching_routing_headers(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        with patch.object(mcp_server, "tool_mintbeacon_status", return_value={"ok": True}):
            with self.post({
                "jsonrpc": "2.0", "id": 120, "method": "tools/call",
                "params": {"_meta": meta, "name": "mintbeacon_status", "arguments": {}},
            }, {
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "mintbeacon_status",
            }) as response:
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["result"]["resultType"], "complete")
                self.assertIn('"ok": true', payload["result"]["content"][0]["text"])

    def test_modern_tool_call_requires_matching_mcp_name(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        with self.post({
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {"_meta": meta, "name": "not-a-tool", "arguments": {}},
        }, {
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "not-a-tool",
        }) as response:
            payload = json.loads(response.read())
            self.assertEqual(payload["error"]["code"], -32602)

    def test_modern_mcp_name_mismatch_is_rejected(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({
                "jsonrpc": "2.0", "id": 14, "method": "tools/call",
                "params": {"_meta": meta, "name": "verify_mint", "arguments": {}},
            }, {
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "free_mints_feed",
            })
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read())
        self.assertEqual(error["error"]["code"], -32020)

    def test_modern_header_body_mismatch_is_rejected(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({
                "jsonrpc": "2.0", "id": 13, "method": "tools/list",
                "params": {"_meta": meta},
            }, {
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
            })
        self.assertEqual(caught.exception.code, 400)
        error = json.loads(caught.exception.read())
        self.assertEqual(error["error"]["code"], -32020)

    def test_notification_returns_202(self):
        with self.post({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(response.read(), b"")

    def test_health_endpoint(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=3) as response:
            payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["transport"], "MCP Streamable HTTP")

    def test_health_and_root_reject_disallowed_origin(self):
        for path in ("/healthz", "/"):
            request = urllib.request.Request(
                self.base + path,
                headers={"Origin": "https://not-allowed.example"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 403)

    def test_host_allowlist_applies_to_health_endpoint(self):
        previous = set(http_server.ALLOWED_HOSTS)
        try:
            http_server.ALLOWED_HOSTS.clear()
            http_server.ALLOWED_HOSTS.add("allowed.example")
            request = urllib.request.Request(self.base + "/healthz")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 403)
        finally:
            http_server.ALLOWED_HOSTS.clear()
            http_server.ALLOWED_HOSTS.update(previous)

    def test_request_body_size_limit(self):
        previous = http_server.MAX_BODY_BYTES
        try:
            http_server.MAX_BODY_BYTES = 8
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            self.assertEqual(caught.exception.code, 413)
        finally:
            http_server.MAX_BODY_BYTES = previous

    def test_rate_limit_returns_429(self):
        previous = http_server.RATE_LIMIT
        try:
            http_server._rate_hits.clear()
            http_server.RATE_LIMIT = 1
            with self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            self.assertEqual(caught.exception.code, 429)
            self.assertIn("Retry-After", caught.exception.headers)
        finally:
            http_server.RATE_LIMIT = previous
            http_server._rate_hits.clear()

    def test_disallowed_origin_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"Origin": "https://not-allowed.example"},
            )
        self.assertEqual(caught.exception.code, 403)

    def test_wrong_content_type_is_rejected(self):
        request = urllib.request.Request(
            self.base + "/mcp", data=b"{}",
            headers={"Content-Type": "text/plain", "Accept": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 415)


if __name__ == "__main__":
    unittest.main()
