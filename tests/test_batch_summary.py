"""Read-only batch statistics and independent AI suggestion query contracts.

These fixtures use temporary stores and synthetic input text. They never start
collection, call a model, contact a social platform or modify user data.
"""

import copy
import http.client
import json
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from leadworkbench.server import WorkbenchServer, public_run
from leadworkbench.store import AppError, Store


_MISSING = object()


class _BatchFixtures:
    def new_run(self, source="import", mode="local", limit=20):
        payload = {"source": source, "name": "统计测试", "analysis_mode": mode, "limit": limit}
        if source == "import":
            payload["records"] = [{"source_url": "https://fixture.invalid/input", "content": "仅用于创建批次的输入原文。"}]
        return self.store.create_run(payload)

    def add(self, *, run=None, decision="keep", priority="B", status="completed",
            text=None, synthetic=_MISSING, is_synthetic=_MISSING, material_demo=False):
        self.serial += 1
        record = {
            "source_url": f"https://fixture.invalid/evidence/{self.serial}",
            "platform": "weibo", "author_handle": f"author-{self.serial}",
            "content": text or f"Source evidence {self.serial}: requesting a service quote.",
            "is_demo": material_demo,
        }
        analysis = {"role": "consumer", "decision": decision, "priority": priority,
                    "reason": "fixture analysis only", "facts": {}, "model": "fixture-model"}
        if status is not _MISSING:
            analysis["analysis_status"] = status
        if synthetic is not _MISSING:
            analysis["synthetic"] = synthetic
        if is_synthetic is not _MISSING:
            analysis["is_synthetic"] = is_synthetic
        return self.store.add_candidate((run or self.run)["id"], record, analysis)

    def human_review(self, candidate, decision="exclude"):
        current = self.store.get_candidate(candidate["id"])
        return self.store.submit_review({
            "run_id": current["run_id"], "reviewer": "human",
            "operation_id": "fixture-human-" + candidate["id"],
            "changes": [{"candidate_id": current["id"], "base_version": current["version"],
                         "decision": decision, "reason": "人工依据原文作出独立决定。",
                         "evidence_ids": [current["evidence"][0]["id"]]}],
        })

    def ids(self, **query):
        return [item["id"] for item in self.store.list_candidates(self.run["id"], **query)["items"]]


