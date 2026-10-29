import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
import runner


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'output').mkdir()
        self.case = runner.Case(caseId=1, name='delete', steps='请求：DELETE /api/contracts/{{contractId}}\n期望状态码：200\n断言：$.code=0')
        self.report()
        events = [{'request_data': {'method': 'DELETE', 'url': 'http://localhost/api/contracts/123'}},
                  {'response_data': {'status_code': 200, 'body': '{"code":0}'}}]
        (self.root / 'api_logs.log').write_text('\n'.join(map(json.dumps, events)))

    def tearDown(self):
        self.temp.cleanup()

    def report(self, complete='true', child=''):
        (self.root / 'output/result.xml').write_text(f'<testsuites><testsuite><testcase><properties><property name="is_completed" value="{complete}"/></properties>{child}</testcase></testsuite></testsuites>')

    def test_original_response_pass(self):
        state, evidence = runner.inspect_evidence(self.root, self.case)
        self.assertEqual(state, 'passed')
        self.assertEqual(len(evidence['checks']), 2)

    def test_original_response_mismatch(self):
        self.case.steps = self.case.steps.replace('$.code=0', '$.code=1')
        self.assertEqual(runner.inspect_evidence(self.root, self.case)[0], 'failed')

    def test_incomplete_not_false_pass(self):
        self.report('false')
        self.assertEqual(runner.inspect_evidence(self.root, self.case)[0], 'error')

    def test_explicit_terminal_assertion_with_legacy_completion_flag(self):
        self.report('false')
        history = {'planner': [{'content': {'terminate': 'yes', 'is_assert': True, 'is_passed': True}}]}
        (self.root / 'agent_inner_thoughts.json').write_text(json.dumps(history))
        self.assertEqual(runner.inspect_evidence(self.root, self.case)[0], 'passed')
        (self.root / 'api_logs.log').unlink()
        self.assertEqual(runner.inspect_evidence(self.root, self.case)[0], 'error')

    def test_wrong_origin_is_not_evidence(self):
        self.assertEqual(runner.inspect_evidence(self.root, self.case, 'http://elsewhere')[0], 'error')

    def test_failed_report(self):
        self.report(child='<failure message="secret must not be copied"/>')
        state, evidence = runner.inspect_evidence(self.root, self.case)
        self.assertEqual(state, 'failed')
        self.assertNotIn('secret', json.dumps(evidence))

    def marker_events(self, marked_status=200, duplicate=False, omit=False):
        events = []
        for identifier, headers, status in [(100, {}, 200), (200, {} if omit else {'X-Hercules-Request-Id': 'run:1'}, marked_status), (300, {}, 200)]:
            events += [{'request_data': {'method': 'GET', 'url': f'http://localhost/api/contracts/{identifier}', 'headers': headers}},
                       {'response_data': {'status_code': status, 'body': '{"code":0}'}}]
        if duplicate:
            events.extend(events[2:4])
        (self.root / 'api_logs.log').write_text('\n'.join(map(json.dumps, events)))
        self.case.steps = self.case.steps.replace('DELETE', 'GET')

    def test_marker_selects_test_response_among_discovery_reads(self):
        self.marker_events()
        state, evidence = runner.inspect_evidence(self.root, self.case, 'http://localhost', 'run:1')
        self.assertEqual(state, 'passed')
        self.assertEqual(evidence['correlation']['matchingResponses'], 3)
        self.assertEqual(evidence['correlation']['markedMatchingResponses'], 1)

    def test_marker_never_cherry_picks_a_passing_discovery_response(self):
        self.marker_events(marked_status=401)
        self.assertEqual(runner.inspect_evidence(self.root, self.case, 'http://localhost', 'run:1')[0], 'failed')

    def test_duplicate_or_missing_marker_is_error(self):
        self.marker_events(duplicate=True)
        self.assertEqual(runner.inspect_evidence(self.root, self.case, 'http://localhost', 'run:1')[0], 'error')
        self.marker_events(omit=True)
        self.assertEqual(runner.inspect_evidence(self.root, self.case, 'http://localhost', 'run:1')[0], 'error')

    def test_marker_cannot_override_wrong_target_origin(self):
        self.marker_events()
        self.assertEqual(runner.inspect_evidence(self.root, self.case, 'http://elsewhere', 'run:1')[0], 'error')

    def test_feature_instructs_marker_and_no_inferred_query_writes(self):
        text = runner.feature(self.case, 'http://localhost', True, 'run:1')
        self.assertIn('X-Hercules-Request-Id', text)
        self.assertIn('run:1', text)
        self.assertIn('must not create, modify or delete', text)

    def test_failure_diagnostics_do_not_retain_secrets(self):
        self.report(child='<failure message="Authentication failed for fixture-user password fixture-secret Bearer hidden-token"/>')
        events = [{'request_data': {'method': 'GET', 'url': 'http://localhost/api/contracts/123?token=hidden-token', 'headers': {'Authorization': 'Bearer hidden-token'}}},
                  {'response_data': {'status_code': 401, 'body': '{"password":"fixture-secret"}'}}]
        (self.root / 'api_logs.log').write_text('\n'.join(map(json.dumps, events)))
        state, evidence = runner.inspect_evidence(self.root, self.case)
        self.assertEqual(state, 'failed')
        encoded = json.dumps(evidence)
        for secret in ('fixture-user', 'fixture-secret', 'hidden-token', '/123'):
            self.assertNotIn(secret, encoded)
        self.assertIn('authentication', encoded)
        self.assertIn('contract_detail GET=401', evidence['reason'])

    def test_incomplete_has_http_diagnostics(self):
        self.report('false')
        state, evidence = runner.inspect_evidence(self.root, self.case)
        self.assertEqual(state, 'error')
        self.assertEqual(evidence['diagnostics']['httpExchangeCount'], 1)

    def test_missing_and_duplicate_response(self):
        log = self.root / 'api_logs.log'
        log.write_text(log.read_text() + '\n' + log.read_text())
        self.assertEqual(runner.inspect_evidence(self.root, self.case)[0], 'error')
        log.unlink()
        self.assertEqual(runner.inspect_evidence(self.root, self.case)[0], 'error')


