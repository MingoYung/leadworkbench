import copy
import tempfile
import unittest
from unittest import mock

from leadworkbench import models
from leadworkbench.store import Store, AppError
from leadworkbench.targeting import normalize_targeting, build_queries, search_preview, TargetingError


def brief():
    return {'market': 'cn', 'industry': '家装', 'regions': ['杭州'], 'customer_type': 'consumer',
            'include_tags': ['旧房改造'], 'exclude_tags': ['装修公司', '招聘'],
            'needs': '寻找有实际装修需求的业主', 'custom_fields': {}}


def homeowner():
    return {'source_url': 'https://fixture.invalid/renovation', 'platform': 'test',
            'content': '【虚构验收样例，非真实客户】我在杭州，准备旧房改造，求推荐装修公司，预算20万。', 'is_demo': True}


def response():
    return {'role': 'consumer', 'priority': 'A', 'decision': 'keep', 'reason': '业主明确寻求旧房装修服务。',
            'facts': {}, 'citations': [{'field': 'role', 'quote': '准备旧房改造，求推荐装修公司'}],
            'target_match': {'status': 'matched', 'reason': '符合杭州家装顾客范围。',
                             'matched_tags': [{'tag': '旧房改造', 'quote': '准备旧房改造'}],
                             'region': {'value': '杭州', 'quote': '我在杭州'},
                             'custom_evidence': {}, 'missing_fields': []}}


