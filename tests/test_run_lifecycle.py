"""Atomic task lifecycle, privacy and in-flight worker exclusion contracts.

All stores and HTTP servers use temporary directories. Engine work is either
mocked behind Events or uses a mocked model on imported fixture text only.
"""

import copy
from datetime import datetime
import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from leadworkbench import models
from leadworkbench.engine import RunEngine
from leadworkbench.server import WorkbenchServer
from leadworkbench.store import AppError, Store


class _LifecycleFixtures:
    def new_run(self, mode="collect_only"):
        self.serial += 1
        record = {"source_url": f"https://fixture.invalid/run/{self.serial}",
                  "content": f"PRIVATE-SOURCE-{self.serial}: requesting a local service quote."}
        return self.store.create_run({"source": "import", "name": f"Fixture {self.serial}",
                                      "records": [record], "analysis_mode": mode})

    def candidate(self, run):
        record = copy.deepcopy(run["config"]["records"][0])
        return self.store.add_candidate(run["id"], record, {
            "analysis_status": "completed", "model": "fixture-model", "role": "consumer",
            "priority": "B", "decision": "needs_info", "reason": "Fixture analysis only.", "facts": {},
        })

    def review_payload(self, run, candidate, operation_id, decision="keep"):
        return {"run_id": run["id"], "reviewer": "human", "operation_id": operation_id,
                "changes": [{"candidate_id": candidate["id"], "base_version": candidate["version"],
                             "decision": decision, "reason": "PRIVATE-REVIEW: 核对原文后的人工意见。",
                             "evidence_ids": [candidate["evidence"][0]["id"]]}]}

    def has_receipt(self, operation_id):
        with self.store._db() as db:
            return db.execute("SELECT COUNT(*) FROM operations WHERE operation_id=?", (operation_id,)).fetchone()[0]

    def database_state(self):
        with self.store._db() as db:
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("runs", "candidates", "evidence", "reviews", "events", "snapshots", "operations")}

    def assert_error(self, code, status, fn):
        with self.assertRaises(AppError) as raised:
            fn()
        self.assertEqual(code, raised.exception.code)
        self.assertEqual(status, raised.exception.status)

    def assert_minimal_receipt(self, receipt, action, run_ids, operation_id):
        self.assertEqual({"action", "run_ids", "operation_id", "changed_count"}, set(receipt))
        self.assertEqual(action, receipt["action"])
        self.assertEqual(sorted(run_ids), receipt["run_ids"])
        self.assertEqual(operation_id, receipt["operation_id"])
        self.assertEqual(len(run_ids), receipt["changed_count"])
        serialized = json.dumps(receipt)
        self.assertNotIn("PRIVATE-SOURCE", serialized)
        self.assertNotIn("PRIVATE-REVIEW", serialized)


