"""Pure input parsing and payload validation for the native desktop forms.

No Qt imports and no business writes: the authenticated HTTP API remains the
authority for normalization, evidence references and optimistic review versions.
"""
from __future__ import annotations

import csv
import io
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .processing import normalize_collection, normalize_analysis_mode
from .targeting import normalize_targeting, build_queries

MAX_INPUT_BYTES = 3_500_000
SOURCES = {"demo", "import", "urls", "opencli", "comments"}
DECISIONS = {"keep", "exclude", "needs_info", "watch"}


class FormError(ValueError):
    pass


def split_terms(value):
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,，;；\r\n]+", str(value or ""))
    return list(dict.fromkeys(str(item).strip().lstrip("#") for item in values if str(item).strip().lstrip("#")))


def split_lines(value):
    return list(dict.fromkeys(line.strip() for line in str(value or "").splitlines() if line.strip()))


def valid_http_url(value):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
                    and not any(ord(char) < 32 for char in value))
    except (ValueError, TypeError):
        return False


def parse_records(text):
    """Read UTF-8 text as JSON, JSONL, CSV or TSV without evaluating content."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise FormError("导入内容超过 3.5 MB，请拆成多个批次。")
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise FormError("请选择文件或粘贴带来源链接和原文的记录。")
    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            try:
                data = [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError:
                raise FormError(f"JSON / JSONL 格式不正确：第 {exc.lineno} 行，{exc.msg}。") from None
        if isinstance(data, dict):
            if isinstance(data.get("records"), list):
                data = data["records"]
            elif "source_url" in data or "content" in data:
                data = [data]
            else:
                raise FormError("JSON 应为记录数组、含 records 数组的对象，或单条来源记录。")
    else:
        aliases = {"来源网址": "source_url", "来源链接": "source_url", "网址": "source_url", "链接": "source_url", "url": "source_url",
                   "原文": "content", "内容": "content", "平台": "platform", "品牌": "brand_name", "品牌名称": "brand_name",
                   "发布日期": "posted_at", "官网": "company_website", "国家": "country", "作者": "author_handle",
                   "记录类型": "kind", "评论ID": "comment_id", "父帖链接": "parent_source_url"}
        try:
            delimiter = "\t" if "\t" in text.splitlines()[0] else ","
            reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            headers = reader.fieldnames
            if not headers or not any(h.strip() for h in headers):
                raise FormError("CSV 需要表头，例如 source_url,content,platform。")
            normalized_headers = [aliases.get(key.strip(), key.strip()) for key in headers]
            if len(set(normalized_headers)) != len(normalized_headers):
                raise FormError("CSV 表头重复，请为每一列使用不同名称。")
            data = []
            for row in reader:
                if None in row:
                    raise FormError("CSV 某行的列数超过表头，请检查引号与分隔符。")
                if any(str(v or "").strip() for v in row.values()):
                    data.append({aliases.get(k.strip(), k.strip()): v or "" for k, v in row.items()})
        except csv.Error as exc:
            raise FormError(f"CSV 格式不正确：{exc}。") from None
    if not isinstance(data, list) or not data:
        raise FormError("文件中没有可导入的记录。")
    if len(data) > 1000:
        raise FormError("每批最多导入 1000 条，请拆成多个批次。")
    if any(not isinstance(record, dict) for record in data):
        raise FormError("每条记录必须是对象；原文和来源链接由校验接口继续检查。")
    return data


def targeting_payload(values):
    fields = values.get("custom_fields", {})
    if isinstance(fields, list):
        custom = {}
        for row in fields:
            key, value = str(row.get("key", "")).strip(), str(row.get("value", "")).strip()
            if not key and not value:
                continue
            if not key or not value:
                raise FormError("补充条件需要同时填写名称和内容，或移除空行。")
            if key in custom:
                raise FormError(f"补充条件“{key}”重复，请合并后再创建。")
            custom[key] = value
        fields = custom
    return normalize_targeting({"market": values.get("market", "cn"), "industry": str(values.get("industry", "")).strip(),
        "customer_type": values.get("customer_type", "consumer"), "regions": split_terms(values.get("regions")),
        "include_tags": split_terms(values.get("include_tags")), "exclude_tags": split_terms(values.get("exclude_tags")),
        "needs": str(values.get("needs", "")).strip(), "custom_fields": fields})


def collection_payload(values, source):
    scope = "comments" if source == "comments" else values.get("scope", "posts")
    return normalize_collection({"scope": scope, "keywords": split_terms(values.get("keywords")),
        "match_mode": values.get("match_mode", "any"), "max_posts": values.get("max_posts", 3),
        "max_comments_per_post": values.get("max_comments_per_post", 20), "include_replies": values.get("include_replies", False)}, source)


def preview_payload(values):
    return {"targeting": targeting_payload(values), "queries": split_lines(values.get("queries")),
            "platforms": list(values.get("platforms", []))}


def run_payload(values, catalog, maximum=50, validated_records=None):
    source = values.get("source", "opencli")
    if source not in SOURCES:
        raise FormError("请选择有效的资料来源。")
    name = str(values.get("name", "")).strip()
    if not name or len(name) > 160:
        raise FormError("请填写任务名称，最多 160 字。")
    limit = values.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise FormError(f"本轮最多条数应为 1–{maximum} 的整数。")
    result = {"name": name, "source": source, "goal": str(values.get("goal", "")).strip(), "limit": limit,
              "queries": [], "platforms": [], "urls": [], "records": []}
    if len(result["goal"]) > 8000:
        raise FormError("本轮目标最多 8000 字。")
    if source != "demo":
        result["targeting"] = target = targeting_payload(values)
        result["analysis_mode"] = normalize_analysis_mode(values.get("analysis_mode", "collect_only"))
        if not result["goal"]:
            result["goal"] = " · ".join(x for x in ("国内定向获客" if target["market"] == "cn" else "海外商机", target["industry"], "、".join(target["regions"]), target["needs"]) if x)
    if source in {"comments", "opencli"}:
        result["collection"] = collection = collection_payload(values, source)
        if collection["scope"] == "posts_and_comments" and limit < 2:
            raise FormError("同时保留帖子和评论至少需要 2 条结果预算。")
    if source == "import":
        records = validated_records if validated_records is not None else parse_records(values.get("records_text", ""))
        if not isinstance(records, list) or not records:
            raise FormError("没有通过校验的原文记录，请检查后重试。")
        result["records"] = records
    if source in {"urls", "comments"}:
        urls = split_lines(values.get("urls", ""))
        if not urls or len(urls) > 200:
            raise FormError("请填写 1–200 个来源链接，每行一个。")
        if any(not valid_http_url(url) for url in urls):
            raise FormError("来源链接必须是完整且不含账号密码的 http:// 或 https:// 地址。")
        result["urls"] = urls
    if source == "opencli":
        by_id = {platform["id"]: platform for platform in catalog}
        selected = list(dict.fromkeys(values.get("platforms", [])))
        if not selected:
            raise FormError("请至少选择一个支持当前范围的平台。")
        for platform in selected:
            item = by_id.get(platform)
            if not item or not item.get("search_supported"):
                raise FormError(f"平台 {platform} 尚未接入只读搜索，请改用公开链接或导入。")
            if result["collection"]["scope"] != "posts" and not item.get("comments_supported"):
                raise FormError(f"{item.get('label', platform)} 尚未接入评论读取，请调整平台或采集范围。")
        queries = split_lines(values.get("queries"))
        if not queries:
            raise FormError("请先点击“生成搜索方案”，或每行填写一个搜索词。")
        result["queries"] = build_queries(result["targeting"], queries)
        result["platforms"] = selected
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 4 * 1024 * 1024:
        raise FormError("本批请求超过 4 MB，请减少记录或原文长度。")
    return result


def review_payload(data, run_id):
    """Normalize export wrappers without manufacturing versions or citations."""
    if not isinstance(data, (dict, list)):
        raise FormError("审核 JSON 应为对象或变更数组。")
    outer = data if isinstance(data, dict) else {}
    raw = outer.get("review", outer.get("proposed_review", outer.get("review_template", data)))
    changes = raw if isinstance(raw, list) else raw.get("changes") if isinstance(raw, dict) else None
    if not isinstance(changes, list) or not changes:
        raise FormError("审核 JSON 需要非空 changes 数组。")
    if len(changes) > 1000:
        raise FormError("每批最多导入 1000 条审核意见。")
    proposal = raw if isinstance(raw, dict) else {}
    run_wrapper = outer.get("run") if isinstance(outer.get("run"), dict) else {}
    supplied_run = proposal.get("run_id") or outer.get("run_id") or run_wrapper.get("id")
    if supplied_run and supplied_run != run_id:
        raise FormError("审核文件属于其他批次，请切换到对应批次后导入。")
    if not run_id:
        raise FormError("请先选择要审核的批次。")
    clean, seen = [], set()
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict):
            raise FormError(f"第 {index} 条审核意见应为对象。")
        cid, version = change.get("candidate_id"), change.get("base_version")
        if not isinstance(cid, str) or not cid.strip() or isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise FormError(f"第 {index} 条缺少 candidate_id 或有效 base_version，不能自动补造版本。")
        if cid in seen:
            raise FormError(f"候选 {cid} 在审核文件中重复，请合并后重试。")
        seen.add(cid)
        if change.get("decision") not in DECISIONS:
            raise FormError(f"第 {index} 条 decision 应为 keep / exclude / needs_info / watch。")
        reason = change.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise FormError(f"第 {index} 条请填写 reason 审核依据。")
        arrays = {}
        for field in ("evidence_ids", "tags"):
            value = change.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise FormError(f"第 {index} 条 {field} 应为文字数组。")
            arrays[field] = value
        clean.append({"candidate_id": cid, "base_version": version, "decision": change["decision"], "reason": reason, **arrays})
    operation = proposal.get("operation_id") or outer.get("operation_id") or str(uuid4())
    if not isinstance(operation, str) or len(operation) > 200 or any(ord(char) < 32 for char in operation):
        raise FormError("operation_id 应为不超过 200 字的有效文字；同一请求重试应保留它。")
    return {"run_id": run_id, "reviewer": "codex", "operation_id": operation, "changes": clean}


def settings_payload(values):
    base = str(values.get("base_url", "")).strip()
    if not valid_http_url(base):
        raise FormError("请输入完整的本地模型 http:// 或 https:// 服务地址。")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise FormError("本程序的模型设置只连接本地服务，请使用 localhost 或 127.0.0.1 地址。")
    model_name = str(values.get("model", "")).strip()
    if not model_name or len(model_name) > 200:
        raise FormError("请填写本地服务实际提供的模型名称或 alias，最多 200 字。")
    protocol = values.get("protocol", "openai")
    if protocol not in {"openai", "ollama"}:
        raise FormError("请选择 OpenAI 兼容或 Ollama 协议。")
    model = {"base_url": base, "model": model_name, "protocol": protocol, "enabled": bool(values.get("enabled", False)),
             "timeout_seconds": values.get("timeout_seconds", 180), "max_tokens": values.get("max_tokens", 2048),
             "temperature": values.get("temperature", .1), "concurrency": 1}
    for key, low, high in (("timeout_seconds", 5, 600), ("max_tokens", 128, 16384), ("temperature", 0, 1)):
        value = model[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise FormError(f"{key} 应在 {low}–{high} 之间。")
    if values.get("clear_key"):
        model["api_key"] = ""
    elif str(values.get("api_key", "")).strip():
        model["api_key"] = str(values["api_key"]).strip()
    extra_text = str(values.get("extra_body", "{}")).strip()
    try:
        extra = json.loads(extra_text or "{}")
    except json.JSONDecodeError:
        raise FormError("附加模型参数应为 JSON 对象。") from None
    if not isinstance(extra, dict) or {"model", "messages", "stream", "temperature", "max_tokens", "response_format"}.intersection(extra) or len(json.dumps(extra)) > 10000:
        raise FormError("附加模型参数必须为对象，且不能覆盖 model/messages/stream/temperature/max_tokens/response_format。")
    model["extra_body"] = extra
    timeout = values.get("opencli_timeout", 90)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= timeout <= 600:
        raise FormError("OpenCLI 超时应为 5–600 秒。")
    return {"model": model, "opencli": {"executable": str(values.get("executable", "opencli")).strip() or "opencli",
            "node_entry": str(values.get("node_entry", "")).strip(), "profile": str(values.get("profile", "")).strip(), "timeout_seconds": timeout}}
