"""Read-only evidence collection. No account login, messages, or shell evaluation."""
from __future__ import annotations

import html
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
MAX_CONTENT = 18000

# Capabilities verified against the installed OpenCLI 2.0.2 command sources and
# machine-readable help, not against an authenticated social account.
_PLATFORMS = (
    {"id": "douyin", "label": "抖音", "market": "cn", "search_supported": True, "evidence_kind": "video_card", "search_limit": 30,
     "limitations": "读取搜索视频卡片，需要已登录的浏览器；视频画面、音频和评论正文未采集。", "search_url_template": "https://www.douyin.com/search/{query}?type=video"},
    {"id": "weibo", "label": "微博", "market": "cn", "search_supported": True, "evidence_kind": "post_snippet", "search_limit": 50,
     "limitations": "读取搜索页显示的微博内容，可能折叠；发布时间保留页面原文，不自动换算相对日期。", "search_url_template": "https://s.weibo.com/weibo?q={query}"},
    {"id": "xiaohongshu", "label": "小红书", "market": "cn", "search_supported": True, "evidence_kind": "note_title", "search_limit": 50,
     "limitations": "搜索仅返回笔记标题、作者与链接，尚无笔记全文或评论；上游由笔记ID推算的日期不视为已核实发布日期。"},
    {"id": "bilibili", "label": "B站", "market": "cn", "search_supported": True, "evidence_kind": "video_snippet", "search_limit": 50,
     "limitations": "读取视频搜索标题和简介，未读取视频内容或评论；默认只搜索视频。"},
    {"id": "zhihu", "label": "知乎", "market": "cn", "search_supported": True, "evidence_kind": "result_title", "search_limit": 50,
     "limitations": "搜索仅返回问题、回答或文章标题与链接，未读取回答正文。"},
    {"id": "tieba", "label": "贴吧", "market": "cn", "search_supported": True, "evidence_kind": "thread_snippet", "search_limit": 20,
     "limitations": "读取第一页帖子标题和至多200字摘要，未读取完整帖子与回复。", "search_url_template": "https://tieba.baidu.com/f/search/res?qw={query}&ie=utf-8&pn=1"},
    {"id": "kuaishou", "label": "快手", "market": "cn", "search_supported": True, "experimental": True, "evidence_kind": "experimental_video_card", "search_limit": 20,
     "limitations": "实验性浏览器DOM搜索，尚未完成真实登录账号验收；仅读取卡片文字和实际链接，页面结构变化或登录验证会明确失败。支持公开链接与原文导入。", "home_url": "https://www.kuaishou.com/", "search_url_template": "https://www.kuaishou.com/search/video?searchKey={query}"},
    {"id": "tiktok", "label": "TikTok", "market": "global", "search_supported": True, "evidence_kind": "video_snippet", "search_limit": 50,
     "limitations": "搜索描述被上游截为100字符；未采集视频画面、音频或评论正文。"},
    {"id": "instagram", "label": "Instagram", "market": "global", "search_supported": True, "evidence_kind": "profile_only", "search_limit": 50,
     "limitations": "搜索返回账号资料而非采购帖子；账号存在不能证明有采购需求。"},
)
_SEARCH_PLATFORMS = {p["id"]: p for p in _PLATFORMS if p["search_supported"]}
_LEGACY_READ_ACTIONS = {"tiktok": {"user", "profile"}, "instagram": {"user", "profile"}}


def platform_catalog():
    """Return a detached, truthful capability catalog for UI/API consumers."""
    from .comments import comment_capabilities
    comments = comment_capabilities()
    result = json.loads(json.dumps(_PLATFORMS, ensure_ascii=False))
    for platform in result:
        platform.update(comments.get(platform["id"], {"comments_supported": False, "comments_limitations": "尚未接入可核实的只读评论获取；可导入已取得的评论原文。", "comment_replies_supported": False}))
    return result