class RunLifecycleStoreTests(_LifecycleFixtures, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.serial = 0
        self.run = self.new_run()

    def test_trash_restore_preserve_run_history_review_and_frozen_evidence(self):
        candidate = self.candidate(self.run)
        self.store.submit_review(self.review_payload(self.run, candidate, "review-preserve-original"))
        self.store.update_run(self.run["id"], status="paused")
        before = self.store.get_run(self.run["id"])
        full_candidate = self.store.get_candidate(candidate["id"])
        reviews = self.store.list_reviews(self.run["id"])
        snapshot = self.store.freeze_run(self.run["id"])
        receipt = self.store.manage_runs("trash", [self.run["id"]], "trash-preserve-original")
        self.assert_minimal_receipt(receipt, "trash", [self.run["id"]], "trash-preserve-original")
        trashed = self.store.list_runs("trash")[0]
        self.assertIsInstance(datetime.fromisoformat(trashed["deleted_at"].replace("Z", "+00:00")), datetime)
        self.assertEqual("paused", trashed["status"])
        self.assertFalse(trashed.get("events"))
        self.store.manage_runs("restore", [self.run["id"]], "restore-preserve-original")
        self.assertEqual(before, self.store.get_run(self.run["id"]))
        self.assertEqual(full_candidate, self.store.get_candidate(candidate["id"]))
        self.assertEqual(reviews, self.store.list_reviews(self.run["id"]))
        self.assertEqual(snapshot, self.store.freeze_run(self.run["id"]))

    def test_default_active_trash_and_all_scopes_are_disjoint(self):
        other = self.new_run()
        self.store.manage_runs("trash", [self.run["id"]], "trash-scope-fixture")
        self.assertEqual([other["id"]], [r["id"] for r in self.store.list_runs()])
        self.assertEqual([other["id"]], [r["id"] for r in self.store.list_runs("active")])
        self.assertEqual([self.run["id"]], [r["id"] for r in self.store.list_runs("trash")])
        self.assertEqual({self.run["id"], other["id"]}, {r["id"] for r in self.store.list_runs("all")})

    def test_invalid_ids_are_rejected_without_receipt_or_partial_mutation(self):
        invalid = [None, "run-string", [], {}, [self.run["id"], self.run["id"]],
                   [""], ["   "], [1], [True], [None], [self.run["id"], {}],
                   [f"run-{n}" for n in range(101)]]
        for index, run_ids in enumerate(invalid):
            with self.subTest(run_ids=run_ids):
                before = self.database_state()
                operation_id = f"invalid-ids-operation-{index}"
                self.assert_error("INVALID_RUN_IDS", 400, lambda: self.store.manage_runs("trash", run_ids, operation_id))
                self.assertEqual(before, self.database_state())
                self.assertEqual(0, self.has_receipt(operation_id))

    def test_invalid_action_scope_and_operation_id_do_not_modify_runs(self):
        before = self.database_state()
        for action in ("delete", "TRASH", "", None, {}):
            with self.subTest(action=action):
                self.assert_error("INVALID_RUN_ACTION", 400, lambda: self.store.manage_runs(action, [self.run["id"]], "invalid-action-operation"))
        for scope in ("deleted", "ACTIVE", "", None, {}):
            with self.subTest(scope=scope):
                self.assert_error("INVALID_RUN_SCOPE", 400, lambda: self.store.list_runs(scope))
        self.assert_error("OPERATION_ID_REQUIRED", 400, lambda: self.store.manage_runs("trash", [self.run["id"]], None))
        self.assertEqual(before, self.database_state())

    def test_unknown_id_makes_whole_batch_fail_without_receipt(self):
        before = self.database_state()
        self.assert_error("NOT_FOUND", 404, lambda: self.store.manage_runs("trash", [self.run["id"], "run-does-not-exist"], "atomic-missing-operation"))
        self.assertEqual(before, self.database_state())

    def test_action_state_preconditions_are_atomic(self):
        other = self.new_run()
        for action in ("restore", "purge"):
            with self.subTest(action=action):
                before = self.database_state()
                self.assert_error("RUN_NOT_DELETED", 409, lambda: self.store.manage_runs(action, [self.run["id"]], f"invalid-active-{action}"))
                self.assertEqual(before, self.database_state())
        self.store.manage_runs("trash", [self.run["id"]], "trash-state-first")
        for action, ids, code, status in [
            ("trash", [other["id"], self.run["id"]], "RUN_DELETED", 410),
            ("restore", [self.run["id"], other["id"]], "RUN_NOT_DELETED", 409),
            ("purge", [self.run["id"], other["id"]], "RUN_NOT_DELETED", 409),
        ]:
            with self.subTest(action=action):
                before = self.database_state()
                self.assert_error(code, status, lambda: self.store.manage_runs(action, ids, "mixed-state-" + action))
                self.assertEqual(before, self.database_state())

    def test_same_operation_and_reordered_ids_returns_canonical_receipt(self):
        other = self.new_run()
        ids = [self.run["id"], other["id"]]
        receipt = self.store.manage_runs("trash", ids, "idempotent-trash-operation")
        self.assert_minimal_receipt(receipt, "trash", ids, "idempotent-trash-operation")
        before = self.database_state()
        self.assertEqual(receipt, self.store.manage_runs("trash", list(reversed(ids)), "idempotent-trash-operation"))
        self.assertEqual(before, self.database_state())
        self.assertEqual(1, self.has_receipt("idempotent-trash-operation"))

    def test_operation_reuse_with_different_action_or_ids_conflicts(self):
        other = self.new_run()
        self.store.manage_runs("trash", [self.run["id"]], "operation-reuse-fixture")
        before = self.database_state()
        for action, ids in [("restore", [self.run["id"]]), ("trash", [other["id"]])]:
            with self.subTest(action=action):
                self.assert_error("IDEMPOTENCY_CONFLICT", 409, lambda: self.store.manage_runs(action, ids, "operation-reuse-fixture"))
        self.assertEqual(before, self.database_state())

    def test_scheduled_guard_has_priority_and_never_writes_receipt(self):
        for status in ("completed", "paused", "running", "pause_requested"):
            with self.subTest(status=status):
                self.store.update_run(self.run["id"], status=status)
                self.store._scheduled_runs.add(self.run["id"])
                before = self.database_state()
                operation = "busy-status-" + status
                try:
                    self.assert_error("RUN_BUSY", 409, lambda: self.store.manage_runs("trash", [self.run["id"]], operation))
                    self.assertEqual(before, self.database_state())
                    self.assertEqual(0, self.has_receipt(operation))
                finally:
                    self.store._scheduled_runs.discard(self.run["id"])

    def test_all_mutations_reject_scheduled_tasks_before_state_checks(self):
        self.store.manage_runs("trash", [self.run["id"]], "busy-deleted-preparation")
        self.store._scheduled_runs.add(self.run["id"])
        try:
            for action in ("trash", "restore", "purge"):
                with self.subTest(action=action):
                    before = self.database_state()
                    operation = "busy-all-actions-" + action
                    self.assert_error("RUN_BUSY", 409, lambda: self.store.manage_runs(action, [self.run["id"]], operation))
                    self.assertEqual(before, self.database_state())
        finally:
            self.store._scheduled_runs.discard(self.run["id"])

    def test_running_member_prevents_deletion_of_entire_batch(self):
        other = self.new_run()
        for status in ("running", "pause_requested"):
            with self.subTest(status=status):
                self.store.update_run(other["id"], status=status)
                before = self.database_state()
                self.assert_error("RUN_RUNNING", 409, lambda: self.store.manage_runs("trash", [self.run["id"], other["id"]], "running-batch-" + status))
                self.assertEqual(before, self.database_state())

    def test_deleted_run_blocks_reads_writes_exports_and_old_review_receipts(self):
        candidate = self.candidate(self.run)
        old_payload = self.review_payload(self.run, candidate, "review-before-delete")
        review = self.store.submit_review(old_payload)
        self.store.revert_review(review["id"], "revert-before-delete")
        current = self.store.get_candidate(candidate["id"])
        fresh = self.review_payload(self.run, current, "review-after-delete")
        self.store.manage_runs("trash", [self.run["id"]], "trash-all-access")
        calls = {
            "get_run": lambda: self.store.get_run(self.run["id"]),
            "get_candidate": lambda: self.store.get_candidate(candidate["id"]),
            "list_candidates": lambda: self.store.list_candidates(self.run["id"]),
            "list_reviews": lambda: self.store.list_reviews(self.run["id"]),
            "submit_review": lambda: self.store.submit_review(fresh),
            "review_receipt_retry": lambda: self.store.submit_review(old_payload),
            "revert_review": lambda: self.store.revert_review(review["id"], "new-revert-after-delete"),
            "revert_receipt_retry": lambda: self.store.revert_review(review["id"], "revert-before-delete"),
            "export_run": lambda: self.store.export_run(self.run["id"]),
            "export_csv": lambda: self.store.export_csv(self.run["id"]),
            "update_run": lambda: self.store.update_run(self.run["id"], status="failed"),
            "add_candidate": lambda: self.store.add_candidate(self.run["id"], {"content": "new source"}, {}),
            "freeze_run": lambda: self.store.freeze_run(self.run["id"]),
            "log_event": lambda: self.store.log_event(self.run["id"], "info", "not allowed"),
        }
        before = self.database_state()
        for name, fn in calls.items():
            with self.subTest(operation=name):
                self.assert_error("RUN_DELETED", 410, fn)
        self.assertEqual(before, self.database_state())

    def test_restore_continues_original_review_and_revert_workflow(self):
        candidate = self.candidate(self.run)
        review = self.store.submit_review(self.review_payload(self.run, candidate, "review-restore-flow"))
        version = self.store.get_candidate(candidate["id"])["version"]
        self.store.manage_runs("trash", [self.run["id"]], "trash-restore-flow")
        self.store.manage_runs("restore", [self.run["id"]], "restore-review-flow")
        self.assertEqual(version, self.store.get_candidate(candidate["id"])["version"])
        self.store.revert_review(review["id"], "revert-after-restore")
        restored = self.store.get_candidate(candidate["id"])
        self.assertEqual("unreviewed", restored["effective_decision"])
        self.store.submit_review(self.review_payload(self.run, restored, "review-new-after-restore", "watch"))
        self.assertEqual("watch", self.store.get_candidate(candidate["id"])["effective_decision"])

    def test_purge_cascades_data_and_owned_receipts_but_keeps_other_task(self):
        candidate = self.candidate(self.run)
        review = self.store.submit_review(self.review_payload(self.run, candidate, "purged-review-original"))
        self.store.revert_review(review["id"], "purged-revert-original")
        self.store.freeze_run(self.run["id"])
        other = self.new_run()
        other_candidate = self.candidate(other)
        self.store.submit_review(self.review_payload(other, other_candidate, "retained-review-original"))
        other_snapshot = self.store.freeze_run(other["id"])
        other_before = self.store.get_candidate(other_candidate["id"])
        self.store.manage_runs("trash", [self.run["id"]], "purged-trash-original")
        receipt = self.store.manage_runs("purge", [self.run["id"]], "purge-final-minimal")
        self.assert_minimal_receipt(receipt, "purge", [self.run["id"]], "purge-final-minimal")
        with self.store._db() as db:
            for table, key, value in [("runs", "id", self.run["id"]), ("candidates", "run_id", self.run["id"]),
                                      ("reviews", "run_id", self.run["id"]), ("events", "run_id", self.run["id"]),
                                      ("snapshots", "run_id", self.run["id"]), ("evidence", "candidate_id", candidate["id"])]:
                self.assertEqual(0, db.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}=?", (value,)).fetchone()[0], table)
        for operation in ("purged-review-original", "purged-revert-original", "purged-trash-original"):
            self.assertEqual(0, self.has_receipt(operation), operation)
        self.assertEqual(1, self.has_receipt("purge-final-minimal"))
        self.assertEqual(1, self.has_receipt("retained-review-original"))
        self.assertEqual(other_before, self.store.get_candidate(other_candidate["id"]))
        self.assertEqual(other_snapshot, self.store.freeze_run(other["id"]))
        self.assert_error("NOT_FOUND", 404, lambda: self.store.get_run(self.run["id"]))
        self.assert_error("NOT_FOUND", 404, lambda: self.store.get_candidate(candidate["id"]))

    def test_purge_receipt_is_retryable_after_run_is_gone(self):
        self.store.manage_runs("trash", [self.run["id"]], "trash-purge-retry")
        receipt = self.store.manage_runs("purge", [self.run["id"]], "permanent-purge-retry")
        before = self.database_state()
        self.assertEqual(receipt, self.store.manage_runs("purge", [self.run["id"]], "permanent-purge-retry"))
        self.assertEqual(before, self.database_state())
        self.assert_minimal_receipt(receipt, "purge", [self.run["id"]], "permanent-purge-retry")

    def test_cross_task_receipt_survives_partial_purge_without_retrashing_other(self):
        other = self.new_run()
        ids = [self.run["id"], other["id"]]
        original = self.store.manage_runs("trash", ids, "cross-task-trash-receipt")
        self.store.manage_runs("restore", [other["id"]], "restore-protected-other")
        self.store.manage_runs("purge", [self.run["id"]], "purge-only-first-task")
        before = self.database_state()
        replay = self.store.manage_runs("trash", list(reversed(ids)), "cross-task-trash-receipt")
        self.assertEqual(original, replay)
        self.assertEqual(before, self.database_state())
        self.assertNotIn("deleted_at", self.store.get_run(other["id"]))
        self.assertEqual(1, self.has_receipt("cross-task-trash-receipt"))

    def test_shared_receipt_is_removed_only_after_last_referenced_task_is_purged(self):
        other = self.new_run()
        ids = [self.run["id"], other["id"]]
        self.store.manage_runs("trash", ids, "sequential-shared-trash")
        first_purge = self.store.manage_runs("purge", [self.run["id"]], "sequential-first-purge")
        self.assertEqual(1, self.has_receipt("sequential-shared-trash"))
        self.assertEqual([other["id"]], [run["id"] for run in self.store.list_runs("trash")])
        second_purge = self.store.manage_runs("purge", [other["id"]], "sequential-second-purge")
        self.assertEqual(0, self.has_receipt("sequential-shared-trash"))
        self.assertEqual([], self.store.list_runs("all"))
        self.assertEqual(1, self.has_receipt("sequential-first-purge"))
        self.assertEqual(1, self.has_receipt("sequential-second-purge"))
        before = self.database_state()
        self.assertEqual(first_purge, self.store.manage_runs("purge", [self.run["id"]], "sequential-first-purge"))
        self.assertEqual(second_purge, self.store.manage_runs("purge", [other["id"]], "sequential-second-purge"))
        self.assertEqual(before, self.database_state())

    def test_review_commit_and_log_remain_atomic_against_trash(self):
        for operation in ("review", "revert"):
            with self.subTest(operation=operation):
                run = self.new_run()
                candidate = self.candidate(run)
                payload = self.review_payload(run, candidate, "race-review-" + operation)
                if operation == "revert":
                    review = self.store.submit_review(payload)
                    review_call = lambda: self.store.revert_review(review["id"], "race-revert-operation")
                else:
                    review_call = lambda: self.store.submit_review(payload)
                entered, release = threading.Event(), threading.Event()
                trash_attempted, trash_finished = threading.Event(), threading.Event()
                outcomes = {}
                original_log = self.store.log_event

                def held_log(run_id, level, message, details=None):
                    if run_id == run["id"] and ("提交审核" in message or "撤销审核批次" in message):
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError("fixture log release timeout")
                    return original_log(run_id, level, message, details)

                def review_worker():
                    try:
                        outcomes["review"] = ("ok", review_call())
                    except BaseException as exc:
                        outcomes["review"] = ("error", exc)

                def trash_worker():
                    trash_attempted.set()
                    try:
                        outcomes["trash"] = ("ok", self.store.manage_runs("trash", [run["id"]], "race-log-trash-" + operation))
                    except BaseException as exc:
                        outcomes["trash"] = ("error", exc)
                    finally:
                        trash_finished.set()

                threads = [threading.Thread(target=review_worker), threading.Thread(target=trash_worker)]
                try:
                    with mock.patch.object(self.store, "log_event", side_effect=held_log):
                        threads[0].start()
                        self.assertTrue(entered.wait(2), "review never reached its post-commit log")
                        with self.store._db() as db:
                            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM reviews WHERE run_id=?", (run["id"],)).fetchone()[0])
                        threads[1].start()
                        self.assertTrue(trash_attempted.wait(2))
                        self.assertFalse(trash_finished.wait(.08), "trash overtook the review's post-commit log")
                        release.set()
                        for thread in threads:
                            thread.join(2)
                            self.assertFalse(thread.is_alive(), "review/trash deadlock")
                    self.assertEqual("ok", outcomes["review"][0], repr(outcomes))
                    self.assertEqual("ok", outcomes["trash"][0], repr(outcomes))
                    self.assert_error("RUN_DELETED", 410, lambda: self.store.get_run(run["id"]))
                finally:
                    release.set()
                    for thread in threads:
                        if thread.ident is not None:
                            thread.join(2)

    def test_create_log_and_final_response_remain_atomic_against_trash(self):
        entered, release = threading.Event(), threading.Event()
        trash_attempted, trash_finished = threading.Event(), threading.Event()
        outcomes = {}
        created_ids = []
        original_log = self.store.log_event

        def held_log(run_id, level, message, details=None):
            if message == "创建任务":
                created_ids.append(run_id)
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("fixture creation log release timeout")
            return original_log(run_id, level, message, details)

        def create_worker():
            try:
                outcomes["create"] = ("ok", self.store.create_run({
                    "source": "import", "analysis_mode": "collect_only", "name": "Concurrent creation fixture",
                    "records": [{"source_url": "https://fixture.invalid/create-race", "content": "Creation race fixture."}],
                }))
            except BaseException as exc:
                outcomes["create"] = ("error", exc)

        def trash_worker():
            trash_attempted.set()
            try:
                outcomes["trash"] = ("ok", self.store.manage_runs("trash", created_ids, "race-create-log-trash"))
            except BaseException as exc:
                outcomes["trash"] = ("error", exc)
            finally:
                trash_finished.set()

        threads = [threading.Thread(target=create_worker), threading.Thread(target=trash_worker)]
        try:
            with mock.patch.object(self.store, "log_event", side_effect=held_log):
                threads[0].start()
                self.assertTrue(entered.wait(2), "creation never reached its post-commit log")
                with self.store._db() as db:
                    self.assertEqual(1, db.execute("SELECT COUNT(*) FROM runs WHERE id=?", (created_ids[0],)).fetchone()[0])
                threads[1].start()
                self.assertTrue(trash_attempted.wait(2))
                self.assertFalse(trash_finished.wait(.08), "trash overtook creation's log and final get_run")
                self.assertEqual(0, self.has_receipt("race-create-log-trash"))
                release.set()
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive(), "create/trash deadlock")
            self.assertEqual("ok", outcomes["create"][0], repr(outcomes))
            self.assertEqual("ok", outcomes["trash"][0], repr(outcomes))
            created = outcomes["create"][1]
            self.assertEqual(created_ids[0], created["id"])
            self.assertEqual("created", created["status"])
            self.assertNotIn("deleted_at", created)
            self.assert_minimal_receipt(outcomes["trash"][1], "trash", created_ids, "race-create-log-trash")
            self.assert_error("RUN_DELETED", 410, lambda: self.store.get_run(created_ids[0]))
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(2)


