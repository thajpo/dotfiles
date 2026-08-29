import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.pisec.responses_gateway import (  # noqa: E402
    RequestRewriteError,
    ResponsesGatewayServer,
    restore_responses_namespaces,
    rewrite_responses_request,
    rewrite_sse_line,
)


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict[str, object]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["content-length"]))
        document = json.loads(body)
        self.requests.append(document)
        flat_name = next(tool["name"] for tool in document["tools"] if tool.get("name", "").startswith("mcp__pisec__"))
        events = (
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": flat_name,
                    "arguments": "{}",
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "id": "item-1",
                            "call_id": "call-1",
                            "name": flat_name,
                            "arguments": "{}",
                        }
                    ]
                },
            },
        )
        payload = b"".join(b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n" for event in events)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ResponsesGatewayUnitTests(unittest.TestCase):
    def test_request_rewrite_is_reversible_and_normalizes_null_reasoning(self):
        body = {
            "input": [
                {"type": "reasoning", "summary": [], "content": None},
                {
                    "type": "function_call",
                    "namespace": "mcp__pisec",
                    "name": "pisec_checkpoint_workstream",
                    "call_id": "call-prior",
                    "arguments": "{}",
                },
            ],
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                {
                    "type": "namespace",
                    "name": "mcp__pisec",
                    "description": "Pisec coordination tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "pisec_checkpoint_workstream",
                            "description": "Record progress",
                            "strict": True,
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                },
            ],
        }

        rewritten, namespace_map = rewrite_responses_request(body)

        self.assertNotIn("content", rewritten["input"][0])
        self.assertEqual(rewritten["input"][1]["name"], "mcp__pisec__pisec_checkpoint_workstream")
        self.assertNotIn("namespace", rewritten["input"][1])
        self.assertEqual([tool["type"] for tool in rewritten["tools"]], ["function", "function"])
        self.assertEqual(rewritten["tools"][1]["name"], "mcp__pisec__pisec_checkpoint_workstream")
        self.assertTrue(rewritten["tools"][1]["strict"])
        self.assertEqual(
            namespace_map,
            {"mcp__pisec__pisec_checkpoint_workstream": ("mcp__pisec", "pisec_checkpoint_workstream")},
        )
        self.assertIsNone(body["input"][0]["content"], "the caller's request must not be mutated")

        output = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "name": "mcp__pisec__pisec_checkpoint_workstream",
                        "call_id": "call-new",
                    }
                ]
            },
        }
        self.assertEqual(restore_responses_namespaces(output, namespace_map), 1)
        call = output["response"]["output"][0]
        self.assertEqual(call["namespace"], "mcp__pisec")
        self.assertEqual(call["name"], "pisec_checkpoint_workstream")

    def test_request_rewrite_rejects_flat_name_collisions(self):
        with self.assertRaisesRegex(RequestRewriteError, "collides"):
            rewrite_responses_request(
                {
                    "tools": [
                        {"type": "function", "name": "mcp__pisec__submit", "parameters": {}},
                        {
                            "type": "namespace",
                            "name": "mcp__pisec",
                            "tools": [{"type": "function", "name": "submit", "parameters": {}}],
                        },
                    ]
                }
            )

    def test_sse_rewrite_preserves_event_framing(self):
        line = b'data: {"type":"response.output_item.added","item":{"type":"function_call","name":"mcp__pisec__submit"}}\r\n'
        rewritten = rewrite_sse_line(line, {"mcp__pisec__submit": ("mcp__pisec", "submit")})
        self.assertTrue(rewritten.endswith(b"\r\n"))
        event = json.loads(rewritten[6:].strip())
        self.assertEqual(event["item"]["namespace"], "mcp__pisec")
        self.assertEqual(event["item"]["name"], "submit")


class ResponsesGatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        _UpstreamHandler.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        upstream_address = self.upstream.server_address
        self.gateway = ResponsesGatewayServer(("127.0.0.1", 0), (str(upstream_address[0]), int(upstream_address[1])))
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.gateway_thread.join(timeout=5)
        self.upstream_thread.join(timeout=5)

    def test_gateway_streams_rewritten_namespace_calls(self):
        request = {
            "model": "gpt-5.6-luna",
            "input": [{"type": "reasoning", "summary": [], "content": None}],
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__pisec",
                    "tools": [
                        {
                            "type": "function",
                            "name": "pisec_submit_completion",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ],
            "stream": True,
        }
        connection = http.client.HTTPConnection(*self.gateway.server_address, timeout=5)
        connection.request(
            "POST",
            "/v1/responses",
            body=json.dumps(request).encode(),
            headers={"content-type": "application/json", "authorization": "Bearer test"},
        )
        response = connection.getresponse()
        payload = response.read().decode()
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(len(_UpstreamHandler.requests), 1)
        forwarded = _UpstreamHandler.requests[0]
        self.assertNotIn("content", forwarded["input"][0])
        self.assertEqual(forwarded["tools"][0]["name"], "mcp__pisec__pisec_submit_completion")
        events = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith("data: ")]
        calls = [events[0]["item"], events[1]["response"]["output"][0]]
        self.assertTrue(all(call["namespace"] == "mcp__pisec" for call in calls))
        self.assertTrue(all(call["name"] == "pisec_submit_completion" for call in calls))

    def test_gateway_passes_health_checks_without_responses_translation(self):
        connection = http.client.HTTPConnection(*self.gateway.server_address, timeout=5)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        payload = response.read()
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload), {"ok": True})


if __name__ == "__main__":
    unittest.main()