class TargetPlanTests(unittest.TestCase):
    def test_domestic_queries_cover_regions_and_do_not_apply_literal_exclusions(self):
        target = normalize_targeting(brief())
        target['regions'] = ['杭州', '苏州']
        queries = build_queries(target)
        self.assertTrue(any('杭州' in q for q in queries))
        self.assertTrue(any('苏州' in q for q in queries))
        self.assertTrue(any('找装修公司' in q for q in queries))
        self.assertFalse(any('-装修公司' in q or '-招聘' in q for q in queries))
        self.assertTrue(any('旧房改造' in q for q in queries))

    def test_custom_conditions_and_text_tags_are_typed_and_bounded(self):
        for bad in [{'regions': '杭州'}, {'market': []}, {'customer_type': []}, {'custom_fields': {'预算': 20}}, {'include_tags': ['a'] * 21}]:
            with self.subTest(bad=bad), self.assertRaises(TargetingError):
                normalize_targeting(bad)

    def test_preview_manual_link_encodes_only_query_and_does_not_claim_precise_geo(self):
        catalog = [{'id': 'kuaishou', 'label': '快手', 'market': 'cn', 'search_supported': False,
                    'search_url_template': 'https://www.kuaishou.com/search/video?searchKey={query}'}]
        result = search_preview({'targeting': brief(), 'platforms': ['kuaishou'], 'queries': ['杭州 装修?x=&']}, catalog)
        self.assertIn('%3Fx%3D%26', result['manual_links'][0]['url'])
        self.assertTrue(any('精确地理' in w for w in result['warnings']))
        self.assertTrue(any('暂' in w or '尚无' in w for w in result['warnings']))

    def test_store_persists_full_targeting_and_generates_cn_plan(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            run = store.create_run({'source': 'opencli', 'targeting': brief(), 'platforms': ['douyin']})
            self.assertEqual('consumer', run['config']['targeting']['customer_type'])
            self.assertTrue(run['config']['queries'])
            self.assertTrue(all('杭州' in q for q in run['config']['queries']))
            self.assertIn('家装', run['goal'])

    def test_unknown_platform_is_not_scheduled_as_supported(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            with self.assertRaises(AppError):
                store.create_run({'source': 'opencli', 'targeting': brief(), 'platforms': ['made-up-platform']})


class TargetAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'model': {'enabled': True}, 'targeting': brief()}

    def analyze(self, reply, record=None):
        with mock.patch.object(models, '_chat', return_value=copy.deepcopy(reply)) as chat:
            result = models.analyze_evidence(self.settings, record or homeowner())
            self.assertIn('do NOT reject personal customers', chat.call_args.args[1])
            return result

    def test_personal_homeowner_is_kept_despite_excluded_word_in_request(self):
        result = self.analyze(response())
        self.assertEqual('keep', result['decision'])
        self.assertEqual('matched', result['target_match']['status'])
        self.assertEqual(['旧房改造'], result['target_match']['matched_tags'])
        self.assertTrue(result['is_synthetic'])
        self.assertEqual('evidence-v2-targeting', result['prompt_version'])

    def test_search_city_without_source_evidence_is_not_a_match(self):
        value = response(); value['target_match']['region'] = None
        result = self.analyze(value)
        self.assertEqual('needs_info', result['decision'])
        self.assertEqual('needs_info', result['target_match']['status'])
        self.assertIn('目标地区', result['target_match']['missing_fields'])

    def test_road_named_after_another_city_is_not_a_city_match(self):
        self.settings['targeting']['regions'] = ['北京']
        value = response(); value['target_match']['region'] = {'value': '北京路', 'quote': '我在上海北京路'}
        record = homeowner(); record['content'] += '我在上海北京路。'
        result = self.analyze(value, record)
        self.assertEqual('needs_info', result['decision'])

    def test_needs_info_never_keeps_positive_decision(self):
        value = response(); value['target_match']['status'] = 'needs_info'
        result = self.analyze(value)
        self.assertEqual('needs_info', result['decision'])

    def test_one_bounded_repair_preserves_first_validation_error(self):
        invalid = response(); invalid['facts'] = {'product': {'value': '虚构家装服务名', 'quote': '准备旧房改造'}}
        with mock.patch.object(models, '_chat', side_effect=[invalid, response()]) as chat:
            result = models.analyze_evidence(self.settings, homeowner())
        self.assertEqual(2, chat.call_count)
        self.assertEqual(1, result['validation_retries'])
        self.assertEqual('unsupported_fact', result['validation_first_error']['code'])

    def test_home_service_budget_and_area_are_not_item_quantities(self):
        value = response(); value['facts'] = {'quantity': {'value': '20万', 'quote': '预算20万'}}
        with self.assertRaises(models.ModelError) as caught:
            self.analyze(value)
        self.assertEqual('invalid_fact', caught.exception.code)
        value['facts'] = {'budget': {'value': '20万', 'quote': '预算20万'}}
        result = self.analyze(value)
        self.assertEqual('20万', result['facts']['budget'])
        self.assertNotIn('quantity', result['facts'])

    def test_arbitrary_custom_condition_needs_separate_source_quote(self):
        self.settings['targeting']['custom_fields'] = {'预算': '15至25万'}
        result = self.analyze(response())
        self.assertEqual('needs_info', result['decision'])
        self.assertIn('预算', result['target_match']['missing_fields'])
        value = response()
        value['target_match']['custom_evidence'] = {'预算': {'value': '20万', 'quote': '预算20万'}}
        result = self.analyze(value)
        self.assertEqual('keep', result['decision'])
        self.assertEqual('20万', result['target_match']['custom_evidence']['预算']['value'])

    def test_invented_target_quote_rejected(self):
        value = response(); value['target_match']['region'] = {'value': '杭州', 'quote': '我是杭州的新房业主'}
        with self.assertRaises(models.ModelError) as caught:
            self.analyze(value)
        self.assertEqual('unsupported_citation', caught.exception.code)

    def test_seller_does_not_turn_into_consumer_on_matching_topic(self):
        value = response(); value['role'] = 'seller'
        result = self.analyze(value)
        self.assertEqual('exclude', result['decision'])
        self.assertEqual('excluded', result['target_match']['status'])

    def test_analysis_window_is_explicit_and_full_record_not_modified(self):
        record = homeowner(); record['content'] += '原文' * 5000
        original = copy.deepcopy(record)
        result = self.analyze(response(), record)
        self.assertEqual(6000, result['input_window']['analyzed_chars'])
        self.assertTrue(result['input_window']['truncated'])
        self.assertEqual(original, record)

    def test_unknown_label_is_not_promoted_to_user_tag(self):
        value = response(); value['target_match']['matched_tags'] = [{'tag': '高净值客户', 'quote': '预算20万'}]
        with self.assertRaises(models.ModelError):
            self.analyze(value)

    def test_store_keeps_targeting_evidence_and_tag_search(self):
        result = self.analyze(response())
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            run = store.create_run({'source': 'import', 'records': [homeowner()], 'targeting': brief()})
            candidate = store.add_candidate(run['id'], homeowner(), result)
            self.assertTrue(candidate['is_demo'])
            self.assertEqual('杭州', candidate['target_match']['matched_region'])
            self.assertEqual(1, store.list_candidates(run['id'], q='旧房改造')['total'])
            self.assertEqual(homeowner()['content'], candidate['evidence'][0]['content'])
            csv = store.export_csv(run['id'])
            self.assertIn('target_match_status', csv)
            self.assertIn('matched_region', csv)


if __name__ == '__main__':
    unittest.main()
