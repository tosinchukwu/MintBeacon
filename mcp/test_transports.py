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
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        completed = subprocess.run(
            [sys.executable, str(HERE / "server.py")],
            input="\n".join(json.dumps(item) for item in requests) + "\n",
            text=True, capture_output=True, timeout=8,
            cwd=str(HERE),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "mintbeacon")
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"mintbeacon_status", "verify_mint", "free_mints_feed"})
        for tool in responses[1]["result"]["tools"]:
            self.assertTrue(tool["annotations"]["readOnlyHint"])


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
            "params": {"protocolVersion": "2024-11-05"},
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
