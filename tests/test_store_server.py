import copy
import http.client
import json
import tempfile
import threading
import unittest

from leadworkbench.server import WorkbenchServer, public_run
from leadworkbench.store import AppError, Store


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.run = self.store.create_run({'source': 'demo'})
        self.candidates = []
        for number in range(2):
            record = {'source_url': f'https://example.invalid/{number}', 'content': 'Need 300 boxes.', 'brand_name': '=HYPERLINK("bad")'}
            self.candidates.append(self.store.add_candidate(self.run['id'], record, {'role': 'buyer', 'decision': 'keep', 'evidence_quote': '300 boxes'}))

    def review(self, candidate=None, reviewer='codex', operation_id='operation-unique-1', decision='keep'):
        c = candidate or self.candidates[0]
        return {'run_id': self.run['id'], 'reviewer': reviewer, 'operation_id': operation_id,
                'changes': [{'candidate_id': c['id'], 'base_version': c['version'], 'decision': decision, 'reason': '原文明确提出采购需求。', 'evidence_ids': [c['evidence'][0]['id']]}]}

    def test_local_analysis_is_not_formal_review(self):
        self.assertEqual('unreviewed', self.candidates[0]['effective_decision'])
        self.assertEqual('keep', self.candidates[0]['local_decision'])

    def test_review_retry_is_idempotent_and_payload_reuse_rejected(self):
        payload = self.review()
        first = self.store.submit_review(payload)
        self.assertEqual(first, self.store.submit_review(payload))
        self.assertEqual(2, self.store.get_candidate(self.candidates[0]['id'])['version'])
        payload['changes'][0]['reason'] = 'different'
        with self.assertRaises(AppError) as error:
            self.store.submit_review(payload)
        self.assertEqual('IDEMPOTENCY_CONFLICT', error.exception.code)

    def test_batch_conflict_rolls_back_all_changes(self):
        payload = self.review()
        bad = copy.deepcopy(payload['changes'][0])
        bad.update(candidate_id=self.candidates[1]['id'], base_version=99)
        payload['changes'].append(bad)
        with self.assertRaises(AppError):
            self.store.submit_review(payload)
        self.assertEqual(1, self.store.get_candidate(self.candidates[0]['id'])['version'])
        self.assertEqual([], self.store.list_reviews(self.run['id'])['items'])

    def test_evidence_reference_cannot_cross_candidates(self):
        payload = self.review()
        payload['changes'][0]['evidence_ids'] = [self.candidates[1]['evidence'][0]['id']]
        with self.assertRaises(AppError) as error:
            self.store.submit_review(payload)
        self.assertEqual('INVALID_EVIDENCE_REFERENCE', error.exception.code)

    def test_human_priority_and_evidence_immutable_after_revert(self):
        before = copy.deepcopy(self.candidates[0]['evidence'])
        self.store.submit_review(self.review())
        c = self.store.get_candidate(self.candidates[0]['id'])
        human = self.store.submit_review(self.review(c, 'human', 'operation-human-1', 'exclude'))
        self.assertEqual('exclude', self.store.get_candidate(c['id'])['effective_decision'])
        self.store.revert_review(human['id'], 'operation-revert-1')
        after = self.store.get_candidate(c['id'])
        self.assertEqual('keep', after['effective_decision'])
        self.assertEqual('codex', after['reviewer'])
        self.assertEqual(before, after['evidence'])
        self.assertEqual(4, after['version'])

    def test_undo_rejects_intervening_edit(self):
        first = self.store.submit_review(self.review())
        c = self.store.get_candidate(self.candidates[0]['id'])
        self.store.submit_review(self.review(c, 'codex', 'operation-next-1', 'watch'))
        with self.assertRaises(AppError) as error:
            self.store.revert_review(first['id'], 'operation-revert-1')
        self.assertEqual('VERSION_CONFLICT', error.exception.code)

    def test_frozen_snapshot_and_current_export_diverge_explicitly(self):
        frozen = self.store.freeze_run(self.run['id'])
        self.store.submit_review(self.review())
        self.assertEqual(frozen, self.store.freeze_run(self.run['id']))
        exported = self.store.export_run(self.run['id'])
        self.assertEqual(1, exported['snapshot']['candidates'][0]['version'])
        self.assertEqual(2, exported['candidates'][0]['version'])

    def test_csv_untrusted_formula_is_escaped(self):
        self.assertIn("'=HYPERLINK", self.store.export_csv(self.run['id']))

    def test_invalid_settings_and_review_types_are_business_errors(self):
        for patch in [{'opencli': {'executable': []}}, {'model': {'enabled': 'false'}}, {'model': {'extra_body': {'messages': []}}}]:
            with self.assertRaises(AppError):
                self.store.update_settings(patch)
        with self.assertRaises(AppError):
            self.store.create_run({'source': []})
        for key, value in [('reviewer', []), ('run_id', {})]:
            payload = self.review()
            payload[key] = value
            with self.assertRaises(AppError):
                self.store.submit_review(payload)
        for key, value in [('candidate_id', []), ('decision', {})]:
            payload = self.review()
            payload['changes'][0][key] = value
            with self.assertRaises(AppError):
                self.store.submit_review(payload)

    def test_public_run_removes_input_and_checkpoint_but_keeps_counts(self):
        self.store.update_run(self.run['id'], progress={'checkpoint': {'records': ['private']}, 'current': 0})
        public = public_run(self.store.get_run(self.run['id']))
        self.assertNotIn('checkpoint', public['progress'])
        self.assertNotIn('records', public['config'])
        self.assertEqual(2, public['counts']['total'])


