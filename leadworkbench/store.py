from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DECISIONS = {"keep", "exclude", "needs_info", "watch"}
REVIEWERS = {"human", "codex", "dsh"}
DEFAULT_SETTINGS = {
    "model": {"protocol": "openai", "base_url": "http://127.0.0.1:8080/v1",
              "model": "local-model", "api_key": "",
              "timeout_seconds": 180, "max_tokens": 2048, "temperature": 0.1,
              "concurrency": 1, "enabled": False,
              "extra_body": {}},
    "opencli": {"executable": "opencli", "profile": "", "timeout_seconds": 90},
    "limits": {"max_candidates": 50, "max_content_chars": 18000},
}


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:20]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_url(value, allow_empty=True):
    if not value and allow_empty:
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise AppError("INVALID_URL", "来源链接格式不正确。")
    try:
        p = urlsplit(value.strip())
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise ValueError()
        p.port
    except ValueError:
        raise AppError("INVALID_URL", "只接受没有内嵌凭据的 http 或 https 链接。")
    return value.strip()


def source_key(record):
    if record.get("kind") == "comment":
        from .collectors import record_identity
        return record_identity(record)
    url = safe_url(record.get("source_url"))
    if not url:
        raw = dumps({k: record.get(k) for k in ("platform", "author_handle", "content")})
    else:
        p = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        raw = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(query), ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def json_copy(value):
    return json.loads(dumps(value))


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "workbench.sqlite3"
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self._scheduled_runs = set()
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_key TEXT NOT NULL,
              version INTEGER NOT NULL, data TEXT NOT NULL,
              UNIQUE(run_id, source_key), FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE INDEX IF NOT EXISTS candidates_run ON candidates(run_id);
            CREATE TABLE IF NOT EXISTS evidence(
              id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, data TEXT NOT NULL,
              FOREIGN KEY(candidate_id) REFERENCES candidates(id));
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              created_at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, details TEXT);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
              operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots(run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("app", dumps(DEFAULT_SETTINGS)))
            # Interrupted work remains resumable; no automatic re-execution at startup.
            for row in db.execute("SELECT id,data FROM runs").fetchall():
                data = json.loads(row["data"])
                if not data.get("deleted_at") and data.get("status") in {"running", "pause_requested"}:
                    data.update(status="interrupted", updated_at=now())
                    data.setdefault("progress", {})["message"] = "上次运行中断，可从已保存的检查点继续。"
                    db.execute("UPDATE runs SET data=? WHERE id=?", (dumps(data), row["id"]))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def _run(self, db, run_id, allow_deleted=False):
        row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise AppError("NOT_FOUND", "该运行批次不存在。", 404)
        run = json.loads(row[0])
        if run.get("deleted_at") and not allow_deleted:
            raise AppError("RUN_DELETED", "该任务已移入回收站，请先恢复后再操作。", 410)
        return run

    def bind_engine_lifecycle(self, scheduled_runs):
        """The engine and all lifecycle writes share this lock and live busy set."""
        with self.lifecycle_lock:
            self._scheduled_runs = scheduled_runs

    def get_settings(self, include_secret=False):
        with self.lock, self._db() as db:
            settings = json.loads(db.execute("SELECT value FROM settings WHERE key='app'").fetchone()[0])
        if not include_secret:
            settings["model"]["api_key_configured"] = bool(settings["model"].pop("api_key", ""))
        return settings

    def update_settings(self, patch):
        if not isinstance(patch, dict):
            raise AppError("INVALID_SETTINGS", "设置必须是 JSON 对象。")
        with self.lock, self._db() as db:
            settings = self.get_settings(include_secret=True)
            allowed = {
                "model": {"protocol", "base_url", "model", "api_key", "timeout_seconds", "max_tokens", "temperature", "concurrency", "enabled", "allow_remote", "extra_body"},
                "opencli": {"executable", "node_entry", "profile", "timeout_seconds"},
                "limits": {"max_candidates", "max_content_chars"},
            }
            for section, values in patch.items():
                if section not in allowed or not isinstance(values, dict):
                    raise AppError("INVALID_SETTINGS", "存在无法识别的设置分组。")
                for key, value in values.items():
                    if key == "api_key_configured":
                        continue
                    if key not in allowed[section]:
                        raise AppError("INVALID_SETTINGS", f"不支持的设置：{section}.{key}")
                    settings[section][key] = value
            model = settings["model"]
            if model["protocol"] not in {"openai", "ollama"}:
                raise AppError("INVALID_SETTINGS", "模型协议仅支持 OpenAI 兼容或 Ollama。")
            endpoint = safe_url(model["base_url"], allow_empty=False)
            host = urlsplit(endpoint).hostname
            if host not in {"127.0.0.1", "localhost", "::1"} and not model.get("allow_remote"):
                raise AppError("LOCAL_MODEL_REQUIRED", "默认只连接本地模型；远程地址需明确启用。")
            for key, low, high in [("timeout_seconds", 5, 600), ("max_tokens", 128, 16384)]:
                if isinstance(model[key], bool) or not isinstance(model[key], (int, float)) or not low <= model[key] <= high:
                    raise AppError("INVALID_SETTINGS", f"{key} 应在 {low}–{high} 之间。")
            model["concurrency"] = 1  # observed local hardware: serial inference for first release
            if not isinstance(model.get("model"), str) or len(model["model"]) > 200:
                raise AppError("INVALID_SETTINGS", "模型名称格式不正确。")
            if not isinstance(model.get("api_key", ""), str):
                raise AppError("INVALID_SETTINGS", "API 密钥必须是文字。")
            if isinstance(model.get("temperature"), bool) or not isinstance(model.get("temperature"), (float, int)) or not 0 <= model["temperature"] <= 1:
                raise AppError("INVALID_SETTINGS", "温度值应在 0–1 之间。")
            for key in ("enabled", "allow_remote"):
                if key in model and not isinstance(model[key], bool):
                    raise AppError("INVALID_SETTINGS", f"{key} 必须为布尔值。")
            extra = model.get("extra_body", {})
            protected = {"model", "messages", "stream", "temperature", "max_tokens", "response_format"}
            if not isinstance(extra, dict) or protected.intersection(extra) or len(dumps(extra)) > 10000:
                raise AppError("INVALID_SETTINGS", "extra_body 必须为对象，且不能覆盖核心推理参数。")
            cli = settings["opencli"]
            for key in ("executable", "profile", "node_entry"):
                value = cli.get(key, "")
                if not isinstance(value, str) or len(value) > 4096 or (key == "executable" and not value.strip()):
                    raise AppError("INVALID_SETTINGS", f"opencli.{key} 格式不正确。")
            timeout = cli.get("timeout_seconds", 90)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= timeout <= 600:
                raise AppError("INVALID_SETTINGS", "OpenCLI 超时应在 5–600 秒之间。")
            for key, low, high in [("max_candidates", 1, 1000), ("max_content_chars", 1000, 100000)]:
                value = settings["limits"][key]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise AppError("INVALID_SETTINGS", f"{key} 应在 {low}–{high} 之间。")
            db.execute("UPDATE settings SET value=? WHERE key='app'", (dumps(settings),))
        return self.get_settings()

    def create_run(self, payload):
        with self.lifecycle_lock:
            return self._create_run(payload)

    def _create_run(self, payload):
        if not isinstance(payload, dict):
            raise AppError("INVALID_RUN", "任务应为 JSON 对象。")
        source = payload.get("source", "import")
        if not isinstance(source, str) or source not in {"demo", "import", "urls", "opencli", "comments"}:
            raise AppError("INVALID_SOURCE", "请选择演示、导入、公开链接、指定帖子评论或 OpenCLI 来源。")
        limit = payload.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise AppError("INVALID_LIMIT", "每批数量应为 1–1000。")
        config = {k: payload.get(k, []) for k in ("queries", "platforms", "urls", "records")}
        for key, value in config.items():
            if not isinstance(value, list):
                raise AppError("INVALID_RUN", f"{key} 应为数组。")
        if len(config["records"]) > 1000 or len(config["urls"]) > 200 or len(config["queries"]) > 50:
            raise AppError("LIMIT_EXCEEDED", "本轮输入过多，请拆成多个批次。")
        for key in ("queries", "platforms", "urls"):
            if any(not isinstance(x, str) or len(x) > 4096 for x in config[key]):
                raise AppError("INVALID_RUN", f"{key} 含无效内容。")
        from .processing import normalize_analysis_mode, normalize_collection, ProcessingError
        try:
            config["analysis_mode"] = normalize_analysis_mode(payload.get("analysis_mode"), self.get_settings()["model"]["enabled"])
            config["collection"] = normalize_collection(payload.get("collection"), source)
        except ProcessingError as exc:
            raise AppError("INVALID_PROCESSING", str(exc)) from None
        if config["collection"]["scope"] == "posts_and_comments" and limit < 2:
            raise AppError("INVALID_LIMIT", "帖子和评论模式至少需要 2 条结果预算，才能分别保留帖子与评论。")
        from .targeting import normalize_targeting, build_queries, active_targeting, targeting_summary, TargetingError
        try:
            target = normalize_targeting(payload.get("targeting"))
            if source == "opencli" and active_targeting(target):
                config["queries"] = build_queries(target, config["queries"])
        except TargetingError as exc:
            raise AppError("INVALID_TARGETING", str(exc)) from None
        config["targeting"] = target
        if source == "opencli":
            from .collectors import platform_catalog
            catalog = {p["id"]: p for p in platform_catalog()}
            default_platforms = ["xiaohongshu", "weibo"] if config["collection"]["scope"] != "posts" else (["xiaohongshu", "douyin"] if target["market"] == "cn" else ["tiktok"])
            config["platforms"] = list(dict.fromkeys(config["platforms"] or default_platforms))
            for platform in config["platforms"]:
                if platform not in catalog:
                    raise AppError("INVALID_PLATFORM", "未登记的平台请先通过公开链接或导入资料使用。")
                if not catalog[platform]["search_supported"]:
                    raise AppError("PLATFORM_SEARCH_UNAVAILABLE", f"{catalog[platform]['label']} 暂未接入自动搜索，请先打开平台搜索、导入原文或使用公开链接。")
                if config["collection"]["scope"] != "posts" and not catalog[platform].get("comments_supported"):
                    raise AppError("COMMENTS_UNAVAILABLE", f"{catalog[platform]['label']} 暂未接入评论读取，请选择支持评论的平台或导入评论原文。")
        if source == "import" and not config["records"]:
            raise AppError("EMPTY_INPUT", "请先导入至少一条带原文的记录。")
        if source == "comments" and config["platforms"]:
            from .collectors import platform_catalog
            supported = {p["id"] for p in platform_catalog() if p.get("comments_supported")}
            if any(p not in supported for p in config["platforms"]):
                raise AppError("COMMENTS_UNAVAILABLE", "请选择已接入只读评论的平台；其他平台可以导入评论原文。")
        if source in {"urls", "comments"} and not config["urls"]:
            raise AppError("EMPTY_INPUT", "请填写至少一个公开来源链接。")
        if source == "opencli" and config["analysis_mode"] == "collect_only" and not config["queries"]:
            raise AppError("SEARCH_QUERIES_REQUIRED", "只采集模式请填写搜索词或定向条件；它不会调用模型生成搜索词。")
        if source == "opencli" and not config["queries"] and not str(payload.get("goal", "")).strip():
            raise AppError("EMPTY_INPUT", "请填写搜索词或本轮目标。")
        for value in config["urls"]:
            safe_url(value, allow_empty=False)
        config["limit"] = min(limit, self.get_settings()["limits"]["max_candidates"])
        if config["collection"]["scope"] == "posts_and_comments" and config["limit"] < 2:
            raise AppError("INVALID_LIMIT", "本机每批结果上限不足 2 条，无法同时保留帖子和评论；请调整上限或仅采评论。")
        run = {"id": uid("run"), "name": str(payload.get("name") or "新获客批次")[:160],
               "source": source, "goal": str(payload.get("goal") or (targeting_summary(target) if active_targeting(target) else "寻找中小商家的实物采购需求，保留原文证据。"))[:8000],
               "config": config, "is_demo": source == "demo", "status": "created",
               "created_at": now(), "updated_at": now(), "completed_at": None,
               "progress": {"current": 0, "total": 0, "message": "任务已创建，尚未开始。"}, "error": None}
        with self.lock, self._db() as db:
            db.execute("INSERT INTO runs VALUES (?,?)", (run["id"], dumps(run)))
        self.log_event(run["id"], "info", "创建任务", {"source": source, "is_demo": run["is_demo"]})
        return self.get_run(run["id"])

    @staticmethod
    def _real_ai_analysis(candidate, run_source=None):
        """Inference provenance, independent of whether the source text is synthetic."""
        analysis = candidate.get("analysis") or {}
        return (run_source != "demo" and analysis.get("analysis_status") == "completed"
                and analysis.get("synthetic") is not True)

    def _ai_decision(self, candidate, run_source=None):
        decision = candidate.get("local_decision")
        return decision if self._real_ai_analysis(candidate, run_source) and decision in DECISIONS else None

    def _counts(self, db, run_id, candidates=None, run_source=None):
        counts = {"total": 0, "keep": 0, "exclude": 0, "needs_info": 0, "watch": 0, "unreviewed": 0}
        if candidates is None:
            candidates = [json.loads(row[0]) for row in db.execute("SELECT data FROM candidates WHERE run_id=?", (run_id,))]
        if run_source is None:
            run_source = self._run(db, run_id).get("source")
        for candidate in candidates:
            c = self._summary(candidate, run_source)
            counts["total"] += 1
            counts[c["effective_decision"]] += 1
        return counts

    def _batch_summary(self, run, candidates, settings):
        config, progress = run.get("config") or {}, run.get("progress") or {}
        checkpoint = progress.get("checkpoint")
        checkpoint = checkpoint if isinstance(checkpoint, dict) else None
        records = checkpoint.get("records") if checkpoint is not None else None
        requested = config.get("limit", 20)
        requested = requested if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0 else 20
        ceiling = settings.get("limits", {}).get("max_candidates", 50)
        ceiling = ceiling if isinstance(ceiling, int) and not isinstance(ceiling, bool) and ceiling > 0 else 50
        suggestions = {decision: 0 for decision in ("keep", "needs_info", "watch", "exclude")}
        ai_count = synthetic_count = not_requested_count = 0
        for candidate in candidates:
            status = (candidate.get("analysis") or {}).get("analysis_status")
            synthetic_count += status == "synthetic_demo"
            not_requested_count += status == "not_requested"
            if self._real_ai_analysis(candidate, run.get("source")):
                ai_count += 1
                decision = self._ai_decision(candidate, run.get("source"))
                if decision in suggestions:
                    suggestions[decision] += 1
        collected = len(records) if isinstance(records, list) else len(candidates)
        collection_complete = bool(checkpoint.get("collected")) if checkpoint is not None else None
        status = run.get("status", "created")
        phase = "completed" if status in {"completed", "completed_partial"} else status
        if status == "running":
            if not collection_complete:
                phase = "collecting"
            else:
                mode = config.get("analysis_mode") or ("local" if settings.get("model", {}).get("enabled") else "collect_only")
                next_index = checkpoint.get("next_index", 0)
                next_index = next_index if isinstance(next_index, int) and not isinstance(next_index, bool) else 0
                phase = ("analyzing" if mode == "local" and run.get("source") != "demo"
                         and next_index < collected else "saving")
        return {"requested_limit": requested, "effective_limit": min(requested, ceiling, 50),
                "collected": collected, "collected_basis": "checkpoint" if isinstance(records, list) else "stored_candidates",
                "processed": len(candidates), "ai_analyzed": ai_count, "synthetic": synthetic_count,
                "not_requested": not_requested_count, "unanalysed": len(candidates) - ai_count,
                "ai_suggestions": suggestions, "phase": phase, "collection_complete": collection_complete,
                "is_partial": status == "completed_partial" or bool((checkpoint or {}).get("partial"))}

    def get_run(self, run_id, include_deleted=False):
        with self.lock, self._db() as db:
            run = self._run(db, run_id, allow_deleted=include_deleted)
            candidates = [json.loads(row[0]) for row in db.execute("SELECT data FROM candidates WHERE run_id=? ORDER BY rowid", (run_id,))]
            settings = json.loads(db.execute("SELECT value FROM settings WHERE key='app'").fetchone()[0])
            run["counts"] = self._counts(db, run_id, candidates, run.get("source"))
            run["batch_summary"] = self._batch_summary(run, candidates, settings)
            run["events"] = [dict(r) for r in db.execute(
                "SELECT id,created_at,level,message,details FROM events WHERE run_id=? ORDER BY id DESC LIMIT 150", (run_id,))]
            for event in run["events"]:
                event["details"] = json.loads(event["details"]) if event["details"] else None
            return run

    def list_runs(self, scope="active"):
        if not isinstance(scope, str) or scope not in {"active", "trash", "all"}:
            raise AppError("INVALID_RUN_SCOPE", "任务范围应为 active、trash 或 all。")
        with self.lock:
            with self._db() as db:
                rows = [(row[0], json.loads(row[1])) for row in db.execute("SELECT id,data FROM runs ORDER BY rowid DESC")]
            ids = [run_id for run_id, run in rows if scope == "all" or bool(run.get("deleted_at")) == (scope == "trash")]
            result = [self.get_run(run_id, include_deleted=True) for run_id in ids]
            # Recycle-bin navigation only needs task metadata. Evidence and
            # event detail remain inaccessible until an explicit restoration.
            for run in result:
                if run.get("deleted_at"):
                    run["events"] = []
            return result

    def manage_runs(self, action, run_ids, operation_id):
        if not isinstance(action, str) or action not in {"trash", "restore", "purge"}:
            raise AppError("INVALID_RUN_ACTION", "任务操作应为 trash、restore 或 purge。")
        if (not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 100
                or any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in run_ids)
                or len(set(run_ids)) != len(run_ids)):
            raise AppError("INVALID_RUN_IDS", "请指定 1–100 个不重复的任务 ID。")
        ids = sorted(run_ids)
        payload = {"kind": "run_lifecycle", "action": action, "run_ids": ids}
        with self.lifecycle_lock, self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            digest, previous = self._operation(db, operation_id, payload)
            if previous is not None:
                return previous
            busy = sorted(set(ids).intersection(self._scheduled_runs))
            if busy:
                raise AppError("RUN_BUSY", "任务仍在执行或收尾，请暂停并等待完全停止后再操作。", 409, {"run_ids": busy})
            placeholders = ",".join("?" for _ in ids)
            runs = {row[0]: json.loads(row[1]) for row in db.execute(f"SELECT id,data FROM runs WHERE id IN ({placeholders})", ids)}
            running = [run_id for run_id, run in runs.items() if run.get("status") in {"running", "pause_requested"}]
            if running:
                raise AppError("RUN_RUNNING", "运行中的任务不能移入或清理，请先暂停并等待任务停止。", 409, {"run_ids": sorted(running)})
            missing = [run_id for run_id in ids if run_id not in runs]
            if missing:
                raise AppError("NOT_FOUND", "指定的任务不存在，未修改任何任务。", 404, {"run_ids": missing})
            for run_id in ids:
                deleted = bool(runs[run_id].get("deleted_at"))
                if action == "trash" and deleted:
                    raise AppError("RUN_DELETED", "任务已在回收站，未修改任何任务。", 410, {"run_ids": [run_id]})
                if action in {"restore", "purge"} and not deleted:
                    raise AppError("RUN_NOT_DELETED", "请先将任务移入回收站后再恢复或永久清理。", 409, {"run_ids": [run_id]})
            if action == "purge":
                db.execute(f"DELETE FROM evidence WHERE candidate_id IN (SELECT id FROM candidates WHERE run_id IN ({placeholders}))", ids)
                for table in ("candidates", "reviews", "events", "snapshots"):
                    db.execute(f"DELETE FROM {table} WHERE run_id IN ({placeholders})", ids)
                removed = set(ids)
                surviving_runs = {row[0] for row in db.execute("SELECT id FROM runs")} - removed
                for receipt in db.execute("SELECT operation_id,result FROM operations").fetchall():
                    result = json.loads(receipt[1])
                    if not isinstance(result, dict) or result.get("action") == "purge":
                        continue
                    refs = result.get("run_ids")
                    solely_removed = (isinstance(refs, list) and refs
                                      and all(isinstance(value, str) for value in refs)
                                      and bool(set(refs).intersection(removed))
                                      and not set(refs).intersection(surviving_runs))
                    if result.get("run_id") in removed or solely_removed:
                        db.execute("DELETE FROM operations WHERE operation_id=?", (receipt[0],))
                db.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", ids)
            else:
                stamp = now()
                for run_id in ids:
                    run = runs[run_id]
                    if action == "trash":
                        run["deleted_at"] = stamp
                    else:
                        run.pop("deleted_at", None)
                    db.execute("UPDATE runs SET data=? WHERE id=?", (dumps(run), run_id))
            result = {"action": action, "run_ids": ids, "operation_id": operation_id, "changed_count": len(ids)}
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, digest, dumps(result)))
            return result

    def update_run(self, run_id, **fields):
        with self.lock, self._db() as db:
            run = self._run(db, run_id)
            allowed = {"status", "progress", "error", "completed_at", "config", "snapshot_version"}
            for key, value in fields.items():
                if key not in allowed:
                    raise AppError("INVALID_RUN_UPDATE", "不能修改该任务字段。")
                run[key] = value
            run["updated_at"] = now()
            db.execute("UPDATE runs SET data=? WHERE id=?", (dumps(run), run_id))
        return self.get_run(run_id)

    def log_event(self, run_id, level, message, details=None):
        with self.lock, self._db() as db:
            self._run(db, run_id)
            db.execute("INSERT INTO events(run_id,created_at,level,message,details) VALUES (?,?,?,?,?)",
                       (run_id, now(), str(level)[:20], str(message)[:2000], dumps(details) if details is not None else None))

    def add_candidate(self, run_id, record, analysis):
        if not isinstance(record, dict) or not isinstance(analysis, dict):
            raise AppError("INVALID_CANDIDATE", "记录及分析必须为对象。")
        content = record.get("content") or record.get("text") or ""
        if not isinstance(content, str) or not content.strip():
            raise AppError("EMPTY_EVIDENCE", "候选记录缺少原文，不能入库。")
        content = content[:100000]
        key = source_key(record)
        with self.lock, self._db() as db:
            run = self._run(db, run_id)
            existing = db.execute("SELECT id FROM candidates WHERE run_id=? AND source_key=?", (run_id, key)).fetchone()
            if existing:
                return self.get_candidate(existing[0])
            candidate_id, evidence_id = uid("c"), uid("ev")
            is_demo = bool(run["is_demo"] or record.get("is_demo") is True)
            facts = analysis.get("facts") or {}
            if not isinstance(facts, dict):
                facts = {}
            role = analysis.get("role")
            role = role if role in {"buyer", "seller", "consumer", "discussion", "unknown"} else "unknown"
            decision = analysis.get("decision", "needs_info")
            if analysis.get("analysis_status") == "not_requested":
                decision = None
            elif decision not in DECISIONS:
                decision = "needs_info"
            quote = analysis.get("evidence_quote") or analysis.get("quote") or ""
            if not isinstance(quote, str) or quote not in content:
                quote = ""
            source_url = safe_url(record.get("source_url"))
            website = safe_url(facts.get("company_website") or record.get("company_website"))
            candidate = {"id": candidate_id, "run_id": run_id, "version": 1,
                         "platform": str(record.get("platform") or "web")[:60], "source_url": source_url,
                         "author_handle": record.get("author_handle"), "posted_at": record.get("posted_at"),
                         "brand_name": facts.get("brand_name") or record.get("brand_name"),
                         "company_website": website, "role": role,
                         "priority": analysis.get("priority") if analysis.get("priority") in {"A", "B", "C"} else "unknown",
                         "local_decision": decision, "local_reason": str(analysis.get("reason") or "等待核实。")[:8000],
                         "is_demo": is_demo, "created_at": now(), "updated_at": now(),
                         "analysis": json_copy(analysis), "review_state": {}}
            candidate["kind"] = str(record.get("kind") or "post")
            candidate["content_excerpt"] = content[:300]
            metadata = record.get("metadata") or {}
            candidate["parent_source_url"] = safe_url(metadata.get("parent_source_url")) if isinstance(metadata, dict) else None
            candidate["comment_id"] = metadata.get("comment_id") if isinstance(metadata, dict) else None
            match = analysis.get("target_match")
            candidate["target_match"] = json_copy(match) if isinstance(match, dict) else None
            candidate["tags"] = list(match.get("matched_tags", [])) if isinstance(match, dict) else []
            for name in ("country", "product", "quantity", "destination", "deadline", "budget", "property_type", "area", "renovation_stage", "start_date"):
                candidate[name] = facts.get(name)
            partial = bool(record.get("is_partial") or record.get("metadata", {}).get("partial") or analysis.get("input_window", {}).get("truncated"))
            acquired = record.get("metadata", {}).get("acquisition") or record.get("metadata", {}).get("read_status")
            candidate["evidence_status"] = "demo" if is_demo else "partial" if partial else "complete" if acquired in {"retrieved", "url", "opencli"} else "unverified"
            candidate["duplicate_group"] = website or (str(candidate.get("brand_name") or "").casefold() or None)
            evidence = {"id": evidence_id, "candidate_id": candidate_id,
                        "kind": str(record.get("kind") or "post"), "source_url": source_url,
                        "content": content, "quote": quote, "posted_at": record.get("posted_at"),
                        "retrieved_at": record.get("retrieved_at") or now(),
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        "is_demo": is_demo, "metadata": record.get("metadata") or {}}
            db.execute("INSERT INTO candidates VALUES (?,?,?,?,?)", (candidate_id, run_id, key, 1, dumps(candidate)))
            db.execute("INSERT INTO evidence VALUES (?,?,?)", (evidence_id, candidate_id, dumps(evidence)))
        return self.get_candidate(candidate_id)

    def _summary(self, candidate, run_source=None):
        result = {k: v for k, v in candidate.items() if k not in {"analysis", "review_state"}}
        result["analysis_status"] = candidate.get("analysis", {}).get("analysis_status")
        result["analysis_mode"] = "collect_only" if result["analysis_status"] == "not_requested" else "demo" if result["analysis_status"] == "synthetic_demo" else "local"
        result["ai_decision"] = self._ai_decision(candidate, run_source)
        state = candidate.get("review_state", {})
        for reviewer in REVIEWERS:
            result[reviewer + "_decision"] = state.get(reviewer, {}).get("decision")
        active = next((state[r] for r in ("human", "codex", "dsh") if state.get(r)), None)
        result["effective_decision"] = active["decision"] if active else "unreviewed"
        result["review_reason"] = active.get("reason") if active else None
        result["reviewer"] = active.get("reviewer") if active else None
        return result

    def get_candidate(self, candidate_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT data FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not row:
                raise AppError("NOT_FOUND", "该候选记录不存在。", 404)
            data = json.loads(row[0])
            run = self._run(db, data["run_id"])
            result = self._summary(data, run.get("source"))
            result["analysis"] = data["analysis"]
            result["evidence"] = [json.loads(r[0]) for r in db.execute("SELECT data FROM evidence WHERE candidate_id=?", (candidate_id,))]
            result["reviews"] = list(data.get("review_state", {}).values())
            return result

    def list_candidates(self, run_id, q="", decision="", limit=100, offset=0, ai_decision="", order=""):
        if not isinstance(ai_decision, str) or ai_decision not in {"", "keep", "needs_info", "watch", "exclude", "unanalysed"}:
            raise AppError("INVALID_AI_DECISION", "AI 建议筛选应为 keep、needs_info、watch、exclude 或 unanalysed。")
        if not isinstance(order, str) or order not in {"", "collected", "ai_priority"}:
            raise AppError("INVALID_ORDER", "排序方式应为 collected 或 ai_priority。")
        with self.lock, self._db() as db:
            run = self._run(db, run_id)
            rows = [self._summary(json.loads(r[0]), run.get("source")) for r in db.execute("SELECT data FROM candidates WHERE run_id=? ORDER BY rowid", (run_id,))]
        needle = str(q).casefold().strip()
        if needle:
            fields = ("brand_name", "author_handle", "product", "country", "local_reason", "source_url", "tags", "content_excerpt")
            rows = [r for r in rows if any(needle in str(r.get(f) or "").casefold() for f in fields)]
        if decision:
            rows = [r for r in rows if r["effective_decision"] == decision]
        if ai_decision:
            rows = [r for r in rows if r["ai_decision"] == (None if ai_decision == "unanalysed" else ai_decision)]
        if order == "ai_priority":
            decisions = {value: number for number, value in enumerate(("keep", "needs_info", "watch", "exclude", None))}
            priorities = {"A": 0, "B": 1, "C": 2}
            rows.sort(key=lambda candidate: (decisions[candidate["ai_decision"]], priorities.get(candidate.get("priority"), 3)))
        limit, offset = max(1, min(int(limit), 1000)), max(0, int(offset))
        return {"items": rows[offset:offset + limit], "total": len(rows)}

    def _operation(self, db, operation_id, payload):
        if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 150:
            raise AppError("OPERATION_ID_REQUIRED", "请为本次操作提供稳定且唯一的 operation_id。")
        digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
        existing = db.execute("SELECT payload_hash,result FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise AppError("IDEMPOTENCY_CONFLICT", "同一 operation_id 已用于不同的内容。", 409)
            return digest, json.loads(existing[1])
        return digest, None

    def submit_review(self, payload):
        with self.lifecycle_lock:
            return self._submit_review(payload)

    def _submit_review(self, payload):
        if not isinstance(payload, dict):
            raise AppError("INVALID_REVIEW", "审核内容应为对象。")
        reviewer, run_id = payload.get("reviewer"), payload.get("run_id")
        changes = payload.get("changes")
        if not isinstance(reviewer, str) or reviewer not in REVIEWERS or not isinstance(run_id, str) or not isinstance(changes, list) or not 1 <= len(changes) <= 1000:
            raise AppError("INVALID_REVIEW", "请指定审核者及 1–1000 条审核变更。")
        operation_id = payload.get("operation_id")
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._run(db, run_id)
            digest, previous = self._operation(db, operation_id, payload)
            if previous:
                return previous
            batch_id, stamp, results, seen = uid("rv"), now(), [], set()
            # Validate the whole changeset before writing any candidate.
            prepared = []
            for change in changes:
                if not isinstance(change, dict):
                    raise AppError("INVALID_REVIEW", "审核变更应为对象。")
                cid, version = change.get("candidate_id"), change.get("base_version")
                if not isinstance(cid, str) or not cid:
                    raise AppError("INVALID_REVIEW", "候选 ID 必须为文字。")
                if cid in seen:
                    raise AppError("DUPLICATE_CHANGE", "同一批审核不能重复修改一条候选。")
                seen.add(cid)
                row = db.execute("SELECT version,data FROM candidates WHERE id=? AND run_id=?", (cid, run_id)).fetchone()
                if not row:
                    raise AppError("NOT_FOUND", "审核引用的候选不属于本批次。", 404)
                if isinstance(version, bool) or not isinstance(version, int) or row[0] != version:
                    raise AppError("VERSION_CONFLICT", "候选已被其他审核更新，请读取最新版本后重试。", 409,
                                   {"candidate_id": cid, "expected": row[0], "provided": version})
                decision, reason = change.get("decision"), change.get("reason")
                if not isinstance(decision, str) or decision not in DECISIONS or not isinstance(reason, str) or not reason.strip() or len(reason) > 8000:
                    raise AppError("INVALID_REVIEW", "请选择有效的审核结论，并填写理由。")
                evidence_ids = change.get("evidence_ids", [])
                valid_ids = {r[0] for r in db.execute("SELECT id FROM evidence WHERE candidate_id=?", (cid,))}
                if not isinstance(evidence_ids, list) or not all(isinstance(e, str) and e in valid_ids for e in evidence_ids):
                    raise AppError("INVALID_EVIDENCE_REFERENCE", "审核引用了不属于该候选的证据。")
                tags = change.get("tags", [])
                if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or len(t) > 100 for t in tags):
                    raise AppError("INVALID_REVIEW", "标签应为不超过 20 项的文字数组。")
                candidate = json.loads(row[1])
                before = copy.deepcopy(candidate.setdefault("review_state", {}).get(reviewer))
                after = {"reviewer": reviewer, "decision": decision, "reason": reason.strip(),
                         "evidence_ids": evidence_ids, "tags": tags, "changeset_id": batch_id, "created_at": stamp}
                candidate["review_state"][reviewer] = after
                candidate["version"] += 1
                candidate["updated_at"] = stamp
                prepared.append(candidate)
                results.append({"candidate_id": cid, "base_version": version, "after_version": version + 1,
                                "decision": decision, "reason": reason.strip(), "before": before, "after": after})
            for candidate in prepared:
                db.execute("UPDATE candidates SET version=?,data=? WHERE id=?", (candidate["version"], dumps(candidate), candidate["id"]))
            result = {"id": batch_id, "run_id": run_id, "reviewer": reviewer, "operation_id": operation_id,
                      "status": "applied", "changes": results, "created_at": stamp, "reverted_at": None}
            db.execute("INSERT INTO reviews VALUES (?,?,?)", (batch_id, run_id, dumps(result)))
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, digest, dumps(result)))
        self.log_event(run_id, "info", f"{reviewer} 提交审核：{len(changes)} 条", {"changeset_id": batch_id})
        return result

    def list_reviews(self, run_id):
        with self.lock, self._db() as db:
            self._run(db, run_id)
            return {"items": [json.loads(r[0]) for r in db.execute("SELECT data FROM reviews WHERE run_id=? ORDER BY rowid DESC", (run_id,))]}

    def revert_review(self, batch_id, operation_id):
        with self.lifecycle_lock:
            return self._revert_review(batch_id, operation_id)

    def _revert_review(self, batch_id, operation_id):
        payload = {"kind": "revert", "changeset_id": batch_id}
        with self.lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM reviews WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise AppError("NOT_FOUND", "该审核批次不存在。", 404)
            batch = json.loads(row[0])
            self._run(db, batch["run_id"])
            digest, previous = self._operation(db, operation_id, payload)
            if previous:
                return previous
            if batch["status"] != "applied":
                raise AppError("ALREADY_REVERTED", "该批审核已经撤销。", 409)
            prepared = []
            for change in batch["changes"]:
                row = db.execute("SELECT version,data FROM candidates WHERE id=?", (change["candidate_id"],)).fetchone()
                if not row or row[0] != change["after_version"]:
                    raise AppError("VERSION_CONFLICT", "审核后已有其他修改，无法整批撤销。", 409,
                                   {"candidate_id": change["candidate_id"]})
                candidate = json.loads(row[1])
                if change["before"] is None:
                    candidate["review_state"].pop(batch["reviewer"], None)
                else:
                    candidate["review_state"][batch["reviewer"]] = change["before"]
                candidate["version"] += 1
                candidate["updated_at"] = now()
                prepared.append(candidate)
            for candidate in prepared:
                db.execute("UPDATE candidates SET version=?,data=? WHERE id=?", (candidate["version"], dumps(candidate), candidate["id"]))
            batch.update(status="reverted", reverted_at=now())
            db.execute("UPDATE reviews SET data=? WHERE id=?", (dumps(batch), batch_id))
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, digest, dumps(batch)))
        self.log_event(batch["run_id"], "info", "撤销审核批次", {"changeset_id": batch_id})
        return batch

    def freeze_run(self, run_id):
        with self.lock, self._db() as db:
            self._run(db, run_id)
            existing = db.execute("SELECT data FROM snapshots WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                return json.loads(existing[0])
            candidates = [self.get_candidate(r[0]) for r in db.execute("SELECT id FROM candidates WHERE run_id=? ORDER BY rowid", (run_id,))]
            run = self.get_run(run_id)
            run.get("progress", {}).pop("checkpoint", None)
            run.get("config", {}).pop("records", None)
            analyzed = [c for c in candidates if c.get("analysis", {}).get("analysis_status") != "not_requested"]
            versions = sorted({str(c.get("analysis", {}).get("prompt_version") or "unknown") for c in analyzed})
            models = sorted({str(c.get("analysis", {}).get("model") or "unknown") for c in analyzed})
            snapshot = {"schema_version": "1", "run_id": run_id, "snapshot_id": uid("snap"), "created_at": now(),
                        "run": run, "candidates": candidates, "prompt_version": versions[0] if len(versions) == 1 else None,
                        "prompt_versions": versions, "models": models}
            snapshot["analysis_modes"] = sorted({"collect_only" if c.get("analysis", {}).get("analysis_status") == "not_requested" else "demo" if c.get("analysis", {}).get("analysis_status") == "synthetic_demo" else "local" for c in candidates})
            db.execute("INSERT INTO snapshots VALUES (?,?,?)", (run_id, snapshot["created_at"], dumps(snapshot)))
            return snapshot

    def export_run(self, run_id):
        with self.lock, self._db() as db:
            self._run(db, run_id)
            snap = db.execute("SELECT data FROM snapshots WHERE run_id=?", (run_id,)).fetchone()
            current = [self.get_candidate(r[0]) for r in db.execute("SELECT id FROM candidates WHERE run_id=? ORDER BY rowid", (run_id,))]
            run = self.get_run(run_id)
            run.get("progress", {}).pop("checkpoint", None)
            run.get("config", {}).pop("records", None)
            return {"schema_version": "1", "exported_at": now(), "run": run,
                    "snapshot": json.loads(snap[0]) if snap else None,
                    "candidates": current, "reviews": self.list_reviews(run_id)["items"],
                    "review_template": {"run_id": run_id, "reviewer": "codex", "operation_id": str(uuid.uuid4()), "changes": []},
                    "review_instructions": "按当前候选version填写base_version；仅提交审核判断与证据引用，不改原文。先看全部摘要，再按需读证据，抽查本地排除项。数量/预算/付款没有证据时保持未知。"}

    def export_csv(self, run_id):
        rows = self.list_candidates(run_id, limit=1000)["items"]
        fields = ["id", "version", "brand_name", "platform", "country", "product", "quantity", "destination", "deadline", "role", "priority", "evidence_status", "local_decision", "local_reason", "codex_decision", "human_decision", "effective_decision", "review_reason", "source_url", "is_demo"]
        fields += ["tags", "target_match_status", "target_match_reason", "matched_region", "budget", "property_type", "area", "renovation_stage", "start_date"]
        fields += ["kind", "author_handle", "parent_source_url", "comment_id", "analysis_status", "content_excerpt"]
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row = dict(row)
            match = row.get("target_match") or {}
            row["tags"] = "、".join(row.get("tags", []))
            row["target_match_status"] = match.get("status")
            row["target_match_reason"] = match.get("reason")
            row["matched_region"] = match.get("matched_region")
            cleaned = {}
            for k in fields:
                v = row.get(k)
                # Prevent formulas when an untrusted source is opened in spreadsheet software.
                if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    v = "'" + v
                cleaned[k] = v
            writer.writerow(cleaned)
        return "\ufeff" + out.getvalue()