class EngineTests(unittest.TestCase):
    def request(self, rid='test'):
        return runner.Submission(requestId=rid, targetUrl='http://localhost:8080/contract', loginUsername='fixture-user', loginPassword='fixture-secret', cases=[runner.Case(caseId=1, name='case', steps='请求：GET /health\n期望状态码：200')])

    def test_persistence_idempotency_and_secret_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner.Engine, 'execute', return_value=('error', {'reason': 'test stub'})):
            engine = runner.Engine(tmp)
            request = self.request()
            first = engine.submit(request)
            self.assertEqual(first['runId'], engine.submit(request)['runId'])
            request.targetUrl = 'http://localhost:8081'
            with self.assertRaises(HTTPException) as error:
                engine.submit(request)
            self.assertEqual(error.exception.status_code, 409)
            engine.queue.join()
            engine.close()
            engine = runner.Engine(tmp)
            self.assertEqual(engine.get(first['runId'])['state'], 'error')
            engine.close()
            self.assertNotIn(b'fixture-secret', (Path(tmp) / 'jobs.sqlite').read_bytes())
            self.assertNotIn(b'fixture-user', (Path(tmp) / 'jobs.sqlite').read_bytes())

    def test_restart_reconciles_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = runner.Engine(tmp)
            job = {'runId': 'interrupted', 'state': 'running', 'caseResults': []}
            engine.db.execute('INSERT INTO jobs VALUES (?,?,?,?)', ('interrupted', 'r', 'f', json.dumps(job)))
            engine.db.commit()
            engine.close()
            engine = runner.Engine(tmp)
            self.assertEqual(engine.get('interrupted')['state'], 'error')
            engine.close()

    def test_real_child_timeout_and_scratch_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as hc:
            binary = Path(hc) / '.venv/bin/testzeus-hercules'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nsleep 30\n')
            binary.chmod(0o700)
            engine = runner.Engine(tmp)
            with patch.object(runner, 'HERCULES', Path(hc)):
                request = self.request()
                state, _ = engine.execute('timeout', request.cases[0], request, time.monotonic() + 0.15)
                self.assertEqual(state, 'timed_out')
                self.assertEqual(list(Path(tmp).glob('work-*')), [])
            engine.close()

    def test_api_auth_and_validation_do_not_echo_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, 'TOKEN', 'test-token'), patch.object(runner, 'Engine', side_effect=lambda: engine):
            # Engine created via saved class to avoid the patched constructor.
            engine = EngineClass(tmp)
            with TestClient(runner.app) as client:
                self.assertEqual(client.post('/runs', json={}).status_code, 401)
                response = client.post('/runs', headers={'Authorization': 'Bearer test-token'}, json={'loginPassword': 'fixture-secret'})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('fixture-secret', response.text)


EngineClass = runner.Engine
if __name__ == '__main__':
    unittest.main()