class CollectionError(RuntimeError):
    def __init__(self, message, code="collection_error"):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "iframe"}:
            self.skip += 1
        if not self.skip and tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "iframe"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(value):
    parser = _Text()
    parser.feed(str(value))
    return "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())


def _source_url(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("source_url must be a URL")
    p = urllib.parse.urlsplit(value.strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("仅接受无凭证的http/https来源URL")
    _ = p.port
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def record_identity(record):
    """Stable URL identity for posts; per-comment identity within a parent post.

    The observation hash is a deduplication key, never a fabricated platform
    comment ID or permalink. Retrieval time/rank are deliberately excluded.
    """
    url = _source_url(record.get("source_url") or record.get("url"))
    parsed = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]
    canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), urllib.parse.urlencode(query), ""))
    if record.get("kind") == "comment":
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        comment_id = record.get("comment_id") or metadata.get("comment_id")
        if isinstance(comment_id, (str, int)) and not isinstance(comment_id, bool) and str(comment_id).strip():
            identity = ["comment", canonical, "platform_id", str(comment_id).strip()]
        else:
            identity = ["comment", canonical, "observed_fingerprint", record.get("author_handle") or record.get("author"), record.get("posted_at"),
                        record.get("parent_comment_id") or metadata.get("parent_comment_id"), record.get("content") or record.get("text")]
    else:
        identity = ["post", canonical]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def normalize_records(records):
    if not isinstance(records, list):
        raise CollectionError("records必须是数组", "invalid_import")
    normalized, warnings, seen = [], [], set()
    for index, item in enumerate(records[:5000]):
        if not isinstance(item, dict):
            warnings.append(f"第{index + 1}条不是对象，已跳过")
            continue
        try:
            url = _source_url(item.get("source_url") or item.get("url") or item.get("video_url") or item.get("profile_url") or item.get("link") or item.get("permalink"))
            kind = item.get("kind", "import")
            kind = kind if kind in {"post", "website", "comment", "import"} else "import"
            raw = item.get("content") or item.get("text") or item.get("full_text") or item.get("body") or item.get("description") or item.get("caption") or item.get("desc") or item.get("bio") or ""
            if not isinstance(raw, str):
                raise ValueError("content必须是字符串")
            title = item.get("title")
            if kind != "comment" and isinstance(title, str) and title.strip() and title not in raw:
                raw = title.strip() + "\n" + raw
            text = html_text(raw) if re.search(r"<\s*(?:html|body|script|style|p|div|h[1-6])\b", raw, re.I) else html.unescape(raw).strip()
            if not text:
                raise ValueError("缺少可读取正文，不以元数据伪造需求")
            partial = bool(item.get("is_partial")) or len(text) > MAX_CONTENT
            if len(text) > MAX_CONTENT:
                warnings.append(f"第{index + 1}条正文截至{MAX_CONTENT}字符，标记partial")
            record = {"source_url": url, "platform": str(item.get("platform") or "import")[:80], "kind": kind, "content": text[:MAX_CONTENT], "is_partial": partial, "is_demo": item.get("is_demo") is True, "retrieved_at": str(item.get("retrieved_at") or _now())}
            for key in ("brand_name", "author_handle", "posted_at", "company_website", "country"):
                value = item.get(key)
                record[key] = str(value)[:1000] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None
            metadata = item.get("metadata", {})
            record["metadata"] = dict(metadata) if isinstance(metadata, dict) and len(json.dumps(metadata, ensure_ascii=False)) < 20000 else {}
            if kind == "comment":
                for key in ("comment_id", "parent_comment_id", "parent_source_url", "parent_title"):
                    value = item.get(key, record["metadata"].get(key))
                    if value is None or isinstance(value, (str, int)) and not isinstance(value, bool):
                        record[key] = value
                        record["metadata"][key] = value
                if not record.get("author_handle") and isinstance(item.get("author"), str):
                    record["author_handle"] = item["author"][:1000]
                record["metadata"].setdefault("comment_identity_strategy", "platform_comment_id" if record.get("comment_id") else "observed_content_fingerprint")
            identity = record_identity(record)
            if identity in seen:
                warnings.append(f"第{index + 1}条来源或评论标识重复，已去重")
                continue
            normalized.append(record)
            seen.add(identity)
        except (ValueError, TypeError, OverflowError) as e:
            warnings.append(f"第{index + 1}条无效，已跳过：{e}")
    if len(records) > 5000:
        warnings.append("超过5000条导入上限，余下记录未读取")
    return normalized, warnings