class RunLifecycleEngineTests(_LifecycleFixtures, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.serial = 0
        self.engine = RunEngine(self.store)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.engine.shutdown()
        self.temp.cleanup()

    def wait_unscheduled(self, run_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.engine._lock:
                if run_id not in self.engine._scheduled:
                    return
            time.sleep(.005)
        self.fail("worker remained scheduled")

    def test_engine_and_store_share_lifecycle_lock_and_scheduled_set(self):
        self.assertIs(self.store.lifecycle_lock, self.engine._lock)
        self.assertIs(self.store._scheduled_runs, self.engine._scheduled)

    def test_completed_and_paused_but_inflight_still_reject_trash(self):
        for status in ("completed", "paused"):
            with self.subTest(status=status):
                run = self.new_run()
                entered, release = threading.Event(), threading.Event()

                def hold(run_id):
                    self.store.update_run(run_id, status=status)
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("fixture release timeout")

                try:
                    with mock.patch.object(self.engine, "_execute", side_effect=hold):
                        self.engine.start(run["id"])
                        self.assertTrue(entered.wait(2))
                        before = self.database_state()
                        operation = "inflight-terminal-" + status
                        self.assert_error("RUN_BUSY", 409, lambda: self.engine.manage_runs("trash", [run["id"]], operation))
                        self.assertEqual(before, self.database_state())
                        self.assertEqual(0, self.has_receipt(operation))
                        release.set()
                        self.wait_unscheduled(run["id"])
                finally:
                    release.set()

    def test_real_import_model_request_inflight_is_busy_without_lifecycle_write(self):
        run = self.new_run(mode="local")
        entered, release = threading.Event(), threading.Event()

        def analyze(_settings, _record):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture release timeout")
            return {"analysis_status": "completed", "decision": "needs_info", "role": "unknown", "facts": {}, "model": "fixture"}

        try:
            with mock.patch.object(models, "analyze_evidence", side_effect=analyze):
                self.engine.start(run["id"])
                self.assertTrue(entered.wait(2))
                before = self.database_state()
                self.assert_error("RUN_BUSY", 409, lambda: self.store.manage_runs("trash", [run["id"]], "busy-real-model-fixture"))
                self.assertEqual(before, self.database_state())
                self.assertEqual(0, self.has_receipt("busy-real-model-fixture"))
                release.set()
                self.wait_unscheduled(run["id"])
            self.assertEqual("completed", self.store.get_run(run["id"])["status"])
        finally:
            release.set()

    def test_concurrent_start_and_trash_cannot_both_succeed(self):
        run = self.new_run()
        barrier = threading.Barrier(3)
        release = threading.Event()
        entered = threading.Event()
        results = {}

        def hold(run_id):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture release timeout")
            self.store.update_run(run_id, status="completed")

        def attempt(name, fn):
            try:
                barrier.wait(timeout=2)
                results[name] = ("ok", fn())
            except BaseException as exc:
                results[name] = ("error", exc)

        threads = [threading.Thread(target=attempt, args=("start", lambda: self.engine.start(run["id"]))),
                   threading.Thread(target=attempt, args=("trash", lambda: self.engine.manage_runs("trash", [run["id"]], "race-start-trash")))]
        try:
            with mock.patch.object(self.engine, "_execute", side_effect=hold):
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=2)
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive(), "lifecycle lock deadlock")
                self.assertEqual({"start", "trash"}, set(results))
                self.assertFalse(results["start"][0] == "ok" and results["trash"][0] == "ok")
                if results["start"][0] == "ok":
                    self.assertTrue(entered.wait(2))
                    self.assertIsInstance(results["trash"][1], AppError)
                    self.assertEqual("RUN_BUSY", results["trash"][1].code)
                    self.assertEqual(0, self.has_receipt("race-start-trash"))
                    self.assertNotIn("deleted_at", self.store.get_run(run["id"]))
                else:
                    self.assertEqual("ok", results["trash"][0])
                    self.assertIsInstance(results["start"][1], AppError)
                    self.assertEqual("RUN_DELETED", results["start"][1].code)
                    self.assertNotIn(run["id"], self.engine._scheduled)
                release.set()
                self.wait_unscheduled(run["id"])
        finally:
            release.set()
            for thread in threads:
                thread.join(2)

    def test_deleted_task_cannot_start_resume_or_pause(self):
        run = self.new_run()
        self.engine.manage_runs("trash", [run["id"]], "trash-engine-denial")
        before = self.database_state()
        for action in ("start", "resume", "pause"):
            with self.subTest(action=action):
                self.assert_error("RUN_DELETED", 410, lambda: getattr(self.engine, action)(run["id"]))
        self.assertEqual(before, self.database_state())
        self.assertNotIn(run["id"], self.engine._scheduled)