class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = WorkbenchServer(('127.0.0.1', 0), self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method='GET', path='/api/health', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_bootstrap_and_static_without_model_request(self):
        status, raw, headers = self.request(path='/api/bootstrap')
        data = json.loads(raw)['data']
        self.assertEqual(200, status)
        self.assertTrue(data['token'])
        self.assertNotIn('api_key', data['settings']['model'])
        self.assertFalse(data['settings']['model']['enabled'])
        for path in ['/', '/app.js', '/app.css']:
            self.assertEqual(200, self.request(path=path)[0])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_host_origin_token_guards(self):
        self.assertEqual(403, self.request(headers={'Host': 'evil.example'})[0])
        body = {'source': 'demo'}
        base = {'Content-Type': 'application/json'}
        self.assertEqual(403, self.request('POST', '/api/runs', body, base)[0])
        base['X-App-Token'] = self.server.token
        base['Origin'] = 'https://evil.example'
        self.assertEqual(403, self.request('POST', '/api/runs', body, base)[0])
        base['Origin'] = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(201, self.request('POST', '/api/runs', body, base)[0])

    def test_second_instance_cannot_interrupt_first_instances_run(self):
        run = self.server.store.create_run({'source': 'demo'})
        self.server.store.update_run(run['id'], status='running')
        # Another port with the same data directory must fail before DB recovery.
        with self.assertRaises(AppError) as error:
            WorkbenchServer(('127.0.0.1', 0), self.temp.name)
        self.assertEqual('INSTANCE_RUNNING', error.exception.code)
        # The same busy port must also fail without opening that DB.
        with self.assertRaises(OSError):
            WorkbenchServer(('127.0.0.1', self.server.server_port), self.temp.name)
        self.assertEqual('running', self.server.store.get_run(run['id'])['status'])

    def test_bad_body_and_traversal_do_not_expose_files(self):
        for path in ['/../leadworkbench/store.py', '/data/server.json', '/%2e%2e/server.py']:
            self.assertEqual(404, self.request(path=path)[0])
        status, raw, _ = self.request('POST', '/api/runs', [], {'Content-Type': 'application/json', 'X-App-Token': self.server.token})
        self.assertEqual(400, status)
        self.assertEqual('OBJECT_REQUIRED', json.loads(raw)['error']['code'])


if __name__ == '__main__':
    unittest.main()