def validate_public_url(url):
    try:
        clean = _source_url(url)
        p = urllib.parse.urlsplit(clean)
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or "." not in host and ":" not in host:
            raise ValueError("禁止本地地址")
        addresses = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0].split("%")[0]).is_global for entry in addresses):
            raise ValueError("禁止私网、loopback或非公网地址")
        return clean
    except (ValueError, OSError) as e:
        raise CollectionError(f"URL读取被拒绝：{e}", "unsafe_url") from None


def _public_socket(host, port, timeout):
    # Resolve at connection time, validate ALL results, connect to an already
    # validated numeric sockaddr. No second DNS lookup / rebinding window.
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0].split("%")[0]).is_global for a in addresses):
        raise CollectionError("连接时DNS解析到非公网地址，已拒绝", "unsafe_url")
    last_error = None
    for family, socktype, proto, _, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            last_error = e
            sock.close()
    raise last_error or OSError("No address available")


class _PublicHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = _public_socket(self.host, self.port, self.timeout)


class _PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = _public_socket(self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTP, req)


def _bundled_ca_path():
    app_root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return app_root / "certs" / "cacert.pem"


def _tls_context():
    # Keep system trust, certificate verification and hostname validation; add
    # the app's separately licensed Mozilla roots when bundled with the app.
    context = ssl.create_default_context()
    bundle = _bundled_ca_path()
    if bundle.is_file():
        context.load_verify_locations(cafile=str(bundle))
    return context


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPS, req, context=_tls_context())


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def collect_urls(urls, limit):
    if not isinstance(urls, list):
        raise CollectionError("urls必须是数组", "invalid_urls")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _Redirect())
    for url in urls[:max(1, min(int(limit), 50))]:
        clean = validate_public_url(url)
        try:
            req = urllib.request.Request(clean, headers={"User-Agent": "LeadWorkbench/0.1 evidence-reader", "Accept": "text/html,text/plain,application/json", "Accept-Encoding": "identity"})
            with opener.open(req, timeout=30) as response:
                final = validate_public_url(response.geturl())
                mime = response.headers.get_content_type()
                if mime not in {"text/html", "text/plain", "application/xhtml+xml", "application/json"}:
                    raise CollectionError(f"不支持的网页类型：{mime}", "unsupported_content")
                if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                    raise CollectionError("网页忽略identity请求返回压缩内容；未将压缩字节当正文分析", "unsupported_encoding")
                raw = response.read(MAX_BYTES + 1)
                charset = response.headers.get_content_charset() or "utf-8"
            partial = len(raw) > MAX_BYTES
            text = raw[:MAX_BYTES].decode(charset, errors="replace")
            if mime in {"text/html", "application/xhtml+xml"}:
                text = html_text(text)
            items, warnings = normalize_records([{"source_url": final, "platform": "web", "kind": "website", "content": text, "is_partial": partial, "metadata": {"requested_url": clean, "content_type": mime, "acquisition": "url"}}])
            if not items:
                raise CollectionError("网页没有可读取正文", "empty_content")
            if warnings:
                items[0]["metadata"]["warnings"] = warnings
            yield items[0]
        except CollectionError:
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise CollectionError("HTTPS证书验证失败；请检查系统信任链、网络代理证书或目标站点证书。未绕过证书校验。", "certificate_error") from None
            raise CollectionError(f"网页读取失败：{type(e.reason).__name__}", "url_read_failed") from None
        except ssl.SSLCertVerificationError:
            raise CollectionError("HTTPS证书验证失败；请检查系统信任链或站点证书。未绕过证书校验。", "certificate_error") from None
        except (OSError, ValueError, LookupError) as e:
            raise CollectionError(f"网页读取失败：{type(e).__name__}", "url_read_failed") from None


