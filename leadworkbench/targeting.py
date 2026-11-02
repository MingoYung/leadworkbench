"""Editable targeting criteria and bounded, deterministic search planning."""
from __future__ import annotations

from urllib.parse import quote
import json


class TargetingError(ValueError):
    pass


def _text(value, name, maximum):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise TargetingError(f'{name} 应为不超过 {maximum} 字的文字。')
    return value.strip()


def _strings(value, name, maximum=20, item_maximum=80):
    if not isinstance(value, list) or len(value) > maximum:
        raise TargetingError(f'{name} 应为最多 {maximum} 项的数组。')
    return list(dict.fromkeys(text for item in value if (text := _text(item, name, item_maximum))))


def normalize_targeting(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise TargetingError('定向条件应为 JSON 对象。')
    allowed = {'market', 'industry', 'regions', 'include_tags', 'exclude_tags', 'customer_type', 'needs', 'custom_fields'}
    if set(value) - allowed:
        raise TargetingError('定向条件含未支持的字段。')
    market = value.get('market', 'global')
    customer = value.get('customer_type', 'consumer' if market == 'cn' else 'business')
    if not isinstance(market, str) or market not in {'cn', 'global'}:
        raise TargetingError('市场仅支持 cn 或 global。')
    if not isinstance(customer, str) or customer not in {'consumer', 'business', 'any'}:
        raise TargetingError('顾客类型应为 consumer、business 或 any。')
    fields = value.get('custom_fields', {})
    if not isinstance(fields, dict) or len(fields) > 12:
        raise TargetingError('自定义条件应为最多 12 项的对象。')
    custom = {}
    for key, item in fields.items():
        key = _text(key, '自定义条件名称', 50)
        item = _text(item, '自定义条件内容', 300)
        if not key:
            raise TargetingError('自定义条件名称不能为空。')
        if item:
            custom[key] = item
    result = {'market': market, 'industry': _text(value.get('industry', ''), '行业', 100),
            'regions': _strings(value.get('regions', []), '地区', 10, 60),
            'include_tags': _strings(value.get('include_tags', []), '需求标签'),
            'exclude_tags': _strings(value.get('exclude_tags', []), '排除标签'),
            'customer_type': customer, 'needs': _text(value.get('needs', ''), '其他需求', 2500),
            'custom_fields': custom}
    if len(json.dumps(result, ensure_ascii=False)) > 5000:
        raise TargetingError('定向条件总长超过 5000 字符，请精简需求或补充条件后再运行。')
    return result


def active_targeting(target):
    return bool(target['market'] == 'cn' or target['customer_type'] != 'business' or
                any(target[k] for k in ('industry', 'regions', 'include_tags', 'exclude_tags', 'needs', 'custom_fields')))


def region_text_matches(requested, observed):
    """Conservative textual match, without inventing geocoding knowledge."""
    suffixes = ('特别行政区', '自治州', '地区', '省', '市', '区', '县')
    def core(text):
        text = ''.join(text.split())
        for suffix in suffixes:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                return text[:-len(suffix)]
        return text
    requested, observed = core(requested), core(observed)
    if requested == observed:
        return True
    # "北京路" must not satisfy city "北京". A compound location only
    # counts when the requested place has an explicit administrative boundary.
    return any(observed.startswith(requested + suffix) for suffix in ('省', '市', '区', '县', '自治州'))


def targeting_summary(target):
    parts = ['国内定向' if target['market'] == 'cn' else '海外定向',
             target['industry'] or '行业不限',
             {'consumer': '个人顾客', 'business': '商家客户', 'any': '顾客类型不限'}[target['customer_type']]]
    if target['regions']:
        parts.append('地区：' + '、'.join(target['regions']))
    if target['include_tags']:
        parts.append('需求标签：' + '、'.join(target['include_tags']))
    return ' / '.join(parts)


def build_queries(target, supplied=None, limit=10):
    supplied = _strings(supplied or [], '搜索词', 50, 300)
    if any(q.startswith('-') or '\n' in q or '\r' in q for q in supplied):
        raise TargetingError('搜索词不能以选项符号开头或包含换行。')
    if supplied:
        return supplied[:limit]
    industry = target['industry'] or ('家装' if target['market'] == 'cn' else 'supplier')
    renovation = any(word in industry for word in ('家装', '装修', '整装', '全屋', '装饰'))
    if target['market'] == 'cn':
        intents = ['找装修公司', '装修求推荐', '准备装修', '旧房改造', '找施工队', '装修报价'] if renovation else [industry + ' 求推荐', '找' + industry, industry + ' 询价']
        # Exclusion terms are contextual review criteria, never a naive string
        # blacklist: "求推荐装修公司" is often exactly the desired customer.
        tagged = [industry + ' ' + tag for tag in target['include_tags']]
        interleaved = []
        for index in range(max(len(intents), len(tagged))):
            if index < len(tagged):
                interleaved.append(tagged[index])
            if index < len(intents):
                interleaved.append(intents[index])
        intents = interleaved
    else:
        intents = [industry + ' looking for', industry + ' supplier wanted'] + target['include_tags']
    regions = target['regions'] or ['']
    # Round-robin regions so one city cannot silently consume the whole budget.
    phrases = [' '.join(p for p in (region, intent) if p).strip() for intent in intents for region in regions]
    return list(dict.fromkeys(phrases))[:limit]


def search_preview(payload, catalog):
    if not isinstance(payload, dict):
        raise TargetingError('预览内容应为对象。')
    target = normalize_targeting(payload.get('targeting'))
    supplied = payload.get('queries', [])
    if not isinstance(supplied, list):
        raise TargetingError('搜索词应为数组。')
    queries = build_queries(target, supplied)
    selected = _strings(payload.get('platforms', []), '平台', 20, 40)
    by_id = {p['id']: p for p in catalog}
    if any(p not in by_id for p in selected):
        raise TargetingError('选择了未登记的平台。其他平台可先使用公开链接或导入资料。')
    visible = [p for p in catalog if p['market'] == target['market']]
    manual = []
    for platform in selected:
        item = by_id[platform]
        template = item.get('search_url_template')
        if template and queries:
            manual.append({'platform': platform, 'label': item['label'], 'url': template.replace('{query}', quote(queries[0], safe=''))})
    warnings = []
    if target['regions']:
        warnings.append('地区用于关键词定向；是否属于目标地区仍按原文证据核查，不代表平台精确地理筛选。')
    if target['exclude_tags']:
        warnings.append('排除标签按作者身份和发帖意图判断；“求推荐装修公司”不会因出现“装修公司”而直接排除。')
    if any(not by_id[p]['search_supported'] for p in selected):
        warnings.append('部分平台尚无自动搜索适配，可打开平台搜索或用公开链接、导入资料进入审核流程。')
    if len(supplied) > 10:
        warnings.append('本轮搜索词最多执行 10 条，多余词请拆成下一批。')
    uncovered = [tag for tag in target['include_tags'] if not any(tag in q for q in queries)]
    if uncovered:
        warnings.append('本轮搜索词预算未直接覆盖这些标签，请调整搜索词或拆批：' + '、'.join(uncovered))
    return {'targeting': target, 'queries': queries, 'summary': targeting_summary(target),
            'platforms': visible, 'manual_links': manual, 'warnings': warnings}
