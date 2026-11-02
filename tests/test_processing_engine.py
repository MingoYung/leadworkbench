import copy
import tempfile
import time
import unittest
from unittest import mock

from leadworkbench import collectors, models
from leadworkbench.engine import RunEngine
from leadworkbench.processing import normalize_collection, ProcessingError
from leadworkbench.store import Store, AppError


def comment(number='1', content='多少钱，求推荐施工队', parent='https://www.bilibili.com/video/BV1fixture'):
    return {'source_url': parent, 'kind': 'comment', 'platform': 'bilibili', 'content': content,
            'author_handle': '评论者' + number, 'is_partial': True, 'is_demo': True,
            'metadata': {'comment_id': number, 'parent_source_url': parent, 'parent_title': '杭州装修博主案例'}}


def generated(records, summary=None, error=None):
    yield from copy.deepcopy(records)
    if error:
        raise error
    return summary or {'posts_attempted': 1, 'comments_read': len(records), 'comments_matched': len(records), 'comments_returned': len(records)}


class OptionalProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.engine = RunEngine(self.store)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.engine.shutdown()
        self.temp.cleanup()

    def wait(self, run_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            run = self.store.get_run(run_id)
            if run['status'] in {'completed', 'completed_partial', 'failed', 'paused'}:
                return run
            time.sleep(0.01)
        self.fail('worker did not complete')

    def test_default_disabled_model_collects_without_any_model_function(self):
        self.store.update_settings({'model': {'enabled': False, 'base_url': 'http://127.0.0.1:1/v1'}})
        run = self.store.create_run({'source': 'import', 'records': [comment('1'), comment('2')]})
        self.assertEqual('collect_only', run['config']['analysis_mode'])
        with mock.patch.object(models, 'analyze_evidence', side_effect=AssertionError('must not call a model')) as analyze, mock.patch.object(models, 'plan_queries', side_effect=AssertionError('must not plan with a model')) as plan:
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual('completed_partial', finished['status'])
        self.assertEqual(0, analyze.call_count + plan.call_count)
        candidates = self.store.list_candidates(run['id'])['items']
        self.assertEqual(2, len(candidates))
        self.assertEqual({'评论者1', '评论者2'}, {c['author_handle'] for c in candidates})
        for candidate in candidates:
            full = self.store.get_candidate(candidate['id'])
            self.assertIsNone(full['local_decision'])
            self.assertEqual('unreviewed', full['effective_decision'])
            self.assertEqual('not_requested', full['analysis']['analysis_status'])
            self.assertIsNone(full['analysis']['model'])
            self.assertIn('多少钱', full['content_excerpt'])
        snapshot = self.store.export_run(run['id'])['snapshot']
        self.assertEqual([], snapshot['models'])
        self.assertEqual(['collect_only'], snapshot['analysis_modes'])

    def test_raw_comment_review_does_not_require_model(self):
        run = self.store.create_run({'source': 'import', 'records': [comment()], 'analysis_mode': 'collect_only'})
        self.engine.start(run['id']); self.wait(run['id'])
        candidate = self.store.get_candidate(self.store.list_candidates(run['id'])['items'][0]['id'])
        before = copy.deepcopy(candidate['evidence'])
        result = self.store.submit_review({'run_id': run['id'], 'reviewer': 'codex', 'operation_id': 'raw-comment-review', 'changes': [{'candidate_id': candidate['id'], 'base_version': 1, 'decision': 'needs_info', 'reason': '评论询价但没有自己的城市或预算。', 'evidence_ids': [candidate['evidence'][0]['id']]}]})
        after = self.store.get_candidate(candidate['id'])
        self.assertEqual('applied', result['status'])
        self.assertEqual(before, after['evidence'])
        self.assertEqual('needs_info', after['effective_decision'])

    def test_explicit_local_run_can_override_new_run_default(self):
        run = self.store.create_run({'source': 'import', 'records': [comment()], 'analysis_mode': 'local'})
        seen = []
        def analyze(settings, record):
            seen.append(settings['model']['enabled'])
            return {'role': 'consumer', 'priority': 'B', 'decision': 'needs_info', 'reason': '需核查评论者地区。', 'facts': {}, 'analysis_status': 'completed', 'model': 'fixture'}
        with mock.patch.object(models, 'analyze_evidence', side_effect=analyze):
            self.engine.start(run['id']); self.wait(run['id'])
        self.assertEqual([True], seen)

    def test_legacy_run_without_saved_mode_does_not_force_model_on(self):
        run = self.store.create_run({'source': 'import', 'records': [comment()]})
        config = copy.deepcopy(run['config']); config.pop('analysis_mode')
        self.store.update_run(run['id'], config=config)
        with mock.patch.object(models, 'analyze_evidence', side_effect=AssertionError('legacy must honor disabled preference')) as analyze:
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertNotEqual('failed', finished['status'])
        self.assertEqual(0, analyze.call_count)

    def test_comment_source_preserves_distinct_comments_and_summary(self):
        run = self.store.create_run({'source': 'comments', 'urls': [comment()['source_url']], 'analysis_mode': 'collect_only'})
        summary = {'posts_attempted': 1, 'comments_read': 30, 'comments_matched': 2, 'comments_returned': 2}
        with mock.patch.object(collectors, 'collect_comments', return_value=generated([comment('1'), comment('2')], summary)), mock.patch.object(models, 'analyze_evidence', side_effect=AssertionError('no model')):
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual(2, finished['counts']['total'])
        self.assertEqual(30, finished['progress']['collection_summary']['comments_read'])

    def test_zero_matches_is_successful_bounded_sample(self):
        run = self.store.create_run({'source': 'comments', 'urls': [comment()['source_url']], 'analysis_mode': 'collect_only'})
        summary = {'posts_attempted': 1, 'comments_read': 20, 'comments_matched': 0, 'comments_returned': 0}
        with mock.patch.object(collectors, 'collect_comments', return_value=generated([], summary)):
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual('completed', finished['status'])
        self.assertEqual(summary, finished['progress']['collection_summary'])
        self.assertEqual(0, finished['counts']['total'])

    def test_zero_candidates_still_preserves_failed_read_summary(self):
        run = self.store.create_run({'source': 'comments', 'urls': [comment()['source_url']], 'analysis_mode': 'collect_only'})
        error = collectors.CollectionError('second post could not be read', 'comments_partial_failure')
        error.collection_summary = {'posts_attempted': 2, 'comments_read': 20, 'comments_matched': 0}
        with mock.patch.object(collectors, 'collect_comments', return_value=generated([], error=error)):
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual('failed', finished['status'])
        self.assertEqual(20, finished['progress']['collection_summary']['comments_read'])

    def test_discovery_then_comments_does_not_store_parent_as_commenter(self):
        parent = {'source_url': 'https://weibo.com/detail/12345', 'platform': 'weibo', 'kind': 'post', 'content': '杭州装修博主案例', 'author_handle': '父帖博主', 'is_partial': True}
        record = comment('1', parent=parent['source_url']); record['platform'] = 'weibo'
        run = self.store.create_run({'source': 'opencli', 'queries': ['装修'], 'platforms': ['weibo'], 'analysis_mode': 'collect_only', 'collection': {'scope': 'comments', 'keywords': ['多少钱']}})
        def read_comments(parents, platforms, limit, config, options):
            self.assertEqual(parent['source_url'], parents[0]['source_url'])
            self.assertEqual(['多少钱'], options['keywords'])
            return generated([record])
        with mock.patch.object(collectors, 'collect_opencli', return_value=generated([parent])), mock.patch.object(collectors, 'collect_comments', side_effect=read_comments), mock.patch.object(models, 'plan_queries', side_effect=AssertionError('no model')):
            self.engine.start(run['id']); finished = self.wait(run['id'])
        candidates = self.store.list_candidates(run['id'])['items']
        self.assertEqual(1, len(candidates))
        self.assertEqual('comment', candidates[0]['kind'])
        self.assertEqual('评论者1', candidates[0]['author_handle'])
        self.assertNotIn('杭州', candidates[0]['content_excerpt'])

    def test_combined_scope_reserves_comment_budget(self):
        with self.assertRaises(AppError):
            self.store.create_run({'source': 'opencli', 'queries': ['装修'], 'platforms': ['weibo'], 'analysis_mode': 'collect_only', 'collection': {'scope': 'posts_and_comments'}, 'limit': 1})
        parent = {'source_url': 'https://weibo.com/detail/12345', 'platform': 'weibo', 'kind': 'post', 'content': '装修案例'}
        run = self.store.create_run({'source': 'opencli', 'queries': ['装修'], 'platforms': ['weibo'], 'analysis_mode': 'collect_only', 'collection': {'scope': 'posts_and_comments'}, 'limit': 3})
        def read_comments(parents, platforms, limit, config, options):
            self.assertEqual(2, limit)
            return generated([comment('1'), comment('2')])
        with mock.patch.object(collectors, 'collect_opencli', return_value=generated([parent])), mock.patch.object(collectors, 'collect_comments', side_effect=read_comments):
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual(3, finished['counts']['total'])
        self.assertEqual({'post', 'comment'}, {c['kind'] for c in self.store.list_candidates(run['id'])['items']})

    def test_same_comment_id_in_different_posts_is_not_merged(self):
        records = [comment('1'), comment('1', parent='https://www.bilibili.com/video/BV2fixture')]
        run = self.store.create_run({'source': 'import', 'records': records, 'analysis_mode': 'collect_only'})
        self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual(2, finished['counts']['total'])

    def test_combined_scope_checks_current_limit_before_any_io(self):
        run = self.store.create_run({'source': 'opencli', 'queries': ['装修'], 'platforms': ['weibo'], 'analysis_mode': 'collect_only', 'collection': {'scope': 'posts_and_comments'}, 'limit': 2})
        self.store.update_settings({'limits': {'max_candidates': 1}})
        with mock.patch.object(collectors, 'collect_opencli', side_effect=AssertionError('must reject before collection')) as collect:
            self.engine.start(run['id']); finished = self.wait(run['id'])
        self.assertEqual('failed', finished['status'])
        self.assertEqual(0, collect.call_count)
        self.assertIn('至少为2', str(finished['error']))

    def test_processing_options_reject_writes_and_invalid_limits(self):
        for value in [{'scope': 'write'}, {'max_posts': 0}, {'include_replies': 'false'}, {'keywords': '报价'}]:
            with self.subTest(value=value), self.assertRaises(ProcessingError):
                normalize_collection(value)
        with self.assertRaises(AppError):
            self.store.create_run({'source': 'opencli', 'goal': 'find prospects', 'analysis_mode': 'collect_only'})


if __name__ == '__main__':
    unittest.main()