def _opencli_rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if value.get("error") or value.get("success") is False or value.get("ok") is False:
            raise CollectionError("OpenCLI返回失败，未生成来源证据", "opencli_error")
        for key in ("data", "items", "results", "posts", "records"):
            if key in value:
                return _opencli_rows(value[key])
        if any(key in value for key in ("source_url", "url", "video_url", "profile_url", "link", "permalink", "username")):
            return [value]
    raise CollectionError("OpenCLI输出不是支持的JSON记录列表", "opencli_invalid_output")


def _opencli_record(item, platform, action, query):
    """Map documented output fields; profile data is never called a demand post."""
    mapped = dict(item)
    username = item.get("username") or item.get("author_handle") or item.get("author")
    if isinstance(username, str):
        mapped.setdefault("author_handle", username)
    has_url = any(mapped.get(k) for k in ("source_url", "url", "video_url", "profile_url", "link", "permalink"))
    profile_only = action == "profile" or (platform == "instagram" and action == "search")
    if profile_only and not has_url and isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,50}", username.lstrip("@")):
        handle = username.lstrip("@")
        mapped["source_url"] = f"https://www.instagram.com/{handle}/" if platform == "instagram" else f"https://www.tiktok.com/@{handle}"
    if profile_only:
        # Instagram search has only identity/profile fields. Keep those literal
        # fields as evidence, with no fabricated caption or procurement request.
        parts = []
        for key in ("username", "name", "bio"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}: {value.strip()}")
        if parts:
            mapped["content"] = "\n".join(parts)
    elif platform in {"weibo", "xiaohongshu", "zhihu"}:
        # XHS/Zhihu expose only titles. Weibo's 'title' is rendered post text,
        # not a separate authored headline. Do not fabricate missing bodies.
        mapped["content"] = item.get("title") if isinstance(item.get("title"), str) else ""
    elif platform in {"bilibili", "tieba"}:
        fields = ("title", "desc") if platform == "bilibili" else ("title", "snippet")
        parts = []
        for key in fields:
            value = item.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in parts:
                parts.append(value.strip())
        mapped["content"] = "\n".join(parts)
    mapped.update(platform=platform, kind="website" if profile_only else "post")
    # Current upstream search/profile implementations return abbreviated fields
    # (TikTok desc:100 chars; Instagram profile bio:120), not full post evidence.
    mapped["is_partial"] = bool(mapped.get("is_partial")) or action in {"search", "profile"}
    scope = "profile_only" if profile_only else _SEARCH_PLATFORMS.get(platform, {}).get("evidence_kind", "post_snippet")
    mapped["metadata"] = {"query": query, "action": action, "collector": "opencli", "acquisition": "opencli", "evidence_scope": scope, "upstream_may_truncate": True, "adapter_version": "opencli-2.0.2"}
    if platform in {"weibo", "tieba"} and isinstance(item.get("time"), str) and item["time"].strip():
        # Keep relative/ambiguous visible times literally. Never fill a year.
        mapped["posted_at"] = item["time"].strip()[:200]
        mapped["metadata"]["date_source"] = "visible_time_text"
    elif platform == "bilibili":
        stamp = item.get("pubdate_ts")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 < stamp < 32503680000:
            try:
                mapped["posted_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                mapped["metadata"].update(date_source="upstream_pubdate_ts", pubdate_ts=stamp)
            except (ValueError, OSError, OverflowError):
                pass
    elif platform == "xiaohongshu":
        # Upstream noteIdToDate computes this from the note ID. It is neither
        # a displayed date nor an independent check, so keep it out of facts.
        mapped.pop("posted_at", None)
        if isinstance(item.get("published_at"), str):
            mapped["metadata"]["upstream_inferred_published_at"] = item["published_at"][:200]
            mapped["metadata"]["date_source"] = "note_id_inference_not_verified"
    for key in ("type", "forum"):
        if isinstance(item.get(key), str):
            mapped["metadata"]["upstream_" + key] = item[key][:1000]
    return mapped


def _opencli_exit_error(returncode, raw):
    # Show actionable classes rather than arbitrary upstream text, which can
    # include URLs, signed tokens, browser state or excerpts from a challenge.
    code = ""
    try:
        value = json.loads(raw.decode("utf-8-sig"))
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            code = str(value["error"].get("code", ""))
    except (ValueError, UnicodeError):
        pass
    if code in {"AUTH_REQUIRED", "LOGIN_WALL"} or returncode == 77:
        return CollectionError("平台登录失效或出现登录/验证页面；请在已授权浏览器完成登录与验证后重试。未生成线索。", "opencli_auth_required")
    if code == "ADAPTER_LOAD":
        return CollectionError("该平台的OpenCLI适配器未能加载；请检查已安装版本。", "opencli_adapter_unavailable")
    if code == "BROWSER_CONNECT" or returncode == 69:
        return CollectionError("OpenCLI浏览器连接不可用；请检查Browser Bridge扩展、daemon及所选Profile。未生成线索。", "opencli_browser_unavailable")
    if code in {"TIMEOUT", "SESSION_BUSY"} or returncode == 75:
        return CollectionError("OpenCLI请求超时或浏览器会话忙；请稍后重试，不会自动重复请求。", "opencli_timeout")
    if code in {"EMPTY_RESULT", "NOT_FOUND"} or returncode == 66:
        return CollectionError("平台搜索未返回可读取结果；请核对搜索词、登录和页面状态，不能据此认定没有需求。", "opencli_empty_result")
    if code == "CONFIG" or returncode == 78:
        return CollectionError("OpenCLI配置无效；请检查命令入口及Profile设置。", "opencli_config_error")
    return CollectionError(f"OpenCLI退出码{returncode}，请检查官方命令和连接状态。", "opencli_exit_error")


def _run_opencli_json(argv, config, timeout_cap=180):
    """One native process with bounded, non-shell JSON transport."""
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                process.wait(timeout=max(1, min(float(config.get("timeout_seconds", 90)), timeout_cap)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise CollectionError("OpenCLI读取超时；未生成模拟结果", "opencli_timeout") from None
            stdout.seek(0)
            raw = stdout.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CollectionError("OpenCLI输出超过4MB；请降低每批条数，已有结果保留。", "opencli_output_too_large")
        if process.returncode:
            raise _opencli_exit_error(process.returncode, raw)
        value = json.loads(raw.decode("utf-8-sig"))
        if isinstance(value, dict) and (value.get("ok") is False or value.get("error")):
            raise _opencli_exit_error(1, raw)
        return value
    except CollectionError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise CollectionError(f"OpenCLI调用或JSON解析失败：{type(error).__name__}", "opencli_invalid_output") from None


# The card selectors/search route are informed by MIT-licensed
# anweat/dsh-web-search-pro/src/platform-search.ts. See licenses and research
# record. This reader adds owned-tab targeting, bounded readiness and errors.
# User/model input never becomes executable JavaScript.
_KUAISHOU_DOM_JS = r"""(async () => {
  const limit = __LIMIT__;
  const deadline = Date.now() + 12000;
  const visible = (el) => {
    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  while (Date.now() < deadline) {
    if (!/(^|\.)kuaishou\.com$/i.test(location.hostname) || !location.pathname.startsWith('/search')) {
      return {state: 'wrong_page'};
    }
    const pageText = document.body?.innerText || '';
    if (/验证码|安全验证|完成验证|验证后|访问过于频繁|操作频繁|captcha|security check/i.test(pageText)) {
      return {state: 'challenge'};
    }
    const cards = Array.from(document.querySelectorAll('.video-card')).filter(visible);
    const rows = [], seen = new Set();
    for (const card of cards) {
      const title = (card.querySelector('.video-info-title')?.innerText || '').trim();
      const content = (card.innerText || '').trim().slice(0, 3000);
      const links = Array.from(card.querySelectorAll('a[href]')).map(a => a.href);
      const url = links.find(href => {
        try {
          const parsed = new URL(href);
          return parsed.protocol === 'https:' && /(^|\.)kuaishou\.com$/i.test(parsed.hostname)
            && /^\/(?:short-video|video)\/[^/?#]+/.test(parsed.pathname);
        } catch { return false; }
      });
      if (!url || !content || seen.has(url)) continue;
      seen.add(url);
      rows.push({title, content, url, is_partial: true});
      if (rows.length >= limit) break;
    }
    if (rows.length) return {state: 'ok', rows};
    if (/登录后|请先登录|登录后查看更多|扫码登录/.test(pageText)) return {state: 'login_required'};
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  return {state: 'no_readable_cards'};
})()"""


def _envelope_data(value):
    return value.get("data") if isinstance(value, dict) and "data" in value else value


def _collect_kuaishou_rows(prefix, query, limit, config):
    session = "leadworkbench-ks-" + uuid.uuid4().hex
    base = prefix + ["browser", "--session", session, "--window", "background"]
    page_id, rows, cleanup_warning = None, [], None
    url = "https://www.kuaishou.com/search/video?searchKey=" + urllib.parse.quote(query, safe="")
    try:
        # 'browser open' auto-enables network capture in this OpenCLI version.
        # 'tab new' avoids that and guarantees a newly created, owned target.
        opened = _envelope_data(_run_opencli_json(base + ["tab", "new", url], config))
        if not isinstance(opened, dict) or not isinstance(opened.get("page"), (str, int)) or isinstance(opened.get("page"), bool):
            raise CollectionError("快手实验搜索未取得新建标签的标识；未读取其他浏览器标签。", "kuaishou_tab_unverified")
        page_id = str(opened["page"])
        if page_id.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", page_id):
            page_id = None
            raise CollectionError("快手实验搜索返回了无效标签标识。", "kuaishou_tab_unverified")
        script = _KUAISHOU_DOM_JS.replace("__LIMIT__", str(max(1, min(int(limit), 20))))
        result = _envelope_data(_run_opencli_json(base + ["eval", script, "--tab", page_id], config))
        if isinstance(result, dict) and "session" in result and "data" in result:
            result = result["data"]
        if not isinstance(result, dict):
            raise CollectionError("快手实验搜索返回了非预期DOM结果。", "kuaishou_invalid_output")
        state = result.get("state")
        if state == "login_required":
            raise CollectionError("快手需要登录；请在授权浏览器中登录后重试。", "opencli_auth_required")
        if state == "challenge":
            raise CollectionError("快手出现验证或访问限制；请由用户处理后重试，程序不会自动绕过。", "kuaishou_challenge")
        if state != "ok" or not isinstance(result.get("rows"), list) or not result["rows"]:
            raise CollectionError("快手实验搜索未读到匹配的卡片和视频链接；请检查登录或页面结构，也可用公开链接/原文导入。不能据此认定没有需求。", "kuaishou_dom_unavailable")
        rows = result["rows"][:limit]
    finally:
        if page_id is not None:
            try:
                _run_opencli_json(base + ["tab", "close", page_id], config, timeout_cap=15)
            except CollectionError:
                cleanup_warning = "未能确认实验搜索创建的标签已关闭，请在浏览器检查；未尝试关闭其他标签。"
        # Detach only our unique session even if creation returned no trusted
        # page identity. Never turn an unverified page ID into a close command.
        try:
            _run_opencli_json(base + ["unbind"], config, timeout_cap=15)
        except CollectionError:
            cleanup_warning = cleanup_warning or "实验搜索会话清理未确认，请检查OpenCLI连接。"
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionError("快手实验卡片格式无效。", "kuaishou_invalid_output")
        row["metadata"] = {"experimental": True, "adapter": "kuaishou-dom-v1"}
        if cleanup_warning:
            row["metadata"]["cleanup_warning"] = cleanup_warning
    return rows


def _read_opencli_job(prefix, platform, action, query, requested, config):
    if platform == "kuaishou":
        rows = _collect_kuaishou_rows(prefix, query, requested, config)
    else:
        argv = prefix + [platform, action, query]
        if action == "search":
            argv += ["--limit", str(requested)]
        rows = _opencli_rows(_run_opencli_json(argv, config))
    if not rows:
        raise CollectionError("搜索返回空列表；请核对搜索词、登录和页面状态，不能据此认定没有需求。", "opencli_empty_result")
    mapped = [_opencli_record(item, platform, action, query) for item in rows if isinstance(item, dict)]
    hosts = {"douyin": "douyin.com", "weibo": "weibo.com", "xiaohongshu": "xiaohongshu.com", "bilibili": "bilibili.com", "zhihu": "zhihu.com", "tieba": "tieba.baidu.com", "kuaishou": "kuaishou.com"}
    if platform in hosts:
        for record in mapped:
            value = next((record.get(k) for k in ("source_url", "url", "video_url", "profile_url", "link", "permalink") if record.get(k)), None)
            if value is None:
                continue
            try:
                host = urllib.parse.urlsplit(_source_url(value)).hostname.lower()
            except (ValueError, TypeError, AttributeError):
                raise CollectionError("平台返回了无效来源链接。", "opencli_invalid_source") from None
            expected = hosts[platform]
            if host != expected and not host.endswith("." + expected):
                raise CollectionError("平台结果包含其他站点的链接，未将其标为该平台原文。", "opencli_invalid_source")
    for original, record in zip((item for item in rows if isinstance(item, dict)), mapped):
        if platform == "kuaishou":
            record["metadata"].update(original.get("metadata", {}))
    normalized, warnings = normalize_records(mapped)
    if rows and not normalized:
        raise CollectionError("OpenCLI结果缺少来源URL或正文，未把账号列表编成需求。", "opencli_missing_evidence")
    for record in normalized:
        if warnings:
            record["is_partial"] = True
            record["metadata"]["warnings"] = warnings[:20]
    return normalized


def _opencli_prefix(config):
    if not isinstance(config, dict):
        raise CollectionError("OpenCLI配置必须是对象", "invalid_config")
    executable = str(config.get("executable") or "opencli")
    resolved = shutil.which(executable) or (executable if os.path.isfile(executable) else None)
    if not resolved:
        raise CollectionError("找不到OpenCLI；请安装官方依赖并配置可执行文件", "opencli_not_found")
    if os.path.splitext(resolved)[1].lower() in {".cmd", ".bat", ".ps1"}:
        raise CollectionError("为避免Windows隐式shell，暂不执行.cmd/.bat/.ps1；请配置原生exe或node+node_entry真实JS入口", "unsafe_launcher")
    prefix = [resolved]
    if config.get("node_entry"):
        entry = os.path.abspath(str(config["node_entry"]))
        if not os.path.isfile(entry) or os.path.splitext(entry)[1].lower() not in {".js", ".mjs", ".cjs"}:
            raise CollectionError("node_entry必须是已有JS入口文件", "invalid_launcher")
        prefix.append(entry)
    profile = str(config.get("profile") or "")
    if profile:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", profile) or profile.startswith("-"):
            raise CollectionError("OpenCLI profile别名无效", "invalid_profile")
        prefix += ["--profile", profile]
    return prefix


def collect_comments(post_urls, platforms, limit, config, options=None):
    from .comments import collect_comments as collect
    return (yield from collect(post_urls, platforms, limit, config, {} if options is None else options))


def collect_opencli(queries, platforms, limit, config):
    if not isinstance(queries, list) or not queries:
        raise CollectionError("请先填写搜索词，或启用本地模型生成查询计划", "missing_queries")
    platforms = ["tiktok"] if platforms is None or platforms == [] else platforms
    if not isinstance(platforms, list) or any(not isinstance(p, str) or p not in _SEARCH_PLATFORMS for p in platforms):
        raise CollectionError("当前只支持目录中已接入的只读搜索平台。", "invalid_platform")
    if not isinstance(config, dict):
        raise CollectionError("OpenCLI配置必须是对象", "invalid_config")
    action = config.get("action", "search")
    if not isinstance(action, str) or action not in {"search", "user", "profile"}:
        raise CollectionError("OpenCLI只允许search/user/profile只读动作", "unsafe_action")
    if action != "search" and any(action not in _LEGACY_READ_ACTIONS.get(p, set()) for p in platforms):
        raise CollectionError("国内平台当前仅开放已核实的search只读命令。", "unsafe_action")
    prefix = _opencli_prefix(config)
    clean_queries = []
    for query in queries[:10]:
        if not isinstance(query, str) or not query.strip() or query.lstrip().startswith("-") or len(query) > 300 or any(ch in query for ch in "\r\n\x00"):
            raise CollectionError("查询词为空、过长或可能被当作命令选项", "invalid_query")
        if query.strip() not in clean_queries:
            clean_queries.append(query.strip())
    platforms = list(dict.fromkeys(platforms))
    try:
        if isinstance(limit, bool):
            raise ValueError()
        budget = max(1, min(int(limit), 50))
    except (ValueError, TypeError):
        raise CollectionError("每批条数必须为整数。", "invalid_limit") from None
    jobs = [(platform, query) for query in clean_queries for platform in platforms]
    emitted, seen, errors, attempted, blocked_platforms = 0, set(), [], set(), set()
    for job_index, (platform, query) in enumerate(jobs):
        if platform in blocked_platforms:
            continue
        remaining = budget - emitted
        if remaining <= 0:
            break
        jobs_left = len(jobs) - job_index
        # Fair first pass across selected platforms: the first site cannot
        # consume the whole batch while others silently remain unsearched.
        requested = min((remaining + jobs_left - 1) // jobs_left, _SEARCH_PLATFORMS[platform]["search_limit"])
        attempted.add(platform)
        try:
            records = _read_opencli_job(prefix, platform, action, query, requested, config)
            accepted_in_job = 0
            for record in records:
                if record["source_url"] in seen:
                    continue
                record["metadata"].update(planned_jobs=len(jobs), job_index=job_index + 1, batch_budget=budget, selected_platforms=platforms)
                record["metadata"]["attempted_platforms"] = [p for p in platforms if p in attempted]
                if errors:
                    record["metadata"]["collection_errors"] = [{"platform": p, "code": e.code, "message": str(e)[:140]} for p, e in errors[:8]]
                if emitted + 1 >= budget:
                    record["metadata"]["unsearched_platforms"] = [p for p in platforms if p not in attempted]
                seen.add(record["source_url"])
                emitted += 1
                accepted_in_job += 1
                yield record
                if emitted >= budget or accepted_in_job >= requested:
                    break
        except CollectionError as error:
            errors.append((platform, error))
            if error.code in {"opencli_browser_unavailable", "opencli_config_error"}:
                # Global bridge/config failures affect every site. Do not wait
                # for the same disconnected browser once for each search term.
                break
            if error.code in {"opencli_auth_required", "kuaishou_challenge"}:
                blocked_platforms.add(platform)
    if errors:
        if len(errors) == 1:
            raise errors[0][1]
        summary = "；".join(f"{_SEARCH_PLATFORMS[p]['label']}：{str(error)[:140]}" for p, error in errors[:8])
        raise CollectionError("部分平台/搜索词读取失败：" + summary, "opencli_partial_failure" if emitted else errors[0][1].code)
    skipped = set(platforms) - attempted
    if skipped:
        names = "、".join(_SEARCH_PLATFORMS[p]["label"] for p in platforms if p in skipped)
        raise CollectionError(f"本轮条数预算已用完，以下平台未执行搜索：{names}。请增加预算或分批搜索。", "opencli_platforms_skipped")
