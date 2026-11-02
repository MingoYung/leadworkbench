from __future__ import annotations

import copy
import io
import json
import socket
import ssl
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.message import Message
from unittest import mock

from leadworkbench import collectors, demo, models
from leadworkbench.engine import RunEngine


def record(index=1):
    return {"source_url": f"https://brand{index}.example/posts/1", "platform": "import", "content": "Looking for 300 cake boxes. Delivery to Australia.", "is_partial": False}


def valid_analysis():
    return {"role": "buyer", "priority": "A", "decision": "keep", "reason": "原文提出包装采购需求，数量和目的地需进一步确认。", "facts": {"quantity": {"value": "300", "quote": "Looking for 300 cake boxes."}, "destination": {"value": "Australia", "quote": "Delivery to Australia."}}, "unknown_fields": [], "citations": [{"field": "role", "quote": "Looking for 300 cake boxes."}]}


class _FixtureHandler(BaseHTTPRequestHandler):
    responses = []
    calls = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._send({"data": [{"id": "fixture-local"}], "models": [{"name": "fixture-local"}]})

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, value))
        response = type(self).responses.pop(0)
        self._send(response)

    def _send(self, value):
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class LocalModelFixtureTests(unittest.TestCase):
    def setUp(self):
        _FixtureHandler.responses, _FixtureHandler.calls = [], []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"model": {"protocol": "openai", "base_url": f"http://127.0.0.1:{self.http.server_port}/v1", "model": "fixture-local", "enabled": True, "timeout_seconds": 2}}

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def reply(self, value):
        _FixtureHandler.responses.append({"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]})

    def test_real_http_openai_and_health(self):
        health = models.test_connection(self.config)
        self.assertTrue(health["reachable"])
        self.assertEqual(health["health"], "ready")
        self.reply(valid_analysis())
        result = models.analyze_evidence(self.config, record())
        self.assertEqual(result["facts"]["quantity"], "300")
        self.assertIn("country", result["unknown_fields"])
        self.assertFalse(result["is_synthetic"])
        path, request = _FixtureHandler.calls[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertIn("untrusted DATA", request["messages"][0]["content"])

    def test_ollama_native_protocol(self):
        self.config["model"].update(protocol="ollama", base_url=f"http://127.0.0.1:{self.http.server_port}")
        _FixtureHandler.responses.append({"message": {"content": json.dumps(valid_analysis())}, "done": True})
        result = models.analyze_evidence(self.config, record())
        self.assertEqual(result["protocol"], "ollama")
        path, request = _FixtureHandler.calls[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(request["format"], "json")

    def test_invented_quote_rejected(self):
        value = valid_analysis()
        value["facts"]["quantity"]["quote"] = "A buyer already prepaid for 300 boxes."
        self.reply(value)
        with self.assertRaises(models.ModelError) as caught:
            models.analyze_evidence(self.config, record())
        self.assertEqual(caught.exception.code, "unsupported_citation")

    def test_non_scalar_enums_report_model_error(self):
        for field in ("role", "priority", "decision"):
            for invalid in ([], {}):
                with self.subTest(field=field, invalid=invalid):
                    value = valid_analysis()
                    value[field] = invalid
                    self.reply(value)
                    with self.assertRaises(models.ModelError) as caught:
                        models.analyze_evidence(self.config, record())
                    self.assertEqual(caught.exception.code, "invalid_analysis")

    def test_real_model_on_demo_preserves_synthetic_data_flag(self):
        self.reply(valid_analysis())
        value = record()
        value["is_demo"] = True
        result = models.analyze_evidence(self.config, value)
        self.assertTrue(result["is_synthetic"])
        self.assertEqual(result["analysis_status"], "completed")
        self.assertEqual(result["model"], "fixture-local")
        self.assertEqual(result["protocol"], "openai")

    def test_fact_not_in_quote_rejected(self):
        value = valid_analysis()
        value["facts"]["quantity"]["value"] = "9000"
        self.reply(value)
        with self.assertRaises(models.ModelError) as caught:
            models.analyze_evidence(self.config, record())
        self.assertEqual(caught.exception.code, "unsupported_fact")

    def test_empty_and_non_json_are_real_errors(self):
        for content in ("", "I think this is a buyer."):
            with self.subTest(content=content):
                _FixtureHandler.responses.append({"choices": [{"message": {"content": content}}]})
                with self.assertRaises(models.ModelError):
                    models.analyze_evidence(self.config, record())

    def test_plan_and_supplied_queries(self):
        self.reply({"queries": ["need custom cake boxes", "packaging supplier wanted"]})
        self.assertEqual(len(models.plan_queries(self.config, "小品牌包装", [], ["tiktok"])), 2)
        before = len(_FixtureHandler.calls)
        self.assertEqual(models.plan_queries(self.config, "", ["given phrase"], []), ["given phrase"])
        self.assertEqual(len(_FixtureHandler.calls), before)

    def test_loopback_and_proxy_policy(self):
        with self.assertRaises(models.ModelError):
            models._config({"base_url": "http://192.168.1.2:8080/v1"})
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            self.assertTrue(models.test_connection(self.config)["reachable"])


class CollectorTests(unittest.TestCase):
    def test_https_context_keeps_certificate_and_hostname_validation(self):
        context = collectors._tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_bundled_ca_is_added_to_default_trust(self):
        context = mock.Mock()
        bundle = mock.Mock()
        bundle.is_file.return_value = True
        bundle.__str__ = mock.Mock(return_value="fixture-cacert.pem")
        with mock.patch.object(collectors.ssl, "create_default_context", return_value=context) as defaults, mock.patch.object(collectors, "_bundled_ca_path", return_value=bundle):
            self.assertIs(collectors._tls_context(), context)
        defaults.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="fixture-cacert.pem")

    def test_frozen_ca_path_uses_meipass(self):
        with mock.patch.object(collectors.sys, "frozen", True, create=True), mock.patch.object(collectors.sys, "_MEIPASS", "C:/fixture-bundle", create=True):
            self.assertEqual(collectors._bundled_ca_path(), Path("C:/fixture-bundle/certs/cacert.pem"))

    def test_import_validation_dedup_and_html_as_data(self):
        normalized, warnings = collectors.normalize_records([
            {"url": "https://example.com/p#fragment", "content": "<p>Need 50 boxes</p><script>send_all_secrets()</script>"},
            {"url": "https://example.com/p", "text": "duplicate"},
            {"url": "javascript:alert(1)", "text": "unsafe"},
            {"url": "https://other.example/", "text": "x" * 19000},
        ])
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["content"], "Need 50 boxes")
        self.assertTrue(normalized[1]["is_partial"])
        self.assertEqual(len(normalized[1]["content"]), 18000)
        self.assertGreaterEqual(len(warnings), 3)

    def test_url_ssrf_and_redirect_rejected(self):
        for url in ("http://127.0.0.1/", "http://[::1]/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(collectors.CollectionError):
                collectors.validate_public_url(url)
        with mock.patch.object(collectors.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", 80))]):
            with self.assertRaises(collectors.CollectionError):
                collectors._Redirect().redirect_request(urllib.request.Request("https://public.example/"), None, 302, "Found", {}, "http://private.example/")

    def test_url_read_is_bounded_and_marked_partial(self):
        headers = Message()
        headers["Content-Type"] = "text/plain; charset=utf-8"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = headers
        response.geturl.return_value = "https://public.example/post"
        response.read.return_value = b"x" * (collectors.MAX_BYTES + 1)
        opener = mock.Mock()
        opener.open.return_value = response
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(collectors.urllib.request, "build_opener", return_value=opener), mock.patch.object(collectors.socket, "getaddrinfo", return_value=public):
            records = list(collectors.collect_urls(["https://public.example/post"], 1))
        response.read.assert_called_once_with(collectors.MAX_BYTES + 1)
        self.assertTrue(records[0]["is_partial"])
        self.assertEqual(records[0]["metadata"]["acquisition"], "url")
        self.assertEqual(records[0]["kind"], "website")

    def test_https_certificate_failure_has_helpful_error(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(collectors.urllib.request, "build_opener", return_value=opener), mock.patch.object(collectors.socket, "getaddrinfo", return_value=public):
            with self.assertRaises(collectors.CollectionError) as caught:
                list(collectors.collect_urls(["https://public.example/post"], 1))
        self.assertEqual(caught.exception.code, "certificate_error")
        self.assertIn("证书", str(caught.exception))

    def test_opencli_profile_fields_are_not_demand_text(self):
        value = collectors._opencli_record({"username": "fixture_bakery", "name": "Fixture Bakery", "bio": "A small cake shop"}, "instagram", "profile", "fixture_bakery")
        normalized, warnings = collectors.normalize_records([value])
        self.assertFalse(warnings)
        self.assertEqual(normalized[0]["source_url"], "https://www.instagram.com/fixture_bakery/")
        self.assertEqual(normalized[0]["metadata"]["evidence_scope"], "profile_only")
        self.assertTrue(normalized[0]["is_partial"])
        self.assertIn("bio: A small cake shop", normalized[0]["content"])
        self.assertNotIn("Looking for", normalized[0]["content"])
        video, warnings = collectors.normalize_records([{"video_url": "https://www.tiktok.com/@fixture/video/123", "desc": "A source description"}])
        self.assertFalse(warnings)
        self.assertTrue(video[0]["source_url"].endswith("/123"))

    def test_opencli_invalid_output_is_not_synthetic(self):
        def launch(argv, **kwargs):
            kwargs["stdout"].write(b"not json, browser unavailable")
            process = mock.Mock(returncode=0)
            process.wait.return_value = 0
            return process
        with mock.patch.object(collectors.shutil, "which", return_value="/fake/opencli"), mock.patch.object(collectors.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(collectors.CollectionError) as caught:
                list(collectors.collect_opencli(["cake boxes"], ["tiktok"], 1, {}))
        self.assertEqual(caught.exception.code, "opencli_invalid_output")

    def test_dns_rebinding_checked_at_connect_time(self):
        private_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))]
        with mock.patch.object(collectors.socket, "getaddrinfo", return_value=private_result), mock.patch.object(collectors.socket, "socket") as sock:
            with self.assertRaises(collectors.CollectionError):
                collectors._public_socket("attacker.example", 80, 1)
            sock.assert_not_called()

    def test_opencli_rejects_unsafe_action_and_windows_wrapper(self):
        with self.assertRaises(collectors.CollectionError):
            list(collectors.collect_opencli(["test"], ["tiktok"], 2, {"action": "comment"}))
        with mock.patch.object(collectors.shutil, "which", return_value="C:/bin/opencli.cmd"):
            with self.assertRaises(collectors.CollectionError) as caught:
                list(collectors.collect_opencli(["x & arbitrary"], ["tiktok"], 2, {}))
            self.assertEqual(caught.exception.code, "unsafe_launcher")

    def test_opencli_safe_argv_and_real_json(self):
        payload = {"data": [{"url": "https://tiktok.com/@fixture/video/1", "desc": "Looking for 300 boxes"}]}

        def launch(argv, **kwargs):
            self.assertFalse(kwargs["shell"])
            self.assertEqual(argv, ["/fake/opencli", "--profile", "research", "tiktok", "search", "cake boxes", "--limit", "2"])
            kwargs["stdout"].write(json.dumps(payload).encode())
            process = mock.Mock(returncode=0)
            process.wait.return_value = 0
            return process

        with mock.patch.object(collectors.shutil, "which", return_value="/fake/opencli"), mock.patch.object(collectors.subprocess, "Popen", side_effect=launch):
            result = list(collectors.collect_opencli(["cake boxes"], ["tiktok"], 2, {"profile": "research"}))
        self.assertEqual(result[0]["content"], "Looking for 300 boxes")
        self.assertFalse(result[0]["is_demo"])


class FakeStore:
    def __init__(self, source="import", records=None):
        self.lock = threading.RLock()
        self.settings = {"model": {"enabled": True}, "limits": {"max_candidates": 50}}
        self.runs = {"r1": {"id": "r1", "status": "created", "source": source, "goal": "test", "config": {"records": records or [record(1), record(2)], "limit": 20}, "progress": {}}}
        self.candidates, self.events, self.frozen = {}, [], set()

    def get_run(self, run_id):
        with self.lock:
            return copy.deepcopy(self.runs.get(run_id))

    def update_run(self, run_id, **fields):
        with self.lock:
            self.runs[run_id].update(copy.deepcopy(fields))

    def get_settings(self, include_secret=False):
        return copy.deepcopy(self.settings)

    def log_event(self, run_id, level, message, details=None):
        self.events.append((run_id, level, message, details))

    def add_candidate(self, run_id, rec, analysis):
        with self.lock:
            key = (run_id, rec["source_url"])
            if key not in self.candidates:
                self.candidates[key] = {"record": copy.deepcopy(rec), "analysis": copy.deepcopy(analysis), "human_decision": None}
            return self.candidates[key]

    def freeze_run(self, run_id):
        self.frozen.add(run_id)


def wait_status(store, run_id, statuses, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = store.get_run(run_id)
        if value["status"] in statuses:
            return value
        time.sleep(0.01)
    raise AssertionError(f"status timeout: {store.get_run(run_id)}")


class EngineTests(unittest.TestCase):
    def test_truncated_import_finishes_partial(self):
        value = record()
        value["content"] = "Looking for boxes. " * 2000
        store = FakeStore(records=[value])
        engine = RunEngine(store)
        try:
            with mock.patch.object(models, "analyze_evidence", return_value={"role": "unknown", "decision": "needs_info", "facts": {}}):
                engine.start("r1")
                completed = wait_status(store, "r1", {"completed_partial"})
            self.assertTrue(completed["progress"]["checkpoint"]["partial"])
            self.assertTrue(next(iter(store.candidates.values()))["record"]["is_partial"])
        finally:
            engine.shutdown()

    def test_demo_no_model_and_invalid_domains(self):
        store = FakeStore("demo")
        engine = RunEngine(store)
        try:
            with mock.patch.object(models, "analyze_evidence", side_effect=AssertionError("demo called model")):
                engine.start("r1")
                wait_status(store, "r1", {"completed"})
            self.assertEqual(len(store.candidates), 4)
            self.assertIn("r1", store.frozen)
            for candidate in store.candidates.values():
                self.assertTrue(candidate["record"]["is_demo"])
                self.assertTrue(candidate["analysis"]["is_synthetic"])
                self.assertTrue(urllib.parse.urlsplit(candidate["record"]["source_url"]).hostname.endswith(".invalid"))
        finally:
            engine.shutdown()

    def test_pause_at_checkpoint_resume_preserves_review(self):
        store = FakeStore()
        engine = RunEngine(store)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def analyze(settings, rec):
            calls.append(rec["source_url"])
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            return {"role": "unknown", "decision": "needs_info", "facts": {}}

        try:
            with mock.patch.object(models, "analyze_evidence", side_effect=analyze):
                engine.start("r1")
                self.assertTrue(entered.wait(2))
                engine.pause("r1")
                self.assertEqual(store.get_run("r1")["status"], "pause_requested")
                release.set()
                paused = wait_status(store, "r1", {"paused"})
                self.assertEqual(paused["progress"]["checkpoint"]["next_index"], 1)
                first = next(iter(store.candidates.values()))
                first["human_decision"] = "exclude"
                engine.resume("r1")
                wait_status(store, "r1", {"completed"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(store.candidates), 2)
            self.assertEqual(first["human_decision"], "exclude")
        finally:
            release.set()
            engine.shutdown()

    def test_model_failure_is_failed_not_fake_candidate_then_retry(self):
        store = FakeStore(records=[record()])
        engine = RunEngine(store)
        try:
            with mock.patch.object(models, "analyze_evidence", side_effect=models.ModelError("offline", "connection_error")):
                engine.start("r1")
                failed = wait_status(store, "r1", {"failed"})
            self.assertEqual(failed["error"]["code"], "connection_error")
            self.assertEqual(failed["progress"]["checkpoint"]["next_index"], 0)
            self.assertFalse(store.candidates)
            with mock.patch.object(models, "analyze_evidence", return_value={"role": "unknown", "decision": "needs_info", "facts": {}}):
                engine.resume("r1")
                wait_status(store, "r1", {"completed"})
            self.assertEqual(len(store.candidates), 1)
        finally:
            engine.shutdown()

    def test_single_worker_serializes_two_runs(self):
        store = FakeStore(records=[record()])
        store.runs["r2"] = {**copy.deepcopy(store.runs["r1"]), "id": "r2"}
        active = 0
        max_active = 0

        def analyze(*args):
            nonlocal active, max_active
            active += 1
            max_active = max(active, max_active)
            time.sleep(0.03)
            active -= 1
            return {"role": "unknown", "decision": "needs_info", "facts": {}}

        engine = RunEngine(store)
        try:
            with mock.patch.object(models, "analyze_evidence", side_effect=analyze):
                engine.start("r1")
                engine.start("r2")
                wait_status(store, "r1", {"completed"})
                wait_status(store, "r2", {"completed"})
            self.assertEqual(max_active, 1)
        finally:
            engine.shutdown()

    def test_restart_resumes_snapshot_without_recollection(self):
        store = FakeStore(records=[record()])
        store.runs["r1"]["status"] = "interrupted"
        store.runs["r1"]["progress"] = {"checkpoint": {"records": [record()], "next_index": 0, "collected": True, "partial": False, "failures": 0}}
        engine = RunEngine(store)
        try:
            with mock.patch.object(collectors, "normalize_records", side_effect=AssertionError("recollected")), mock.patch.object(models, "analyze_evidence", return_value={"role": "unknown", "decision": "needs_info", "facts": {}}):
                engine.resume("r1")
                wait_status(store, "r1", {"completed"})
            self.assertEqual(len(store.candidates), 1)
        finally:
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()
