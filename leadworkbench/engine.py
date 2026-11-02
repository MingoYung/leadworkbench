"""One background worker, serial inference, and evidence checkpoints per run."""
from __future__ import annotations

import copy
import queue
import threading
from datetime import datetime, timezone

from . import collectors, demo, models
from .processing import collected_analysis, normalize_collection


class _Paused(Exception):
    pass


class _Stopped(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class RunEngine:
    def __init__(self, store):
        self.store = store
        self._queue = queue.Queue()
        self._lock = getattr(store, "lifecycle_lock", None) or threading.RLock()
        self._stop = threading.Event()
        self._paused = set()
        self._scheduled = set()
        if hasattr(store, "bind_engine_lifecycle"):
            store.bind_engine_lifecycle(self._scheduled)
        self._worker = threading.Thread(target=self._work, name="leadworkbench-worker", daemon=True)
        self._worker.start()

    def start(self, run_id):
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("运行引擎已关闭")
            run = self.store.get_run(run_id)
            if not run:
                raise ValueError("找不到该任务")
            if run_id in self._scheduled:
                return
            if run["status"] in {"completed", "completed_partial"}:
                raise ValueError("已完成任务不能重新启动；请新建任务")
            self._paused.discard(run_id)
            self._scheduled.add(run_id)
            self.store.update_run(run_id, status="running", error=None, completed_at=None)
            self.store.log_event(run_id, "info", "任务已进入执行队列；采集、处理和审核进度会逐步保存。")
            self._queue.put(run_id)

    def pause(self, run_id):
        with self._lock:
            run = self.store.get_run(run_id)
            if not run:
                raise ValueError("找不到该任务")
            if run["status"] not in {"running", "pause_requested"}:
                return
            self._paused.add(run_id)
            self.store.update_run(run_id, status="pause_requested")
            self.store.log_event(run_id, "info", "暂停已请求；当前网络/模型请求结束并保存检查点后暂停")

    def resume(self, run_id):
        with self._lock:
            run = self.store.get_run(run_id)
            if not run:
                raise ValueError("找不到该任务")
            if run["status"] == "pause_requested" and run_id in self._scheduled:
                raise ValueError("当前请求尚未结束；任务暂停后再继续")
            self.start(run_id)

    def manage_runs(self, action, run_ids, operation_id):
        with self._lock:
            result = self.store.manage_runs(action, run_ids, operation_id)
            if action == "purge":
                self._paused.difference_update(result["run_ids"])
            return result

    def shutdown(self):
        self._stop.set()
        with self._lock:
            for run_id in list(self._scheduled):
                self.store.update_run(run_id, status="interrupted")
                self.store.log_event(run_id, "warning", "应用关闭；已保存的来源与检查点可在重启后继续，未完成请求不当作成功")
        self._queue.put(None)
        self._worker.join(timeout=2)

    def _check(self, run_id):
        if self._stop.is_set():
            raise _Stopped()
        if run_id in self._paused:
            raise _Paused()

    def _progress(self, run_id, checkpoint, message):
        self._check_stop()
        progress = {"current": checkpoint.get("next_index", 0), "total": len(checkpoint.get("records", [])), "message": message, "checkpoint": copy.deepcopy(checkpoint)}
        if isinstance(checkpoint.get("collection_summary"), dict):
            progress["collection_summary"] = copy.deepcopy(checkpoint["collection_summary"])
        self.store.update_run(run_id, progress=progress)

    def _comment_records(self, run_id, run, config, settings, checkpoint, limit):
        options = normalize_collection(config.get("collection"), run["source"])
        parents = checkpoint.setdefault("comment_posts", [])
        combined = options["scope"] == "posts_and_comments"
        parent_budget = min(options["max_posts"], max(1, limit // 3)) if combined else options["max_posts"]
        if run["source"] == "comments":
            if not checkpoint.get("comment_discovery_done"):
                parents.extend(config.get("urls", [])[:parent_budget])
                checkpoint["comment_discovery_done"] = True
                self._progress(run_id, checkpoint, f"已选择{len(parents)}个帖子，准备读取评论。")
        elif not checkpoint.get("comment_discovery_done"):
            queries = config.get("queries") or checkpoint.get("queries")
            if not queries:
                if settings.get("analysis_mode") == "collect_only":
                    raise ValueError("只采集模式请先填写搜索词或定向条件。")
                queries = models.plan_queries(settings, run.get("goal", ""), [], config.get("platforms", []))
                checkpoint["queries"] = queries
            seen_parents = {collectors.record_identity(p) for p in parents}
            stream = collectors.collect_opencli(queries, config.get("platforms", []), parent_budget, settings.get("opencli", {}))
            try:
                while len(parents) < parent_budget:
                    self._check(run_id)
                    try:
                        parent = next(stream)
                    except StopIteration:
                        break
                    self._check_stop()
                    identity = collectors.record_identity(parent)
                    if identity in seen_parents:
                        continue
                    seen_parents.add(identity)
                    parents.append(parent)
                    self._progress(run_id, checkpoint, f"已找到{len(parents)}个帖子，接下来按评论关键词筛选。")
                if len(parents) >= parent_budget:
                    try:
                        next(stream)
                    except StopIteration:
                        pass
            except collectors.CollectionError as exc:
                if not parents:
                    raise
                checkpoint["partial"] = True
                self.store.log_event(run_id, "warning", "部分帖子发现失败，将继续读取已保存帖子的评论。", {"code": exc.code, "message": str(exc)[:1500]})
            checkpoint["comment_discovery_done"] = True
            self._progress(run_id, checkpoint, f"帖子发现完成，已保存{len(parents)}个父帖来源。")
        if combined:
            for parent in parents[:parent_budget]:
                self._check(run_id)
                yield parent
        comment_budget = max(0, limit - len(parents[:parent_budget])) if combined else limit
        if not parents or not comment_budget:
            return {"posts_attempted": 0, "comments_read": 0, "comments_matched": 0, "comments_returned": 0,
                    "scope": "selected_post_comment_sample", "global_comment_index": False}
        self._check(run_id)
        return (yield from collectors.collect_comments(parents, config.get("platforms", []), comment_budget, settings.get("opencli", {}), options))

    def _check_stop(self):
        if self._stop.is_set():
            raise _Stopped()

    def _work(self):
        while not self._stop.is_set():
            run_id = self._queue.get()
            if run_id is None:
                break
            try:
                self._execute(run_id)
            except _Paused:
                self.store.update_run(run_id, status="paused")
                self.store.log_event(run_id, "info", "任务已暂停；来源快照与逐条进度已保留")
            except _Stopped:
                pass
            except Exception as e:
                if not self._stop.is_set():
                    self.store.update_run(run_id, status="failed", error={"code": getattr(e, "code", "run_failed"), "message": str(e)[:1500]})
                    self.store.log_event(run_id, "error", str(e)[:1500], {"code": getattr(e, "code", "run_failed")})
            finally:
                with self._lock:
                    self._scheduled.discard(run_id)
                self._queue.task_done()

    def _execute(self, run_id):
        self._check(run_id)
        run = self.store.get_run(run_id)
        config = run.get("config") or {}
        settings = self.store.get_settings(include_secret=True)
        settings = copy.deepcopy(settings)
        settings["targeting"] = config.get("targeting")
        explicit_mode = config.get("analysis_mode")
        mode = explicit_mode if explicit_mode in {"local", "collect_only"} else ("local" if settings.get("model", {}).get("enabled") else "collect_only")
        settings["analysis_mode"] = mode
        if explicit_mode == "local":
            # enabled is the default for new runs; the saved explicit choice
            # authorizes this run, independently of that default preference.
            settings.setdefault("model", {})["enabled"] = True
        collection = normalize_collection(config.get("collection"), run["source"])
        limit = max(1, min(int(config.get("limit", 20)), int(settings.get("limits", {}).get("max_candidates", 50)), 50))
        if collection["scope"] == "posts_and_comments" and limit < 2:
            raise ValueError("帖子和评论同时采集时，当前候选上限至少为2；请调整设置后重试。")
        checkpoint = copy.deepcopy((run.get("progress") or {}).get("checkpoint") or {"records": [], "next_index": 0, "collected": False, "partial": False, "failures": 0})
        records = checkpoint["records"]
        if not checkpoint.get("collected"):
            seen = {collectors.record_identity(r) for r in records}
            source = run["source"]
            if source == "demo":
                stream = iter(demo.synthetic_records())
            elif source == "import":
                items, warnings = collectors.normalize_records(config.get("records", []))
                for warning in warnings:
                    self.store.log_event(run_id, "warning", warning)
                checkpoint["partial"] |= bool(warnings)
                stream = iter(items)
            elif source == "urls":
                # Already-collected input URLs are skipped, avoiding duplicate fetches.
                requested = {r.get("metadata", {}).get("requested_url", r["source_url"]) for r in records}
                stream = collectors.collect_urls([u for u in config.get("urls", []) if u not in requested], limit)
            elif source == "comments" or (source == "opencli" and collection["scope"] != "posts"):
                stream = self._comment_records(run_id, run, config, settings, checkpoint, limit)
            elif source == "opencli":
                queries = config.get("queries") or checkpoint.get("queries")
                if not queries:
                    if mode == "collect_only":
                        raise ValueError("只采集模式请填写搜索词或定向条件，不会调用模型生成搜索词。")
                    self._check(run_id)
                    queries = models.plan_queries(settings, run.get("goal", ""), [], config.get("platforms", []))
                    checkpoint["queries"] = queries
                    self._progress(run_id, checkpoint, "本地模型已生成搜索词；尚未确认任何买家")
                stream = collectors.collect_opencli(queries, config.get("platforms", []), limit, settings.get("opencli", {}))
            else:
                raise ValueError("未知数据来源")
            stream_started = False
            try:
                while len(records) < limit:
                    self._check(run_id)
                    try:
                        stream_started = True
                        record = next(stream)
                    except StopIteration as stopped:
                        if isinstance(stopped.value, dict):
                            checkpoint["collection_summary"] = stopped.value
                        break
                    self._check_stop()
                    identity = collectors.record_identity(record)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    records.append(record)
                    checkpoint["partial"] |= bool(record.get("is_partial"))
                    self._progress(run_id, checkpoint, f"已保存{len(records)}条来源证据")
                if source in {"opencli", "comments"} and stream_started and len(records) >= limit:
                    # The bounded collector has no budget left for another
                    # request; advancing once receives its final error/report.
                    try:
                        next(stream)
                    except StopIteration as stopped:
                        if isinstance(stopped.value, dict):
                            checkpoint["collection_summary"] = stopped.value
            except collectors.CollectionError as e:
                if isinstance(getattr(e, "collection_summary", None), dict):
                    checkpoint["collection_summary"] = e.collection_summary
                if not records:
                    self._progress(run_id, checkpoint, "来源读取失败；本轮已读取范围和错误统计已保存。")
                    raise
                checkpoint["partial"] = True
                checkpoint["collection_error"] = {"code": e.code, "message": str(e)}
                if isinstance(getattr(e, "collection_summary", None), dict):
                    checkpoint["collection_summary"] = e.collection_summary
                self.store.log_event(run_id, "warning", "部分来源读取失败；保留已采集证据", checkpoint["collection_error"])
            checkpoint["collected"] = True
            self._progress(run_id, checkpoint, "来源采集完成，保存原文并等待外部审核。" if mode == "collect_only" else "来源采集完成，开始逐条分析。")
        self._check(run_id)
        for index in range(int(checkpoint.get("next_index", 0)), len(records)):
            self._check(run_id)
            record = records[index]
            self._progress(run_id, checkpoint, f"正在{'保存原文' if mode == 'collect_only' else '分析'}第{index + 1}/{len(records)}条")
            # Errors propagate without consuming this evidence's checkpoint: resume
            # retries the same item, with no synthetic fallback and no review reset.
            analysis = demo.synthetic_analysis(record) if run["source"] == "demo" else collected_analysis(record) if mode == "collect_only" else models.analyze_evidence(settings, record)
            if analysis.get("input_window", {}).get("truncated"):
                checkpoint["partial"] = True
            self._check_stop()
            self.store.add_candidate(run_id, record, analysis)
            checkpoint["next_index"] = index + 1
            self._progress(run_id, checkpoint, f"已{'保存' if mode == 'collect_only' else '分析'}{index + 1}/{len(records)}条")
            self._check(run_id)
        self._check(run_id)
        status = "completed_partial" if checkpoint.get("partial") else "completed"
        if not records:
            if checkpoint.get("collection_summary"):
                summary = checkpoint["collection_summary"]
                self.store.log_event(run_id, "info", f"本轮评论抽样读取{summary.get('comments_read', 0)}条，匹配{summary.get('comments_matched', 0)}条；零匹配只说明本次采样范围。")
            else:
                self.store.log_event(run_id, "warning", "没有找到带来源URL和正文的记录；未生成候选")
        self._progress(run_id, checkpoint, "已完成（部分来源/正文不完整）" if status == "completed_partial" else "已完成；候选仍需人工/外部复核")
        self.store.update_run(run_id, status=status, completed_at=_now(), error=None)
        self.store.freeze_run(run_id)
        self.store.log_event(run_id, "info", "任务完成并冻结证据快照", {"records": len(records), "status": status, "is_demo": run["source"] == "demo"})