class BatchSummaryTests(_BatchFixtures, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.serial = 0
        self.run = self.new_run()

    def test_checkpoint_collected_and_actual_processed_are_different_counts(self):
        self.add()
        self.add(decision="watch")
        records = [{"content": f"private buffered source {n}"} for n in range(5)]
        self.store.update_run(self.run["id"], status="running", progress={
            "current": 4, "total": 5,
            "checkpoint": {"records": records, "next_index": 4, "collected": True},
        })
        summary = self.store.get_run(self.run["id"])["batch_summary"]
        self.assertEqual(5, summary["collected"])
        self.assertEqual(2, summary["processed"])
        self.assertEqual("checkpoint", summary["collected_basis"])
        self.assertIs(True, summary["collection_complete"])
        self.assertEqual(2, summary["ai_analyzed"])

    def test_missing_checkpoint_uses_saved_rows_and_unknown_completeness(self):
        self.add(status="not_requested")
        self.add(status="not_requested")
        self.store.update_run(self.run["id"], progress={"current": 99, "total": 100})
        summary = self.store.get_run(self.run["id"])["batch_summary"]
        self.assertEqual(2, summary["collected"])
        self.assertEqual(2, summary["processed"])
        self.assertEqual("stored_candidates", summary["collected_basis"])
        self.assertIsNone(summary["collection_complete"])
        self.assertEqual(0, summary["ai_analyzed"])

    def test_repeated_add_does_not_inflate_processed(self):
        record = {"source_url": "https://fixture.invalid/duplicate", "content": "One immutable source."}
        analysis = {"analysis_status": "completed", "decision": "keep", "priority": "A"}
        first = self.store.add_candidate(self.run["id"], record, analysis)
        second = self.store.add_candidate(self.run["id"], record, analysis)
        self.store.update_run(self.run["id"], progress={
            "checkpoint": {"records": [record, record, record], "next_index": 3, "collected": True},
        })
        summary = self.store.get_run(self.run["id"])["batch_summary"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(3, summary["collected"])
        self.assertEqual(1, summary["processed"])
        self.assertEqual(1, summary["ai_analyzed"])

    def test_effective_limit_respects_stored_limit_settings_and_engine_cap(self):
        self.store.update_settings({"limits": {"max_candidates": 1000}})
        run = self.new_run(limit=120)
        summary = self.store.get_run(run["id"])["batch_summary"]
        self.assertEqual(120, run["config"]["limit"])
        self.assertEqual(120, summary["requested_limit"])
        self.assertEqual(50, summary["effective_limit"])
        self.store.update_settings({"limits": {"max_candidates": 12}})
        summary = self.store.get_run(run["id"])["batch_summary"]
        self.assertEqual(120, summary["requested_limit"])
        self.assertEqual(12, summary["effective_limit"])
        small = self.new_run(limit=7)
        self.assertEqual(7, small["batch_summary"]["effective_limit"])

    def test_ai_counts_distinguish_completed_synthetic_and_not_requested(self):
        for decision in ("keep", "needs_info", "watch", "exclude"):
            self.add(decision=decision)
        self.add(status="synthetic_demo")
        self.add(status="not_requested")
        self.add(status=_MISSING)
        self.add(status=None)
        self.add(status="completed", synthetic=True)
        summary = self.store.get_run(self.run["id"])["batch_summary"]
        self.assertEqual(9, summary["processed"])
        self.assertEqual(4, summary["ai_analyzed"])
        self.assertEqual(5, summary["unanalysed"])
        self.assertEqual(1, summary["synthetic"])
        self.assertEqual(1, summary["not_requested"])
        self.assertEqual({"keep": 1, "needs_info": 1, "watch": 1, "exclude": 1}, summary["ai_suggestions"])

    def test_real_ai_can_analyze_material_marked_demo_or_is_synthetic(self):
        self.add(material_demo=True)
        self.add(is_synthetic=True)
        self.add(material_demo=True, is_synthetic=True)
        for flag in (False, None, 1):
            self.add(synthetic=flag)
        summary = self.store.get_run(self.run["id"])["batch_summary"]
        self.assertEqual(6, summary["ai_analyzed"])
        self.assertEqual(6, summary["ai_suggestions"]["keep"])
        self.assertEqual(0, summary["synthetic"])
        self.assertTrue(all(item["ai_decision"] == "keep" for item in self.store.list_candidates(self.run["id"])["items"]))

    def test_demo_source_is_never_counted_as_real_ai(self):
        run = self.new_run(source="demo")
        completed = self.add(run=run)
        self.add(run=run, status="synthetic_demo")
        summary = self.store.get_run(run["id"])["batch_summary"]
        self.assertEqual(0, summary["ai_analyzed"])
        self.assertEqual(1, summary["synthetic"])
        self.assertEqual(2, summary["unanalysed"])
        self.assertEqual({"keep": 0, "needs_info": 0, "watch": 0, "exclude": 0}, summary["ai_suggestions"])
        self.assertIsNone(self.store.get_candidate(completed["id"])["ai_decision"])
        self.assertTrue(all(item["ai_decision"] is None for item in self.store.list_candidates(run["id"])["items"]))

    def test_ai_decision_is_consistent_between_detail_and_list(self):
        examples = [
            ("completed", _MISSING, "keep"),
            ("not_requested", _MISSING, None),
            ("synthetic_demo", _MISSING, None),
            (_MISSING, _MISSING, None),
            ("completed", True, None),
        ]
        expected = {}
        for status, flag, suggestion in examples:
            candidate = self.add(status=status, synthetic=flag)
            expected[candidate["id"]] = suggestion
            self.assertEqual(suggestion, self.store.get_candidate(candidate["id"])["ai_decision"])
        actual = {item["id"]: item["ai_decision"] for item in self.store.list_candidates(self.run["id"])["items"]}
        self.assertEqual(expected, actual)

    def test_terminal_statuses_override_checkpoint_phase(self):
        for status, expected in [("created", "created"), ("completed", "completed"),
                                 ("completed_partial", "completed"), ("paused", "paused"),
                                 ("failed", "failed"), ("interrupted", "interrupted"),
                                 ("pause_requested", "pause_requested")]:
            with self.subTest(status=status):
                self.store.update_run(self.run["id"], status=status, progress={
                    "checkpoint": {"records": [{"content": "source"}], "next_index": 0, "collected": True},
                })
                run = self.store.get_run(self.run["id"])
                self.assertEqual(status, run["status"])
                self.assertEqual(expected, run["batch_summary"]["phase"])
                self.assertEqual(status == "completed_partial", run["batch_summary"]["is_partial"])

    def test_running_phase_follows_collection_mode_and_checkpoint(self):
        cases = [
            ("local", None, "collecting"),
            ("local", {"records": [], "next_index": 0, "collected": False}, "collecting"),
            ("local", {"records": [{"content": "source"}], "next_index": 0}, "collecting"),
            ("local", {"records": [{"content": "source"}], "next_index": 0, "collected": True}, "analyzing"),
            ("local", {"records": [{"content": "source"}], "next_index": 1, "collected": True}, "saving"),
            ("local", {"records": [], "next_index": 0, "collected": True}, "saving"),
            ("collect_only", {"records": [{"content": "source"}], "next_index": 0, "collected": True}, "saving"),
        ]
        for mode, checkpoint, expected in cases:
            with self.subTest(mode=mode, checkpoint=checkpoint):
                config = copy.deepcopy(self.store.get_run(self.run["id"])["config"])
                config["analysis_mode"] = mode
                self.store.update_run(self.run["id"], status="running", config=config,
                                      progress={} if checkpoint is None else {"checkpoint": checkpoint})
                self.assertEqual(expected, self.store.get_run(self.run["id"])["batch_summary"]["phase"])
        demo = self.new_run(source="demo", mode="local")
        self.store.update_run(demo["id"], status="running", progress={
            "checkpoint": {"records": [{"content": "source"}], "next_index": 0, "collected": True},
        })
        self.assertEqual("saving", self.store.get_run(demo["id"])["batch_summary"]["phase"])

    def test_summary_reads_preserve_human_decision_versions_and_evidence(self):
        candidate = self.add(decision="keep")
        self.human_review(candidate, "exclude")
        before = self.store.get_candidate(candidate["id"])
        for _ in range(2):
            run = self.store.get_run(self.run["id"])
            self.assertEqual(1, run["counts"]["exclude"])
            self.assertEqual(1, run["batch_summary"]["ai_suggestions"]["keep"])
            self.store.list_runs()
        self.assertEqual(before, self.store.get_candidate(candidate["id"]))
        self.assertEqual("exclude", before["effective_decision"])
        self.assertEqual("keep", before["ai_decision"])

    def test_public_summary_retains_counts_without_private_input_or_checkpoint(self):
        self.add()
        self.store.update_run(self.run["id"], progress={
            "checkpoint": {"records": [{"content": "PRIVATE-CHECKPOINT-TEXT"}] * 3,
                           "collected": False, "next_index": 0},
        })
        internal = self.store.get_run(self.run["id"])
        public = public_run(internal)
        self.assertEqual(internal["batch_summary"], public["batch_summary"])
        self.assertEqual(3, public["batch_summary"]["collected"])
        self.assertNotIn("checkpoint", public["progress"])
        self.assertNotIn("records", public["config"])
        self.assertNotIn("PRIVATE-CHECKPOINT-TEXT", json.dumps(public))
        self.assertIn("checkpoint", internal["progress"])


class AiCandidateQueryTests(_BatchFixtures, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.serial = 0
        self.run = self.new_run()

    def test_default_and_explicit_collected_order_keep_legacy_insertion(self):
        rows = [self.add(decision="exclude"), self.add(decision="keep"), self.add(status="not_requested")]
        expected = [row["id"] for row in rows]
        self.assertEqual(expected, self.ids())
        self.assertEqual(expected, self.ids(order="collected"))
        legacy = self.store.list_candidates(self.run["id"], "", "", 100, 0)
        self.assertEqual(expected, [item["id"] for item in legacy["items"]])

    def test_ai_order_groups_then_priorities_and_stable_insertion(self):
        exclude = self.add(decision="exclude", priority="A")
        raw_c = self.add(status="not_requested", priority="C")
        watch = self.add(decision="watch", priority="B")
        keep_c = self.add(decision="keep", priority="C")
        needs = self.add(decision="needs_info", priority="A")
        keep_a1 = self.add(decision="keep", priority="A")
        raw_a1 = self.add(status="not_requested", priority="A")
        keep_a2 = self.add(decision="keep", priority="A")
        keep_b = self.add(decision="keep", priority="B")
        raw_a2 = self.add(status="not_requested", priority="A")
        raw_unknown = self.add(status=_MISSING, priority="unknown")
        expected = [keep_a1, keep_a2, keep_b, keep_c, needs, watch, exclude, raw_a1, raw_a2, raw_c, raw_unknown]
        self.assertEqual([row["id"] for row in expected], self.ids(order="ai_priority"))
        self.assertEqual([row["id"] for row in expected], self.ids(order="ai_priority"))

    def test_ai_filters_use_only_real_ai_decisions(self):
        expected = {decision: self.add(decision=decision)["id"] for decision in ("keep", "needs_info", "watch", "exclude")}
        raw = [self.add(status="not_requested"), self.add(status="synthetic_demo"),
               self.add(status=_MISSING), self.add(synthetic=True)]
        for decision, candidate_id in expected.items():
            with self.subTest(decision=decision):
                self.assertEqual([candidate_id], self.ids(ai_decision=decision))
        self.assertEqual([candidate["id"] for candidate in raw], self.ids(ai_decision="unanalysed"))

    def test_ai_and_effective_review_filters_are_independent(self):
        candidate = self.add(decision="keep")
        self.human_review(candidate, "exclude")
        self.add(decision="keep")
        result = self.store.list_candidates(self.run["id"], decision="exclude", ai_decision="keep")
        self.assertEqual(1, result["total"])
        self.assertEqual(candidate["id"], result["items"][0]["id"])
        self.assertEqual("exclude", result["items"][0]["effective_decision"])
        self.assertEqual("keep", result["items"][0]["ai_decision"])
        self.assertEqual([], self.ids(decision="keep", ai_decision="exclude"))

    def test_keyword_and_ai_filter_precede_sorted_pagination(self):
        self.add(decision="keep", priority="A", text="Unrelated source.")
        c = self.add(decision="keep", priority="C", text="Needle quote C")
        a = self.add(decision="keep", priority="A", text="NEEDLE quote A")
        self.add(decision="watch", priority="A", text="needle discussion")
        b = self.add(decision="keep", priority="B", text="needle quote B")
        result = self.store.list_candidates(self.run["id"], q="needle", ai_decision="keep", order="ai_priority", limit=1, offset=1)
        self.assertEqual(3, result["total"])
        self.assertEqual([b["id"]], [item["id"] for item in result["items"]])
        self.assertEqual([a["id"], b["id"], c["id"]], self.ids(q="Needle", ai_decision="keep", order="ai_priority"))

    def test_sorted_pages_are_stable_nonoverlapping_and_keep_filtered_total(self):
        for decision, priority in [("exclude", "A"), ("keep", "C"), ("keep", "A"),
                                   ("watch", "B"), ("needs_info", "A"), ("keep", "B")]:
            self.add(decision=decision, priority=priority)
        full = self.ids(order="ai_priority")
        first = self.store.list_candidates(self.run["id"], order="ai_priority", limit=3)
        second = self.store.list_candidates(self.run["id"], order="ai_priority", limit=3, offset=3)
        self.assertEqual(6, first["total"])
        self.assertEqual(6, second["total"])
        self.assertEqual(full, [r["id"] for r in first["items"] + second["items"]])
        self.assertEqual(first, self.store.list_candidates(self.run["id"], order="ai_priority", limit=3))
        self.assertEqual([], self.store.list_candidates(self.run["id"], order="ai_priority", offset=100)["items"])

    def test_invalid_ai_filter_and_order_are_business_errors(self):
        for query, code in [({"ai_decision": "not_requested"}, "INVALID_AI_DECISION"),
                            ({"ai_decision": "KEEP"}, "INVALID_AI_DECISION"),
                            ({"order": "priority"}, "INVALID_ORDER"),
                            ({"order": "rowid DESC"}, "INVALID_ORDER")]:
            with self.subTest(query=query), self.assertRaises(AppError) as raised:
                self.store.list_candidates(self.run["id"], **query)
            self.assertEqual(code, raised.exception.code)

    def test_ai_query_does_not_write_review_or_candidate_state(self):
        first = self.add(decision="keep")
        self.add(decision="exclude")
        self.human_review(first)
        before_candidate = self.store.get_candidate(first["id"])
        before_reviews = self.store.list_reviews(self.run["id"])
        before_summary = self.store.get_run(self.run["id"])["batch_summary"]
        self.store.list_candidates(self.run["id"], ai_decision="keep", order="ai_priority")
        self.store.list_candidates(self.run["id"], ai_decision="unanalysed", order="collected")
        self.assertEqual(before_candidate, self.store.get_candidate(first["id"]))
        self.assertEqual(before_reviews, self.store.list_reviews(self.run["id"]))
        self.assertEqual(before_summary, self.store.get_run(self.run["id"])["batch_summary"])


class BatchSummaryHttpTests(_BatchFixtures, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = WorkbenchServer(("127.0.0.1", 0), self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.store = self.server.store
        self.serial = 0
        self.run = self.new_run()

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, **query):
        if query:
            path += "?" + urlencode(query)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_bootstrap_exposes_batch_execution_cap(self):
        status, response = self.request("/api/bootstrap")
        self.assertEqual(200, status)
        self.assertEqual(50, response["data"]["capabilities"]["max_batch_candidates"])

    def test_run_list_and_detail_publish_summary_without_private_buffers(self):
        self.add()
        self.store.update_run(self.run["id"], progress={
            "checkpoint": {"records": [{"content": "PRIVATE-HTTP-CHECKPOINT"}] * 4,
                           "next_index": 1, "collected": True},
        })
        status, detail = self.request("/api/runs/" + self.run["id"])
        self.assertEqual(200, status)
        status, listing = self.request("/api/runs")
        self.assertEqual(200, status)
        public = detail["data"]
        self.assertEqual(public["batch_summary"], listing["data"]["items"][0]["batch_summary"])
        self.assertEqual(4, public["batch_summary"]["collected"])
        self.assertEqual(1, public["batch_summary"]["processed"])
        self.assertNotIn("checkpoint", public["progress"])
        self.assertNotIn("records", public["config"])
        self.assertNotIn("PRIVATE-HTTP-CHECKPOINT", json.dumps([detail, listing]))

    def test_candidates_route_forwards_ai_filters_order_and_pagination(self):
        low = self.add(decision="keep", priority="C")
        high = self.add(decision="keep", priority="A")
        self.human_review(high)
        self.add(decision="exclude", priority="A")
        status, response = self.request("/api/candidates", run_id=self.run["id"], ai_decision="keep", order="ai_priority", limit=1)
        self.assertEqual(200, status)
        self.assertEqual(2, response["data"]["total"])
        item = response["data"]["items"][0]
        self.assertEqual(high["id"], item["id"])
        self.assertEqual("keep", item["ai_decision"])
        self.assertEqual("exclude", item["effective_decision"])
        status, response = self.request("/api/candidates", run_id=self.run["id"], ai_decision="keep", order="ai_priority", limit=1, offset=1)
        self.assertEqual(200, status)
        self.assertEqual(low["id"], response["data"]["items"][0]["id"])

    def test_invalid_ai_filter_and_order_return_specific_400_codes(self):
        for query, expected in [({"ai_decision": "not_requested"}, "INVALID_AI_DECISION"),
                                ({"ai_decision": "KEEP"}, "INVALID_AI_DECISION"),
                                ({"order": "unknown"}, "INVALID_ORDER")]:
            with self.subTest(query=query):
                status, response = self.request("/api/candidates", run_id=self.run["id"], **query)
                self.assertEqual(400, status)
                self.assertFalse(response["ok"])
                self.assertEqual(expected, response["error"]["code"])

    def test_non_numeric_pagination_still_returns_business_error(self):
        for query in ({"limit": "NaN"}, {"offset": "bad"}):
            with self.subTest(query=query):
                status, response = self.request("/api/candidates", run_id=self.run["id"], **query)
                self.assertEqual(400, status)
                self.assertEqual("INVALID_PAGINATION", response["error"]["code"])

    def test_route_defaults_remain_insertion_order(self):
        first = self.add(decision="exclude")
        second = self.add(decision="keep")
        status, response = self.request("/api/candidates", run_id=self.run["id"])
        self.assertEqual(200, status)
        self.assertEqual([first["id"], second["id"]], [item["id"] for item in response["data"]["items"]])


if __name__ == "__main__":
    unittest.main()
