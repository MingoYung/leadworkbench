import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from leadworkbench.cli import ApiClient, ApiError, default_data_dir, review_payload
from leadworkbench.mcp import MCPServer, TOOLS, serve


def request(identifier, method, params=None):
    value = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        value["params"] = params
    return value


def initialize(server):
    return server.handle(request(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "fixture", "version": "1"}}))


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        route = urlsplit(path)
        if route.path == "/api/runs":
            return {"items": [{"id": f"run_{number}"} for number in range(5)]}
        if route.path == "/api/candidates":
            query = parse_qs(route.query)
            offset, limit = int(query["offset"][0]), int(query["limit"][0])
            return {"items": [{"id": f"c_{number}"} for number in range(offset, min(offset + limit, 7))], "total": 7}
        if route.path == "/api/reviews":
            raise ApiError("VERSION_CONFLICT", "Candidate changed since export.", {"candidate_id": "c_1"})
        if route.path.startswith("/api/candidates/"):
            return {"id": "c_1", "version": 3, "is_demo": True, "evidence": [{"id": "ev_1", "content": "synthetic evidence"}]}
        return {"id": "run_1", "status": "pause_requested"}


class ProtocolTests(unittest.TestCase):
    def test_initialize_and_tool_capabilities(self):
        server = MCPServer(FakeClient())
        result = initialize(server)["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(set(result["capabilities"]), {"tools"})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = server.handle(request(2, "tools/list"))["result"]["tools"]
        self.assertEqual(len(tools), 7)
        flags = {tool["name"]: tool["annotations"]["readOnlyHint"] for tool in tools}
        self.assertTrue(flags["evidence_get"])
        self.assertFalse(flags["reviews_submit"])
        self.assertFalse(flags["runs_start"])

    def test_notifications_never_execute_or_reply_and_eof_exits(self):
        fake = FakeClient()
        output = io.StringIO()
        messages = [request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "runs_start", "arguments": {"run_id": "run_1"}}},
                    request(2, "ping")]
        code = serve(fake, io.StringIO("\n".join(map(json.dumps, messages)) + "\n"), output)
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual([item["id"] for item in results], [1, 2])
        self.assertEqual(fake.calls, [])
        self.assertEqual(results[0]["result"]["protocolVersion"], "2024-11-05")

    def test_parse_errors_and_uninitialized_calls_are_errors(self):
        output = io.StringIO()
        serve(FakeClient(), io.StringIO('{broken}\n' + json.dumps(request(2, "tools/list")) + '\n'), output)
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(results[0]["error"]["code"], -32700)
        self.assertEqual(results[1]["error"]["code"], -32002)

    def test_bad_tool_schema_is_not_success(self):
        fake = FakeClient()
        server = MCPServer(fake)
        initialize(server)
        result = server.handle(request(2, "tools/call", {"name": "reviews_submit", "arguments": {
            "run_id": "run_1", "reviewer": "dsh", "operation_id": "op1", "changes": [{
                "candidate_id": "c_1", "base_version": True, "decision": "keep", "reason": "reviewed", "evidence_ids": ["ev_1"]}]}}))
        self.assertEqual(result["error"]["code"], -32602)
        self.assertNotIn("result", result)
        self.assertEqual(fake.calls, [])

    def test_business_conflict_uses_is_error(self):
        server = MCPServer(FakeClient())
        initialize(server)
        result = server.handle(request(2, "tools/call", {"name": "reviews_submit", "arguments": {
            "run_id": "run_1", "reviewer": "dsh", "operation_id": "op1", "changes": [{
                "candidate_id": "c_1", "base_version": 1, "decision": "keep", "reason": "source checked", "evidence_ids": ["ev_1"]}]}}))["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["error"]["code"], "VERSION_CONFLICT")

    def test_run_and_candidate_pagination(self):
        server = MCPServer(FakeClient())
        initialize(server)
        for tool, arguments, expected, total in [
            ("runs_list", {"limit": 2, "offset": 2}, ["run_2", "run_3"], 5),
            ("candidates_list", {"run_id": "run_1", "limit": 2, "offset": 2}, ["c_2", "c_3"], 7),
        ]:
            result = server.handle(request(3, "tools/call", {"name": tool, "arguments": arguments}))["result"]
            data = json.loads(result["content"][0]["text"])["data"]
            self.assertEqual([row["id"] for row in data["items"]], expected)
            self.assertEqual((data["total"], data["next_offset"]), (total, 4))

    def test_batch_preserves_request_ids_without_notification_responses(self):
        server = MCPServer(FakeClient())
        initialize(server)
        result = server.dispatch([request("a", "ping"), {"jsonrpc": "2.0", "method": "notifications/cancelled"}, request(9, "unknown")])
        self.assertEqual([item["id"] for item in result], ["a", 9])
        self.assertEqual(result[1]["error"]["code"], -32601)

    def test_cli_review_operation_is_stable_for_retry(self):
        review = {"run_id": "run_1", "reviewer": "codex", "changes": [{"candidate_id": "c_1", "base_version": 1}]}
        self.assertEqual(review_payload(review)["operation_id"], review_payload(review)["operation_id"])


class DataDirectoryTests(unittest.TestCase):
    def test_home_environment_precedes_legacy_data_dir(self):
        with patch.dict("os.environ", {"LEADWORKBENCH_HOME": "fixture-home", "LEADWORKBENCH_DATA_DIR": "fixture-legacy"}):
            self.assertEqual(default_data_dir(), Path("fixture-home").resolve())

    def test_desktop_onedir_uses_main_executable_data(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(Path("dist/LeadWorkbenchDesktop/LeadWorkbenchDesktop.exe").resolve())):
            self.assertEqual(default_data_dir(), Path("dist/LeadWorkbench/data").resolve())

    def test_onefile_and_main_onedir_defaults(self):
        for executable, expected in [("dist/LeadWorkbench.exe", "dist/data"), ("dist/LeadWorkbenchDesktop.exe", "dist/data"), ("dist/LeadWorkbench/LeadWorkbench.exe", "dist/LeadWorkbench/data")]:
            with self.subTest(executable=executable), patch.dict("os.environ", {}, clear=True), patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(Path(executable).resolve())):
                self.assertEqual(default_data_dir(), Path(expected).resolve())


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        received = cls.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/api/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/never-send-token")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"data":{"items":[],"total":0}}' if self.path != "/api/bad" else b'{"unwrapped":true}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, self.headers.get("X-App-Token"), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "data": {"id": "rv_fixture", "status": "applied"}}).encode())

        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.worker = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.worker.join(timeout=2)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        (self.directory / "server.json").write_text(json.dumps({"base_url": f"http://127.0.0.1:{self.http.server_port}", "token": "fixture-token"}), encoding="utf-8")
        self.client = ApiClient(self.directory, timeout=2)

    def tearDown(self):
        self.temporary.cleanup()

    def test_mcp_mutation_uses_shared_http_token_client(self):
        server = MCPServer(self.client)
        initialize(server)
        arguments = {"run_id": "run_1", "reviewer": "dsh", "operation_id": "op-http", "changes": [{
            "candidate_id": "c_1", "base_version": 1, "decision": "watch", "reason": "synthetic fixture", "evidence_ids": ["ev_1"]}]}
        result = server.handle(request(2, "tools/call", {"name": "reviews_submit", "arguments": arguments}))["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(self.received[-1], ("/api/reviews", "fixture-token", arguments))

    def test_redirect_and_missing_result_envelope_fail(self):
        with self.assertRaises(ApiError) as redirect:
            self.client.request("GET", "/api/redirect")
        self.assertEqual(redirect.exception.code, "REDIRECT_BLOCKED")
        with self.assertRaises(ApiError) as missing:
            self.client.request("GET", "/api/bad")
        self.assertEqual(missing.exception.code, "INVALID_RESPONSE")

    def test_remote_server_file_is_rejected_before_network(self):
        (self.directory / "server.json").write_text(json.dumps({"base_url": "https://example.invalid", "token": "fixture-token"}), encoding="utf-8")
        with self.assertRaises(ApiError) as error:
            self.client.connection()
        self.assertEqual(error.exception.code, "SERVER_CONFIG")

    def test_stdio_process_closes_on_eof_and_stdout_contains_only_json(self):
        messages = [request(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}, request(2, "tools/list")]
        completed = subprocess.run([sys.executable, "-m", "leadworkbench", "--data-dir", str(self.directory), "mcp"],
            input="\n".join(map(json.dumps, messages)) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=10,
            cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        output = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([item["id"] for item in output], [1, 2])
        self.assertNotIn("fixture-token", completed.stdout)

    def test_cli_argument_error_keeps_nonzero_process_exit(self):
        completed = subprocess.run([sys.executable, "-m", "leadworkbench", "unknown-command"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
            cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "ARGUMENT")


if __name__ == "__main__":
    unittest.main()