class RunLifecycleHttpTests(_LifecycleFixtures, unittest.TestCase):
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

    def request(self, method="GET", path="/api/runs", body=None, headers=None, **query):
        if query:
            path += "?" + urlencode(query)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def auth(self, **extra):
        return {"Content-Type": "application/json", "X-App-Token": self.server.token, **extra}

    def test_scopes_publish_only_metadata_and_default_hides_trash(self):
        other = self.new_run()
        self.store.update_run(self.run["id"], progress={"checkpoint": {"records": [{"content": "PRIVATE-CHECKPOINT"}]}})
        self.store.log_event(self.run["id"], "info", "PRIVATE-TRASH-EVENT")
        self.store.manage_runs("trash", [self.run["id"]], "http-trash-scopes")
        for scope, expected in [(None, {other["id"]}), ("active", {other["id"]}),
                                ("trash", {self.run["id"]}), ("all", {self.run["id"], other["id"]})]:
            with self.subTest(scope=scope):
                status, result = self.request(**({} if scope is None else {"scope": scope}))
                self.assertEqual(200, status)
                self.assertEqual(expected, {r["id"] for r in result["data"]["items"]})
                self.assertNotIn("PRIVATE-SOURCE", json.dumps(result))
                self.assertNotIn("PRIVATE-CHECKPOINT", json.dumps(result))
                self.assertNotIn("PRIVATE-TRASH-EVENT", json.dumps(result))
                for run in result["data"]["items"]:
                    self.assertNotIn("records", run["config"])
                    self.assertNotIn("checkpoint", run["progress"])
                    if "deleted_at" in run:
                        self.assertFalse(run.get("events"))

    def test_mutations_require_token_and_same_origin_before_state_validation(self):
        for action in ("trash", "restore", "purge"):
            for headers in ({"Content-Type": "application/json"}, self.auth(Origin="https://evil.invalid"),
                            {"Content-Type": "application/json", "X-App-Token": "wrong-token"}):
                with self.subTest(action=action, headers=tuple(headers)):
                    before = self.database_state()
                    status, response = self.request("POST", "/api/runs/" + action,
                                                    {"run_ids": [self.run["id"]], "operation_id": "http-permission-" + action}, headers)
                    self.assertEqual(403, status)
                    self.assertFalse(response["ok"])
                    self.assertEqual(before, self.database_state())

    def test_http_roundtrip_canonical_idempotency_and_minimal_purge_receipt(self):
        other = self.new_run()
        ids = [self.run["id"], other["id"]]
        body = {"run_ids": ids, "operation_id": "http-canonical-trash"}
        status, first = self.request("POST", "/api/runs/trash", body, self.auth())
        self.assertEqual(200, status)
        self.assert_minimal_receipt(first["data"], "trash", ids, body["operation_id"])
        status, retried = self.request("POST", "/api/runs/trash", {**body, "run_ids": list(reversed(ids))}, self.auth())
        self.assertEqual(200, status)
        self.assertEqual(first, retried)
        status, restored = self.request("POST", "/api/runs/restore", {"run_ids": ids, "operation_id": "http-canonical-restore"}, self.auth())
        self.assertEqual(200, status)
        self.assert_minimal_receipt(restored["data"], "restore", ids, "http-canonical-restore")
        self.store.manage_runs("trash", ids, "http-trash-before-purge")
        body = {"run_ids": ids, "operation_id": "http-minimal-purge"}
        status, purged = self.request("POST", "/api/runs/purge", body, self.auth())
        self.assertEqual(200, status)
        self.assert_minimal_receipt(purged["data"], "purge", ids, body["operation_id"])
        self.assertEqual((200, purged), self.request("POST", "/api/runs/purge", {**body, "run_ids": list(reversed(ids))}, self.auth()))

    def test_invalid_scope_and_ids_return_specific_business_errors(self):
        status, response = self.request(scope="deleted")
        self.assertEqual(400, status)
        self.assertEqual("INVALID_RUN_SCOPE", response["error"]["code"])
        for ids in ([], "not-an-array", [self.run["id"], self.run["id"]], [1]):
            with self.subTest(ids=ids):
                status, response = self.request("POST", "/api/runs/trash", {"run_ids": ids, "operation_id": "http-invalid-ids"}, self.auth())
                self.assertEqual(400, status)
                self.assertEqual("INVALID_RUN_IDS", response["error"]["code"])

    def test_deleted_reads_exports_review_retry_and_engine_controls_return_410(self):
        candidate = self.candidate(self.run)
        payload = self.review_payload(self.run, candidate, "http-review-before-delete")
        review = self.store.submit_review(payload)
        self.store.revert_review(review["id"], "http-revert-before-delete")
        self.store.manage_runs("trash", [self.run["id"]], "http-trash-access-test")
        read_paths = ["/api/runs/" + self.run["id"], "/api/candidates/" + candidate["id"],
                      "/api/candidates?" + urlencode({"run_id": self.run["id"]}),
                      "/api/reviews?" + urlencode({"run_id": self.run["id"]}),
                      f"/api/runs/{self.run['id']}/export?format=json",
                      f"/api/runs/{self.run['id']}/export?format=csv"]
        for path in read_paths:
            with self.subTest(path=path):
                status, response = self.request(path=path)
                self.assertEqual(410, status)
                self.assertEqual("RUN_DELETED", response["error"]["code"])
                self.assertNotIn("PRIVATE-SOURCE", json.dumps(response))
        mutations = [("/api/reviews", payload),
                     (f"/api/reviews/{review['id']}/revert", {"operation_id": "http-revert-before-delete"})]
        mutations.extend((f"/api/runs/{self.run['id']}/{action}", {}) for action in ("start", "pause", "resume"))
        for path, body in mutations:
            with self.subTest(path=path):
                status, response = self.request("POST", path, body, self.auth())
                self.assertEqual(410, status)
                self.assertEqual("RUN_DELETED", response["error"]["code"])

    def test_scheduled_completed_task_is_busy_over_http_and_creates_no_receipt(self):
        self.store.update_run(self.run["id"], status="completed")
        with self.server.engine._lock:
            self.server.engine._scheduled.add(self.run["id"])
        before = self.database_state()
        try:
            status, response = self.request("POST", "/api/runs/trash",
                                            {"run_ids": [self.run["id"]], "operation_id": "http-scheduled-completed"}, self.auth())
            self.assertEqual(409, status)
            self.assertEqual("RUN_BUSY", response["error"]["code"])
            self.assertEqual(before, self.database_state())
            self.assertEqual(0, self.has_receipt("http-scheduled-completed"))
        finally:
            with self.server.engine._lock:
                self.server.engine._scheduled.discard(self.run["id"])


if __name__ == "__main__":
    unittest.main()
