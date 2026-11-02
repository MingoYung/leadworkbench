"""Run-level processing choice; raw collection never needs a model service."""
from __future__ import annotations


class ProcessingError(ValueError):
    pass


def normalize_analysis_mode(value, model_enabled=False):
    if value is None:
        return 'local' if model_enabled else 'collect_only'
    if not isinstance(value, str) or value not in {'local', 'collect_only'}:
        raise ProcessingError('处理方式只能选择 local 或 collect_only。')
    return value


def normalize_collection(value=None, source=None):
    if value is None:
        value = {}
    allowed = {'scope', 'keywords', 'match_mode', 'max_posts', 'max_comments_per_post', 'include_replies'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ProcessingError('采集范围应为包含已支持字段的对象。')
    scope = value.get('scope', 'comments' if source == 'comments' else 'posts')
    if not isinstance(scope, str) or scope not in {'posts', 'comments', 'posts_and_comments'}:
        raise ProcessingError('采集范围应为 posts、comments 或 posts_and_comments。')
    if source == 'comments' and scope != 'comments':
        raise ProcessingError('指定帖子评论来源应选择 comments 范围。')
    if source == 'urls' and scope != 'posts':
        raise ProcessingError('读取帖子评论请改选“指定帖子评论”来源。')
    keywords = value.get('keywords', [])
    if not isinstance(keywords, list) or len(keywords) > 20 or any(not isinstance(k, str) or len(k) > 100 or any(c in k for c in '\x00\r\n') for k in keywords):
        raise ProcessingError('评论关键词应为最多 20 项、每项不超过 100 字的文字数组。')
    match = value.get('match_mode', 'any')
    if not isinstance(match, str) or match not in {'any', 'all'}:
        raise ProcessingError('评论关键词匹配方式应为 any 或 all。')
    counts = {}
    for key, default, maximum in [('max_posts', 3, 20), ('max_comments_per_post', 20, 100)]:
        number = value.get(key, default)
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= maximum:
            raise ProcessingError(f'{key} 应为 1–{maximum} 的整数。')
        counts[key] = number
    replies = value.get('include_replies', False)
    if not isinstance(replies, bool):
        raise ProcessingError('是否包含回复应为布尔值。')
    return {'scope': scope, 'keywords': list(dict.fromkeys(k.strip() for k in keywords if k.strip())),
            'match_mode': match, 'include_replies': replies, **counts}


def collected_analysis(record):
    return {'analysis_status': 'not_requested', 'analysis_mode': 'collect_only',
            'role': 'unknown', 'priority': 'unknown', 'decision': None,
            'reason': '本轮只采集原文，未请求本地模型分析；等待人工或 Codex 审核。',
            'facts': {}, 'unknown_fields': [], 'citations': [],
            'evidence_quote': str(record.get('content', ''))[:300],
            'model': None, 'protocol': None, 'prompt_version': None,
            'is_synthetic': record.get('is_demo') is True}
