"""Bounded, read-only comment samples under selected posts, not a global index."""
from __future__ import annotations

import json
import re
import urllib.parse

from . import collectors as evidence


_CAPABILITIES = {
    "weibo": {"comments_supported": True, "comment_replies_supported": False,
              "comments_limitations": "读取指定微博的首批评论，单次最多50条；上游未提供评论ID或评论直达链接，不读取完整楼中楼。"},
    "xiaohongshu": {"comments_supported": True, "comment_replies_supported": True,
                   "comments_limitations": "需要带xsec_token的完整笔记链接；读取已渲染评论，可包含有限楼中楼。没有评论ID或直达链接，滚动预算内不保证全部评论。"},
    "bilibili": {"comments_supported": True, "comment_replies_supported": True,
                 "comments_limitations": "读取指定BV视频首批评论，可追加最多3个根评论下的首批回复；原生评论ID保留，不承诺完整遍历。"},
    "zhihu": {"comments_supported": True, "comment_replies_supported": True,
              "comments_limitations": "仅支持回答链接；按条数预算读取评论和有限回复，保留真实ID/父ID。问题或文章链接不适用于此入口。"},
}


def comment_capabilities():
    return json.loads(json.dumps(_CAPABILITIES, ensure_ascii=False))


def _integer(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise evidence.CollectionError(f"{name}应为{low}–{high}之间的整数。", "invalid_comment_options")
    return value


def _options(value):
    if not isinstance(value, dict):
        raise evidence.CollectionError("评论选项必须是对象。", "invalid_comment_options")
    keywords = value.get("keywords", [])
    if not isinstance(keywords, list) or len(keywords) > 20 or any(not isinstance(k, str) or not k.strip() or len(k) > 100 for k in keywords):
        raise evidence.CollectionError("评论关键词应为最多20项、每项1–100字的文字数组。", "invalid_comment_options")
    mode = value.get("match_mode", "any")
    if not isinstance(mode, str) or mode not in {"any", "all"}:
        raise evidence.CollectionError("评论匹配方式应为any或all。", "invalid_comment_options")
    replies = value.get("include_replies", False)
    if not isinstance(replies, bool):
        raise evidence.CollectionError("include_replies必须为布尔值。", "invalid_comment_options")
    return {"keywords": list(dict.fromkeys(k.strip() for k in keywords)), "match_mode": mode,
            "max_posts": _integer(value.get("max_posts", 3), 1, 20, "每轮帖子数"),
            "max_comments_per_post": _integer(value.get("max_comments_per_post", 20), 1, 100, "每帖评论数"), "include_replies": replies}


def _scalar(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()[:300]
    return ""


def _target(item):
    raw = item if isinstance(item, str) else item.get("source_url") if isinstance(item, dict) else None
    try:
        url = evidence._source_url(raw)
    except (ValueError, TypeError):
        raise evidence.CollectionError("评论来源必须是无内嵌凭据的完整帖子链接。", "comments_invalid_target") from None
    parsed = urllib.parse.urlsplit(url)
    host, path = parsed.hostname.lower(), parsed.path
    source = item if isinstance(item, dict) else {}
    title = source.get("parent_title") or source.get("title")
    title_origin = "supplied_parent_title"
    if not isinstance(title, str) or not title.strip():
        body = source.get("content")
        title = body.splitlines()[0] if isinstance(body, str) and body else ""
        title_origin = "parent_post_first_line" if title else "unknown"
    target = {"url": url, "parent_title": title.strip()[:500], "parent_title_source": title_origin}
    if host == "weibo.com" or host.endswith(".weibo.com") or host == "m.weibo.cn":
        match = re.fullmatch(r"/(?:detail|status|statuses)/([A-Za-z0-9]+)/*", path) or re.fullmatch(r"/\d+/([A-Za-z0-9]+)/*", path)
        if match:
            target.update(platform="weibo", id=match[1])
            return target
    elif host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
        match = re.fullmatch(r"/(?:explore|note|search_result|discovery/item)/[A-Fa-f0-9]+/*", path) or re.fullmatch(r"/user/profile/[^/]+/[A-Fa-f0-9]+/*", path)
        if match:
            if not urllib.parse.parse_qs(parsed.query).get("xsec_token", [""])[0].strip():
                raise evidence.CollectionError("小红书评论需要搜索结果中带xsec_token的完整笔记链接，不能仅用笔记ID或主页。", "comments_signed_url_required")
            target.update(platform="xiaohongshu", id=url)
            return target
    elif host == "bilibili.com" or host.endswith(".bilibili.com"):
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]+)/*", path)
        if match:
            target.update(platform="bilibili", id=match[1])
            return target
    elif host in {"zhihu.com", "www.zhihu.com"}:
        if re.fullmatch(r"/(?:question/\d+/)?answer/\d+/*", path):
            target.update(platform="zhihu", id=url)
            return target
        raise evidence.CollectionError("知乎评论入口只支持回答链接，请提供包含/answer/的URL。", "comments_answer_url_required")
    raise evidence.CollectionError("该链接不是已支持的微博、小红书、B站视频或知乎回答来源。", "comments_invalid_target")


def _rows(value):
    value = evidence._envelope_data(value)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise evidence.CollectionError("评论接口返回了非预期记录列表。", "comments_invalid_output")
    return value


def _read(prefix, target, options, config):
    platform, identity = target["platform"], target["id"]
    requested, replies = options["max_comments_per_post"], options["include_replies"]
    parent_cap = min(requested, 50)
    report = {"platform": platform, "parent_source_url": target["url"], "requested_max_comments": requested,
              "include_replies_requested": replies, "partial": True, "pagination": "bounded_sample", "reply_threads_read": 0}
    if platform == "weibo":
        if not identity.isdigit():
            # 'post' is a separately verified READ command returning field/value
            # rows. Resolve canonical numeric idstr rather than guessing BID math.
            fields = _rows(evidence._run_opencli_json(prefix + ["weibo", "post", identity], config))
            observed = {row.get("field"): row.get("value") for row in fields if isinstance(row.get("field"), str)}
            identity = _scalar(observed.get("id"))
            if not re.fullmatch(r"\d{1,30}", identity):
                raise evidence.CollectionError("微博只读详情未返回可用数字帖子ID。", "comments_post_id_missing")
            report["resolved_post_id"] = identity
            if not target["parent_title"] and isinstance(observed.get("text"), str):
                target["parent_title"] = observed["text"][:500]
                target["parent_title_source"] = "read_post_text"
        rows = _rows(evidence._run_opencli_json(prefix + ["weibo", "comments", identity, "--limit", str(parent_cap)], config))
        report.update(parent_limit=parent_cap, pagination="first_comment_batch_no_cursor", replies_supported=False)
    elif platform == "xiaohongshu":
        rows = _rows(evidence._run_opencli_json(prefix + ["xiaohongshu", "comments", identity, "--limit", str(parent_cap), "--with-replies", "true" if replies else "false"], config))
        report.update(parent_limit=parent_cap, pagination="bounded_rendered_dom", replies_supported=True)
    elif platform == "zhihu":
        per_root_replies = 3 if replies and requested > 1 else 0
        root_limit = max(1, requested // (per_root_replies + 1))
        rows = _rows(evidence._run_opencli_json(prefix + ["zhihu", "answer-comments", identity, "--limit", str(root_limit), "--replies-limit", str(per_root_replies)], config))
        report.update(parent_limit=root_limit, replies_per_root=per_root_replies, pagination="bounded_native_pagination", replies_supported=True)
    else:  # bilibili
        root_limit = max(1, min(50, (requested + 1) // 2)) if replies else parent_cap
        rows = _rows(evidence._run_opencli_json(prefix + ["bilibili", "comments", identity, "--limit", str(root_limit)], config))
        report.update(parent_limit=root_limit, pagination="first_page_per_root_or_reply_thread", replies_supported=True)
        if replies:
            roots = [row for row in rows if _scalar(row.get("rpid")).isdigit() and isinstance(row.get("replies"), (int, float)) and row["replies"] > 0][:3]
            for index, root in enumerate(roots):
                remaining = requested - len(rows)
                if remaining <= 0:
                    break
                child_limit = max(1, min(50, (remaining + len(roots) - index - 1) // (len(roots) - index)))
                try:
                    children = _rows(evidence._run_opencli_json(prefix + ["bilibili", "comments", identity, "--parent", _scalar(root["rpid"]), "--limit", str(child_limit)], config))
                except evidence.CollectionError as error:
                    report.setdefault("read_errors", []).append({"code": error.code, "message": str(error)[:300]})
                    if error.code in {"opencli_auth_required", "opencli_browser_unavailable", "opencli_config_error"}:
                        break
                    continue
                for row in children:
                    row = dict(row)
                    row["_read_root_comment_id"] = _scalar(root["rpid"])
                    row["_read_is_reply"] = True
                    rows.append(row)
                report["reply_threads_read"] += 1
    report["upstream_rows_returned"] = len(rows)
    return rows[:requested], report


def _record(row, target, options):
    platform = target["platform"]
    raw = row.get("text") if platform != "zhihu" else row.get("content")
    if not isinstance(raw, str) or not raw.strip():
        return None
    comment_id = _scalar(row.get("rpid") if platform == "bilibili" else row.get("id") if platform == "zhihu" else row.get("comment_id")) or None
    parent_id = _scalar(row.get("parent_id")) if platform == "zhihu" else ""
    published = row.get("created_at") if platform == "zhihu" else row.get("time")
    published = published[:300] if isinstance(published, str) else None
    is_reply = bool(row.get("is_reply") is True or row.get("_read_is_reply") or (isinstance(row.get("depth"), int) and row["depth"] > 0))
    if is_reply and not options["include_replies"]:
        return None
    metadata = {"collector": "opencli", "acquisition": "opencli", "adapter_version": "opencli-2.0.2", "evidence_scope": "comment_sample",
                "parent_source_url": target["url"], "parent_title": target["parent_title"], "parent_title_source": target["parent_title_source"],
                "comment_id": comment_id, "parent_comment_id": parent_id or None, "comment_author_distinct_from_post_author": True,
                "comment_permalink_available": False, "comment_identity_strategy": "platform_comment_id" if comment_id else "observed_content_fingerprint",
                "published_raw": published, "is_reply": is_reply, "reply_to_display": _scalar(row.get("reply_to")),
                "comments_partial": True, "all_comments_read": False, "comment_read_limit": options["max_comments_per_post"]}
    if row.get("_read_root_comment_id"):
        metadata.update(root_comment_id=row["_read_root_comment_id"], parent_relation="root_thread_only_direct_parent_not_exposed")
    if platform == "zhihu" and isinstance(row.get("url"), str) and row["url"]:
        # Native adapter constructs #comment-ID links. Keep its report separate;
        # this client has not independently verified a direct comment permalink.
        metadata["provider_reported_comment_url"] = row["url"][:4000]
    return {"source_url": target["url"], "platform": platform, "kind": "comment", "content": raw,
            "author_handle": _scalar(row.get("author")) or None, "posted_at": published, "is_partial": True,
            "comment_id": comment_id, "parent_comment_id": parent_id or None, "parent_source_url": target["url"],
            "parent_title": target["parent_title"], "metadata": metadata}


def _matches(content, keywords, mode):
    if not keywords:
        return True
    checks = [keyword.casefold() in content.casefold() for keyword in keywords]
    return all(checks) if mode == "all" else any(checks)


def collect_comments(post_urls, platforms, limit, config, options=None):
    """Yield canonical comments and return an explicit bounded-read summary."""
    options = _options({} if options is None else options)
    budget = _integer(limit, 1, 1000, "本轮评论候选数")
    if not isinstance(post_urls, list) or not post_urls or len(post_urls) > 200:
        raise evidence.CollectionError("请提供1–200个待查看评论的来源帖子链接。", "comments_invalid_target")
    if platforms is None or platforms == []:
        platforms = list(_CAPABILITIES)
    if not isinstance(platforms, list) or any(not isinstance(p, str) or p not in _CAPABILITIES for p in platforms):
        raise evidence.CollectionError("当前仅接入微博、小红书、B站、知乎回答的只读评论；其他平台可导入评论原文。", "comments_unsupported_platform")
    if isinstance(config, dict) and config.get("action") not in (None, "search", "comments"):
        raise evidence.CollectionError("评论采集只允许已登记的读取命令。", "unsafe_action")
    prefix = evidence._opencli_prefix(config)
    summary = {"scope": "selected_post_comment_sample", "global_comment_index": False, "posts_supplied": len(post_urls), "posts_attempted": 0,
               "posts_succeeded": 0, "comments_read": 0, "comments_matched": 0, "comments_returned": 0, "post_results": [],
               "max_posts": options["max_posts"], "max_comments_per_post": options["max_comments_per_post"], "keywords": options["keywords"],
               "match_mode": options["match_mode"], "include_replies": options["include_replies"]}
    errors, seen, seen_posts, blocked = [], set(), set(), set()
    for item in post_urls:
        if summary["posts_attempted"] >= options["max_posts"] or summary["comments_returned"] >= budget:
            break
        try:
            target = _target(item)
            if target["platform"] not in platforms or target["platform"] in blocked or target["url"] in seen_posts:
                continue
            seen_posts.add(target["url"])
            summary["posts_attempted"] += 1
            rows, receipt = _read(prefix, target, options, config)
            records, warnings = evidence.normalize_records([r for row in rows if (r := _record(row, target, options)) is not None])
            summary["posts_succeeded"] += 1
            summary["comments_read"] += len(rows)
            receipt.update(comments_read=len(rows), comments_matched=0, comments_returned=0, normalization_warnings=warnings[:10])
            summary["post_results"].append(receipt)
            for problem in receipt.get("read_errors", []):
                errors.append(evidence.CollectionError(problem["message"], problem["code"]))
                if problem["code"] == "opencli_auth_required":
                    blocked.add(target["platform"])
            matched = [record for record in records if _matches(record["content"], options["keywords"], options["match_mode"])]
            receipt["comments_matched"] = len(matched)
            summary["comments_matched"] += len(matched)
            for record in matched:
                identity = evidence.record_identity(record)
                if identity in seen:
                    continue
                seen.add(identity)
                record["metadata"].update(keyword_match_mode=options["match_mode"], matched_keywords=[k for k in options["keywords"] if k.casefold() in record["content"].casefold()])
                if receipt.get("read_errors"):
                    record["metadata"]["comment_read_errors"] = receipt["read_errors"]
                summary["comments_returned"] += 1
                receipt["comments_returned"] += 1
                yield record
                if summary["comments_returned"] >= budget:
                    break
            if any(problem["code"] in {"opencli_browser_unavailable", "opencli_config_error"} for problem in receipt.get("read_errors", [])):
                break
        except evidence.CollectionError as error:
            errors.append(error)
            summary["post_results"].append({"status": "failed", "code": error.code, "message": str(error)[:300]})
            if error.code in {"opencli_browser_unavailable", "opencli_config_error"}:
                break
            if error.code == "opencli_auth_required" and "target" in locals():
                blocked.add(target["platform"])
    summary["posts_not_read"] = max(0, len(post_urls) - summary["posts_attempted"])
    if errors:
        error = errors[0] if len(errors) == 1 else evidence.CollectionError("部分帖子评论读取失败：" + "；".join(str(e)[:180] for e in errors[:5]), "comments_partial_failure")
        error.collection_summary = summary
        raise error
    if summary["posts_attempted"] == 0:
        error = evidence.CollectionError("没有与所选评论平台匹配的帖子链接，未执行评论读取。", "comments_no_matching_posts")
        error.collection_summary = summary
        raise error
    return summary
