from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import secrets
import signal
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .store import AppError, Store


def app_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_data_dir():
    from .cli import default_data_dir as shared_default
    return shared_default()


def public_run(run):
    data = copy.deepcopy(run)
    data.get("progress", {}).pop("checkpoint", None)
    if isinstance(data.get("config"), dict):
        records = data["config"].pop("records", [])
        data["config"]["imported_record_count"] = len(records)
    return data


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, data_dir):
        from .engine import RunEngine
        self.engine = None
        self._data_lock = None
        # Bind before opening the DB: a duplicate launch must not recover the
        # first instance's live tasks as interrupted.
        super().__init__(address, Handler)
        try:
            directory = Path(data_dir).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            lock = (directory / "instance.lock").open("a+b")
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    if not lock.read(1):
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise AppError("INSTANCE_RUNNING", "该数据目录已由另一个获客平台实例使用。", 409) from None
            self._data_lock = lock
            self.store = Store(directory)
            self.token = secrets.token_urlsafe(32)
            self.instance_id = secrets.token_hex(16)
            self.engine = RunEngine(self.store)
            self.static_dir = Path(getattr(sys, "_MEIPASS", app_root())) / "static"
        except Exception:
            self.server_close()
            raise

    def server_close(self):
        if self.engine:
            self.engine.shutdown()
        super().server_close()
        if self._data_lock:
            self._data_lock.close()
            self._data_lock = None


