"""Local HTTP client and command line entry points; never opens the database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_BODY = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
DEFAULT_PORT = 8766


class ApiError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


def default_data_dir() -> Path:
    configured = os.environ.get("LEADWORKBENCH_HOME") or os.environ.get("LEADWORKBENCH_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        root = executable.parent
        if executable.stem.lower() == "leadworkbenchdesktop" and root.name.lower() == "leadworkbenchdesktop":
            root = root.parent / "LeadWorkbench"
    else:
        root = Path(__file__).resolve().parent.parent
    return root / "data"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("REDIRECT_BLOCKED", "Local application redirects are not allowed.")


class ApiClient:
    """Reads instance address/token from server.json for each request."""
    def __init__(self, data_dir: str | Path | None = None, timeout: float = 30):
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def connection(self) -> tuple[str, str]:
        path = self.data_dir / "server.json"
        try:
            if path.stat().st_size > 65536:
                raise ApiError("SERVER_CONFIG", "server.json is too large.")
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ApiError("SERVER_OFFLINE", "Application server is not available. Start the app with start.cmd.") from exc
        if not isinstance(config, dict):
            raise ApiError("SERVER_CONFIG", "server.json must contain an object.")
        base = config.get("base_url", config.get("url", ""))
        token = config.get("token", "")
        if not isinstance(base, str) or not isinstance(token, str):
            raise ApiError("SERVER_CONFIG", "server.json address/token has an invalid type.")
        try:
            parsed = urllib.parse.urlsplit(base)
            port = parsed.port
        except ValueError as exc:
            raise ApiError("SERVER_CONFIG", "Invalid local server address.") from exc
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ApiError("SERVER_CONFIG", "Only an http://127.0.0.1:<port> application address is accepted.")
        if not token or len(token) > 4096 or "\r" in token or "\n" in token:
            raise ApiError("SERVER_CONFIG", "Application instance token is missing or invalid.")
        return base.rstrip("/"), token

    def raw(self, method: str, path: str, body: Any = None) -> tuple[bytes, str]:
        if not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise ApiError("ARGUMENT", "Invalid application API path.")
        base, token = self.connection()
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_BODY:
                raise ApiError("BODY_TOO_LARGE", "Request exceeds the 4 MB limit.")
        headers = {"Accept": "application/json", "X-App-Token": token}
        if encoded is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(base + path, data=encoded, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE + 1)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                error = payload["error"]
                raise ApiError(str(error.get("code", "HTTP_ERROR")), str(error.get("message", "Application request failed.")), error.get("details")) from None
            raise ApiError("HTTP_ERROR", f"Application returned HTTP {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError("SERVER_OFFLINE", "Cannot reach the local application server. Check that it is running.") from exc
        if len(raw) > MAX_RESPONSE:
            raise ApiError("RESPONSE_TOO_LARGE", "Application response exceeds the client limit; request a smaller page.")
        return raw, content_type

    def request(self, method: str, path: str, body: Any = None, *, envelope: bool = True) -> Any:
        raw, _ = self.raw(method, path, body)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ApiError("INVALID_RESPONSE", "Application did not return valid JSON.") from exc
        if isinstance(result, dict) and result.get("ok") is False:
            error = result.get("error") or {}
            raise ApiError(str(error.get("code", "API_ERROR")), str(error.get("message", "Application request failed.")), error.get("details"))
        if envelope:
            if not isinstance(result, dict) or result.get("ok") is not True or "data" not in result:
                raise ApiError("INVALID_RESPONSE", "Application response is missing its result envelope.")
            return result["data"]
        if isinstance(result, dict) and result.get("ok") is True and "data" in result:
            return result["data"]
        return result


def api_path(path: str, **params: Any) -> str:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    return path + ("?" + query if query else "")


def object_id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def read_json_file(path: str) -> Any:
    source = Path(path).expanduser()
    try:
        if source.stat().st_size > MAX_BODY:
            raise ApiError("BODY_TOO_LARGE", "Input JSON exceeds 4 MB.")
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ApiError("INPUT_FILE", "Cannot read a valid UTF-8 JSON input file.") from exc


def review_payload(value: Any, reviewer: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ApiError("ARGUMENT", "Review JSON must be an object.")
    proposal = value.get("proposed_review", value.get("review_template", value.get("review", value)))
    if not isinstance(proposal, dict):
        raise ApiError("ARGUMENT", "proposed_review must be an object.")
    result = {key: proposal[key] for key in ("run_id", "reviewer", "operation_id", "changes") if key in proposal}
    if "run_id" not in result:
        result["run_id"] = value.get("run_id", (value.get("run") or {}).get("id") if isinstance(value.get("run"), dict) else None)
    if reviewer:
        result["reviewer"] = reviewer
    if not result.get("run_id") or result.get("reviewer") not in ("human", "codex", "dsh"):
        raise ApiError("ARGUMENT", "Review requires run_id and reviewer (human, codex or dsh).")
    if not isinstance(result.get("changes"), list) or not result["changes"]:
        raise ApiError("ARGUMENT", "Review requires a nonempty changes array.")
    if not result.get("operation_id"):
        canonical = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        result["operation_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "leadworkbench-review:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()))
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ApiError("ARGUMENT", message)


def _page_limit(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return result


def _add_targeting_arguments(command):
    command.add_argument("--targeting-file", help="定向条件 JSON 文件")
    command.add_argument("--market", choices=("cn", "global"))
    command.add_argument("--industry")
    command.add_argument("--region", action="append")
    command.add_argument("--include-tag", action="append")
    command.add_argument("--exclude-tag", action="append")
    command.add_argument("--customer-type", choices=("consumer", "business", "any"))
    command.add_argument("--needs")
    command.add_argument("--condition", action="append", metavar="NAME=VALUE", help="补充条件，可重复")


def _targeting_payload(args):
    value = read_json_file(args.targeting_file) if args.targeting_file else {}
    if not isinstance(value, dict):
        raise ApiError("ARGUMENT", "Targeting file must contain an object.")
    value = dict(value)
    for flag, key in (("market", "market"), ("industry", "industry"), ("region", "regions"),
                      ("include_tag", "include_tags"), ("exclude_tag", "exclude_tags"),
                      ("customer_type", "customer_type"), ("needs", "needs")):
        item = getattr(args, flag, None)
        if item is not None:
            value[key] = item
    if args.condition:
        custom = value.get("custom_fields", {})
        if not isinstance(custom, dict):
            raise ApiError("ARGUMENT", "custom_fields must be an object.")
        custom = dict(custom)
        for condition in args.condition:
            key, separator, text = condition.partition("=")
            if not separator or not key.strip() or key.strip() in custom:
                raise ApiError("ARGUMENT", "Each condition must have a unique NAME=VALUE.")
            custom[key.strip()] = text.strip()
        value["custom_fields"] = custom
    return value


def parser() -> argparse.ArgumentParser:
    root = _Parser(prog="leadworkbench", description="获客平台本地 API 客户端；输出 JSON。")
    root.add_argument("--data-dir", type=Path, default=None, help="数据目录；默认项目旁 data")
    root.add_argument("--timeout", type=float, default=30, help="HTTP 等待秒数")
    sub = root.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动本地应用服务")
    serve.add_argument("--data-dir", dest="serve_data_dir", type=Path)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--no-browser", action="store_true")
    runs = sub.add_parser("runs", help="列出、创建或控制运行")
    actions = runs.add_mutually_exclusive_group()
    actions.add_argument("--create", action="store_true")
    for action in ("start", "pause", "resume"):
        actions.add_argument("--" + action, metavar="RUN_ID")
    runs.add_argument("--name", default="新运行")
    runs.add_argument("--source", choices=("import", "urls", "opencli", "comments"), default="urls")
    runs.add_argument("--analysis-mode", choices=("collect_only", "local"), help="本轮只采集，或连接设置中的本地模型分析")
    runs.add_argument("--scope", choices=("posts", "comments", "posts_and_comments"))
    runs.add_argument("--comment-keyword", action="append", help="评论关键词，可重复；只筛选实际读取的评论样本")
    runs.add_argument("--comment-match", choices=("any", "all"))
    runs.add_argument("--max-posts", type=int)
    runs.add_argument("--comments-per-post", type=int)
    runs.add_argument("--with-replies", action="store_true", default=None)
    runs.add_argument("--goal", default="")
    runs.add_argument("--query", action="append", default=[])
    runs.add_argument("--platform", action="append", default=[])
    runs.add_argument("--url", action="append", default=[])
    runs.add_argument("--records", help="导入记录 JSON 文件")
    runs.add_argument("--limit", type=_page_limit, default=20)
    _add_targeting_arguments(runs)
    preview = sub.add_parser("search-preview", help="预览国内/海外定向条件和搜索词，不执行平台采集")
    _add_targeting_arguments(preview)
    preview.add_argument("--query", action="append", default=[])
    preview.add_argument("--platform", action="append", default=[])
    show = sub.add_parser("show", help="运行详情与事件")
    show.add_argument("run_id")
    candidates = sub.add_parser("candidates", help="候选分页列表")
    candidates.add_argument("--run", dest="run_id")
    candidates.add_argument("--q")
    candidates.add_argument("--decision", choices=("keep", "exclude", "needs_info", "watch", "unreviewed"))
    candidates.add_argument("--limit", type=_page_limit, default=25)
    candidates.add_argument("--offset", type=int, default=0)
    evidence = sub.add_parser("evidence", help="候选详情、证据和版本")
    evidence.add_argument("candidate_id")
    export = sub.add_parser("review-export", help="导出不可变快照及审核草案")
    export.add_argument("run_id")
    export.add_argument("--format", choices=("json", "csv"), default="json")
    export.add_argument("--output", type=Path)
    export.add_argument("--force", action="store_true", help="允许替换已有导出文件")
    apply = sub.add_parser("review-apply", help="提交版本检查的原子审核变更集")
    apply.add_argument("file")
    apply.add_argument("--reviewer", choices=("human", "codex", "dsh"))
    revert = sub.add_parser("review-revert", help="撤销一个审核变更集")
    revert.add_argument("review_id")
    revert.add_argument("--operation-id")
    demo = sub.add_parser("demo", help="创建并启动明确标注的合成演示")
    demo.add_argument("--name", default="合成演示 · 非真实客户")
    demo.add_argument("--wait", action="store_true")
    test = sub.add_parser("model-test", help="通过应用测试已配置本地模型")
    test.add_argument("--config", help="可选模型配置 JSON 文件")
    sub.add_parser("mcp", help="运行 stdio MCP 服务")
    return root


def execute(args: argparse.Namespace) -> Any:
    data_dir = args.data_dir or default_data_dir()
    if args.command == "serve":
        from . import server
        server_args = ["--data-dir", str(args.serve_data_dir or data_dir), "--port", str(args.port)]
        if args.no_browser:
            server_args.append("--no-browser")
        return server.main(server_args)
    client = ApiClient(data_dir, timeout=args.timeout)
    if args.command == "mcp":
        from .mcp import serve
        return serve(client)
    if args.command == "runs":
        for action in ("start", "pause", "resume"):
            if getattr(args, action):
                return client.request("POST", f"/api/runs/{object_id(getattr(args, action))}/{action}", {})
        if args.create:
            records = read_json_file(args.records) if args.records else []
            if isinstance(records, dict):
                records = records.get("records")
            if not isinstance(records, list):
                raise ApiError("ARGUMENT", "Import records must be a JSON array or an object with records.")
            payload = {"name": args.name, "source": args.source, "goal": args.goal,
                "queries": args.query, "platforms": args.platform, "urls": args.url, "records": records, "limit": args.limit,
                "targeting": _targeting_payload(args)}
            if args.analysis_mode is not None:
                payload["analysis_mode"] = args.analysis_mode
            collection = {key: getattr(args, flag) for flag, key in (
                ("scope", "scope"), ("comment_keyword", "keywords"), ("comment_match", "match_mode"),
                ("max_posts", "max_posts"), ("comments_per_post", "max_comments_per_post"),
                ("with_replies", "include_replies")) if getattr(args, flag) is not None}
            if collection:
                payload["collection"] = collection
            return client.request("POST", "/api/runs", payload)
        return client.request("GET", "/api/runs")
    if args.command == "search-preview":
        return client.request("POST", "/api/search/preview", {"targeting": _targeting_payload(args), "queries": args.query, "platforms": args.platform})
    if args.command == "show":
        return client.request("GET", f"/api/runs/{object_id(args.run_id)}")
    if args.command == "candidates":
        if args.offset < 0:
            raise ApiError("ARGUMENT", "offset cannot be negative.")
        return client.request("GET", api_path("/api/candidates", run_id=args.run_id, q=args.q,
            decision=args.decision, limit=args.limit, offset=args.offset))
    if args.command == "evidence":
        return client.request("GET", f"/api/candidates/{object_id(args.candidate_id)}")
    if args.command == "review-export":
        path = api_path(f"/api/runs/{object_id(args.run_id)}/export", format=args.format)
        if args.output:
            if args.output.exists() and not args.force:
                raise ApiError("OUTPUT_EXISTS", "Export file exists; use another path or --force.")
            raw, _ = client.raw("GET", path)
            if args.format == "json":
                try:
                    parsed = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise ApiError("INVALID_RESPONSE", "Export was not valid JSON.") from exc
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    raise ApiError("EXPORT_ERROR", "Application rejected this export.")
            destination = args.output.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_bytes(raw)
            os.replace(temporary, destination)
            return {"path": str(destination), "format": args.format, "bytes": len(raw)}
        if args.format != "json":
            raise ApiError("ARGUMENT", "CSV exports require --output so stdout remains JSON.")
        return client.request("GET", path, envelope=False)
    if args.command == "review-apply":
        return client.request("POST", "/api/reviews", review_payload(read_json_file(args.file), args.reviewer))
    if args.command == "review-revert":
        operation = args.operation_id or str(uuid.uuid5(uuid.NAMESPACE_URL, "leadworkbench-revert:" + args.review_id))
        return client.request("POST", f"/api/reviews/{object_id(args.review_id)}/revert", {"operation_id": operation})
    if args.command == "model-test":
        config = read_json_file(args.config) if args.config else {}
        if not isinstance(config, dict):
            raise ApiError("ARGUMENT", "Model configuration must be an object.")
        return client.request("POST", "/api/model/test", config)
    if args.command == "demo":
        run = client.request("POST", "/api/runs", {"name": args.name, "source": "demo", "goal": "检查证据与审核流程；合成数据不是客户。", "limit": 20})
        run_id = run["id"]
        run = client.request("POST", f"/api/runs/{object_id(run_id)}/start", {})
        if args.wait:
            deadline = time.monotonic() + max(1, args.timeout)
            while run.get("status") not in ("completed", "completed_partial", "failed", "interrupted", "paused"):
                if time.monotonic() >= deadline:
                    raise ApiError("WAIT_TIMEOUT", "Demo is still running; use show to inspect it.", {"run_id": run_id})
                time.sleep(0.2)
                run = client.request("GET", f"/api/runs/{object_id(run_id)}")
        return run
    raise ApiError("ARGUMENT", "Unknown command.")


def configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    try:
        args = parser().parse_args(argv)
        if args.timeout <= 0:
            raise ApiError("ARGUMENT", "timeout must be positive.")
        result = execute(args)
        if args.command not in ("serve", "mcp"):
            print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, allow_nan=False))
        return result if args.command in ("serve", "mcp") and isinstance(result, int) else 0
    except ApiError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2 if exc.code == "ARGUMENT" else 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"ok": False, "error": {"code": "CLIENT_ERROR", "message": "The client could not complete the command."}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