class Handler(BaseHTTPRequestHandler):
    server_version = "LeadWorkbench/" + __version__

    def log_message(self, fmt, *args):
        # No query strings, model credentials, or request payloads in access logs.
        if os.environ.get("LEADWORKBENCH_HTTP_LOG") == "1":
            print(f"[http] {self.command} {urlsplit(self.path).path}", file=sys.stderr)

    def _allowed_host(self):
        host = self.headers.get("Host", "")
        port = self.server.server_port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _headers(self, status, content_type="application/json; charset=utf-8", length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, data, status=200):
        body = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _error(self, exc):
        if isinstance(exc, AppError):
            code, message, status, details = exc.code, exc.message, exc.status, exc.details
        else:
            code, message, status, details = "INTERNAL_ERROR", "程序遇到错误，请查看运行日志。", 500, None
            traceback.print_exc(file=sys.stderr)
        body = json.dumps({"ok": False, "error": {"code": code, "message": message, "details": details}}, ensure_ascii=False).encode("utf-8")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError("INVALID_BODY", "请求长度不正确。")
        if not 0 <= size <= 4 * 1024 * 1024:
            raise AppError("BODY_TOO_LARGE", "一次请求最多 4MB，请拆分导入。", 413)
        if size and self.headers.get_content_type() != "application/json":
            raise AppError("JSON_REQUIRED", "请求必须使用 application/json。", 415)
        try:
            data = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
        except (ValueError, UnicodeDecodeError):
            raise AppError("INVALID_JSON", "JSON 格式不正确。")
        if not isinstance(data, dict):
            raise AppError("OBJECT_REQUIRED", "请求根节点必须是 JSON 对象。")
        return data

    def _guard(self, mutation=False):
        if not self._allowed_host():
            raise AppError("HOST_DENIED", "仅接受本机地址访问。", 403)
        if mutation:
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if origin and origin not in allowed:
                raise AppError("ORIGIN_DENIED", "写入请求必须来自本程序界面。", 403)
            if not secrets.compare_digest(self.headers.get("X-App-Token", ""), self.server.token):
                raise AppError("TOKEN_REQUIRED", "连接凭据已过期，请刷新界面或重连本程序。", 403)

    def do_OPTIONS(self):
        self._error(AppError("CORS_DISABLED", "本地程序不开放跨站请求。", 403))

    def do_GET(self):
        try:
            self._guard()
            p = urlsplit(self.path)
            path, query = p.path, parse_qs(p.query)
            getq = lambda key, default="": query.get(key, [default])[0]
            store = self.server.store
            if path == "/api/health":
                return self._send({"ready": True, "version": __version__, "app_name": "获客平台", "instance_id": self.server.instance_id})
            if path == "/api/bootstrap":
                import shutil
                from .collectors import platform_catalog
                return self._send({"app_version": __version__, "app_name": "获客平台", "token": self.server.token,
                                   "settings": store.get_settings(),
                                   "capabilities": {"sources": ["demo", "import", "urls", "opencli", "comments"], "analysis_modes": ["collect_only", "local"], "opencli_installed": bool(shutil.which(store.get_settings()["opencli"]["executable"])),
                                                    "model_runtime": "Strata / OpenAI-compatible", "concurrency": 1, "max_batch_candidates": 50,
                                                    "platforms": platform_catalog()}})
            if path == "/api/settings":
                return self._send(store.get_settings())
            if path == "/api/runs":
                return self._send({"items": [public_run(r) for r in store.list_runs(getq("scope", "active"))]})
            if path == "/api/candidates":
                try:
                    data = store.list_candidates(getq("run_id"), getq("q"), getq("decision"), int(getq("limit", "100")), int(getq("offset", "0")),
                                                 ai_decision=getq("ai_decision"), order=getq("order"))
                except ValueError:
                    raise AppError("INVALID_PAGINATION", "分页参数不正确。")
                return self._send(data)
            if path == "/api/reviews":
                return self._send(store.list_reviews(getq("run_id")))
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "candidates"]:
                return self._send(store.get_candidate(parts[2]))
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._send(public_run(store.get_run(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "export":
                fmt = getq("format", "json")
                if fmt == "csv":
                    body, ct = store.export_csv(parts[2]).encode("utf-8"), "text/csv; charset=utf-8"
                elif fmt == "json":
                    body = json.dumps(store.export_run(parts[2]), ensure_ascii=False, indent=2).encode("utf-8")
                    ct = "application/json; charset=utf-8"
                else:
                    raise AppError("INVALID_FORMAT", "导出格式仅支持 JSON 或 CSV。")
                self._headers(200, ct, len(body), {"Content-Disposition": f'attachment; filename="{parts[2]}.{fmt}"'})
                self.wfile.write(body)
                return
            allowed_static = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js",
                              "/app-icon.png": "app-icon.png", "/app.ico": "app.ico", "/favicon.ico": "app.ico"}
            if path in allowed_static:
                file = self.server.static_dir / allowed_static[path]
                if not file.is_file():
                    raise AppError("UI_NOT_READY", "界面资源不存在，请使用完整程序目录启动。", 503)
                body = file.read_bytes()
                ct = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                self._headers(200, ct + ("; charset=utf-8" if ct.startswith("text/") or ct.endswith("javascript") else ""), len(body))
                self.wfile.write(body)
                return
            raise AppError("NOT_FOUND", "未找到该页面或接口。", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        self._mutate("POST")

    def do_PUT(self):
        self._mutate("PUT")

    def _mutate(self, method):
        try:
            self._guard(mutation=True)
            payload = self._body()
            path = urlsplit(self.path).path
            store = self.server.store
            if method == "PUT" and path == "/api/settings":
                return self._send(store.update_settings(payload))
            if method == "POST" and path == "/api/runs":
                return self._send(public_run(store.create_run(payload)), 201)
            if method == "POST" and path in {"/api/runs/trash", "/api/runs/restore", "/api/runs/purge"}:
                if not isinstance(payload, dict):
                    raise AppError("INVALID_RUN_IDS", "任务操作应包含 run_ids 和 operation_id。")
                return self._send(self.server.engine.manage_runs(path.rsplit("/", 1)[1], payload.get("run_ids"), payload.get("operation_id")))
            if method == "POST" and path == "/api/reviews":
                return self._send(store.submit_review(payload))
            if method == "POST" and path == "/api/import/validate":
                from .collectors import normalize_records
                records, warnings = normalize_records(payload.get("records", []))
                return self._send({"records": records, "warnings": warnings, "count": len(records)})
            if method == "POST" and path == "/api/search/preview":
                from .targeting import search_preview, TargetingError
                from .collectors import platform_catalog
                try:
                    return self._send(search_preview(payload, platform_catalog()))
                except TargetingError as exc:
                    raise AppError("INVALID_TARGETING", str(exc)) from None
            if method == "POST" and path == "/api/model/test":
                from .models import test_connection
                settings = store.get_settings(include_secret=True)
                patch = payload.get("model", payload)
                if isinstance(patch, dict):
                    settings["model"].update({k: v for k, v in patch.items() if k != "api_key_configured"})
                result = test_connection(settings)
                return self._send(result)
            parts = path.strip("/").split("/")
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in {"start", "pause", "resume"}:
                try:
                    getattr(self.server.engine, parts[3])(parts[2])
                except ValueError as exc:
                    raise AppError("INVALID_RUN_STATE", str(exc), 409) from None
                return self._send(public_run(store.get_run(parts[2])))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "reviews"] and parts[3] == "revert":
                return self._send(store.revert_review(parts[2], payload.get("operation_id")))
            raise AppError("NOT_FOUND", "未找到该操作接口。", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if not isinstance(exc, AppError) and hasattr(exc, "code"):
                exc = AppError(str(exc.code), str(exc), getattr(exc, "status", 400))
            self._error(exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description="获客平台本地服务")
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    server = WorkbenchServer(("127.0.0.1", args.port), args.data_dir)
    url = f"http://127.0.0.1:{server.server_port}"
    state_file = Path(args.data_dir).resolve() / "server.json"
    state = {"base_url": url, "token": server.token, "pid": os.getpid(),
             "data_dir": str(Path(args.data_dir).resolve()), "instance_id": server.instance_id}
    temp = state_file.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, state_file)
    print(json.dumps({"ready": True, "base_url": url, "data_dir": state["data_dir"]}, ensure_ascii=False), flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            current = json.loads(state_file.read_text(encoding="utf-8"))
            if current.get("instance_id") == server.instance_id:
                state_file.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
