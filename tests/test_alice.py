"""Cloud protocol and durable router job execution; no network or real heaters."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cloud/alice'))
import skill
from yandex_services import Services, ServiceError, owner_authorized


def event(value=22, heater='h2', cap=skill.RANGE, instance='temperature'):
    return {'headers': {'request_id': 'unit-test-01', 'authorization': 'Bearer fixture'},
            'api_version': 1.0, 'request_type': 'action',
            'payload': {'devices': [{'id': heater, 'capabilities': [
                {'type': cap, 'state': {'instance': instance, 'value': value}}]}]}}


def ack(job):
    return {**{k: job[k] for k in ('id', 'heater', 'action', 'value')}, 'ok': True,
            'result': {'encrypted_session': True, 'command_acknowledged': True,
                       'state_echo_matches_request': True,
                       'reported_target': job['value'] if job['action'] == 'set' else 22,
                       'device_mode': job['value'] if job['action'] == 'mode' else 1}}


class FakeServices:
    def __init__(self, transform=lambda data: data):
        self.jobs = []
        self.transform = transform

    def submit(self, job, deadline):
        self.jobs.append(job)

    def acknowledgement(self, identity, deadline):
        return self.transform(ack(next(job for job in self.jobs if job['id'] == identity)))


class AliceProtocolTest(unittest.TestCase):
    def run_request(self, request, services=None, authorized=True):
        return skill.process(request, {'h2': 'Мансарда'}, '123', services or FakeServices(),
                             authorize=lambda *args: authorized)

    def result(self, response):
        return response['payload']['devices'][0]['capabilities'][0]['state']['action_result']

    def test_discovery_is_private_control_only_and_contains_no_device_credentials(self):
        request = event(); request['request_type'] = 'discovery'
        data = self.run_request(request)
        self.assertEqual(data['payload']['user_id'], '123')
        self.assertEqual([d['id'] for d in data['payload']['devices']], ['h2'])
        for cap in data['payload']['devices'][0]['capabilities']:
            self.assertFalse(cap['retrievable'])
        self.assertNotIn('token', json.dumps(data))

    def test_wrong_account_does_not_enqueue_commands(self):
        client = FakeServices()
        response = self.run_request(event(), client, authorized=False)
        self.assertEqual(response['payload']['error_code'], 'ACCOUNT_LINKING_ERROR')
        self.assertEqual(client.jobs, [])

    def test_confirmed_temperature_and_power_changes(self):
        self.assertEqual(self.result(self.run_request(event()))['status'], 'DONE')
        for value in (False, True):
            client = FakeServices()
            response = self.run_request(event(value, cap=skill.POWER, instance='on'), client)
            self.assertEqual(self.result(response)['status'], 'DONE')
            self.assertEqual(client.jobs[0]['action'], 'mode')
            self.assertEqual(client.jobs[0]['value'], int(value))

    def test_missing_encryption_echo_or_matching_target_is_not_success(self):
        def no_echo(data):
            data['result']['state_echo_matches_request'] = False
            return data
        def wrong_target(data):
            data['result']['reported_target'] = 12
            return data
        def wrong_id(data):
            data['id'] = 'f' * 64
            return data
        for transform in (no_echo, wrong_target, wrong_id, lambda data: {'ok': True}):
            response = self.run_request(event(), FakeServices(transform))
            self.assertEqual(self.result(response)['status'], 'ERROR')

    def test_invalid_values_and_unknown_heaters_are_never_queued(self):
        for value in (True, 4, 31, 22.5, '22', None):
            client = FakeServices()
            self.assertEqual(self.result(self.run_request(event(value), client))['error_code'], 'INVALID_VALUE')
            self.assertFalse(client.jobs)
        client = FakeServices()
        response = self.run_request(event(heater='h4'), client)
        self.assertEqual(response['payload']['devices'][0]['action_result']['error_code'], 'DEVICE_NOT_FOUND')
        self.assertFalse(client.jobs)

    def test_relative_temperature_cannot_use_an_unverified_cached_setpoint(self):
        request = event();request['payload']['devices'][0]['capabilities'][0]['state']['relative'] = True
        client = FakeServices()
        self.assertEqual(self.result(self.run_request(request, client))['error_code'], 'INVALID_ACTION')
        self.assertFalse(client.jobs)

    def test_retries_have_stable_ids_and_jobs_expire_quickly(self):
        first, second = FakeServices(), FakeServices()
        self.run_request(event(), first);self.run_request(event(), second)
        self.assertEqual(first.jobs[0]['id'], second.jobs[0]['id'])
        self.assertLess(first.jobs[0]['expires_at'] - time.time(), 2.4)
        self.assertGreater(first.jobs[0]['expires_at'], time.time())

    def test_no_acknowledgement_is_an_error_without_exceeding_deadline(self):
        ticks = [100.0]
        class Missing(FakeServices):
            def acknowledgement(self, identity, deadline):
                ticks[0] = deadline
                return None
        with patch('skill.time.monotonic', side_effect=lambda: ticks[0]), patch('skill.time.sleep'):
            self.assertEqual(self.result(self.run_request(event(), Missing()))['status'], 'ERROR')


class CloudHttpsTest(unittest.TestCase):
    def test_handler_accepts_raw_json_text_and_bytes_and_rejects_bad_or_large_input(self):
        diagnostic = {'heatbridge_diagnostic': 'expired_queue_probe'}
        for raw in (json.dumps(diagnostic), json.dumps(diagnostic).encode(),
                    bytearray(json.dumps(diagnostic).encode())):
            with patch('diagnostics.probe', return_value={'diagnostic': 'fixture'}) as probe:
                self.assertEqual(skill.handler(raw, None), {'diagnostic': 'fixture'})
                probe.assert_called_once_with()
        settings = {'HEATBRIDGE_OWNER_LOGIN': 'owner', 'HEATBRIDGE_DEVICES_JSON': '{"h2":"Мансарда"}'}
        with patch.dict(os.environ, settings, clear=True), \
             patch('skill.from_environment', return_value=FakeServices()), \
             patch('skill.process', return_value={'fixture': True}) as process:
            request = event()
            self.assertEqual(skill.handler(json.dumps(request).encode(), None), {'fixture': True})
            self.assertEqual(process.call_args.args[0], request)
        for raw in ('{broken', b'\xff', ' ' * 16001):
            self.assertEqual(skill.handler(raw, None)['payload']['error_code'], 'INVALID_ACTION')

    def test_console_probe_submits_only_expired_job_and_reports_safe_failures(self):
        client = FakeServices()
        client.acknowledgement = lambda *args: None
        with patch('diagnostics.from_environment', return_value=client):
            result = skill.handler({'heatbridge_diagnostic': 'expired_queue_probe'}, None)
        self.assertFalse(result['heater_control'])
        self.assertEqual(result['queue_send']['status'], 'OK')
        self.assertEqual(result['ack_read'], {'status': 'OK', 'object_found': False})
        self.assertEqual(len(client.jobs), 1)
        self.assertEqual(client.jobs[0]['expires_at'], 0)
        with patch('diagnostics.from_environment', side_effect=KeyError('fixture-secret')):
            self.assertEqual(skill.handler({'heatbridge_diagnostic': 'expired_queue_probe'}, None)
                             ['configuration'], 'invalid')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(skill.handler({'heatbridge_diagnostic': 'expired_queue_probe',
                                           'expires_at': time.time()+1}, None)
                             ['payload']['error_code'], 'INTERNAL_ERROR')

    def test_submit_failure_logs_stage_and_cloud_code_without_credentials(self):
        for code in ('AccessDeniedException', 'fixture-secret'):
            def opener(req, timeout):
                body = ('<ErrorResponse><Error><Code>' + code + '</Code>'
                        '<Message>fixture-secret Bearer fixture</Message></Error></ErrorResponse>')
                raise urllib.error.HTTPError(req.full_url, 400, 'fixture-secret', {},
                                             io.BytesIO(body.encode()))
            client = Services('https://message-queue.api.cloud.yandex.net/folder/id/jobs',
                              'private-bucket', 'fixture-key', 'fixture-secret', opener)
            output = io.StringIO()
            with patch('sys.stdout', output):
                response = skill.process(event(), {'h2': 'Мансарда'}, '123', client,
                                         authorize=lambda *args: True)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            failure = next(record for record in records if record['stage'] == 'queue_submit_failed')
            self.assertEqual(failure['http_status'], 400)
            self.assertEqual(failure['service'], 'sqs')
            self.assertEqual(failure.get('cloud_code'), code if code == 'AccessDeniedException' else None)
            self.assertNotIn('fixture-secret', output.getvalue())
            self.assertNotIn('fixture-key', output.getvalue())
            self.assertNotIn('Bearer fixture', output.getvalue())
            self.assertEqual(response['payload']['devices'][0]['capabilities'][0]['state']
                             ['action_result']['status'], 'ERROR')

    def test_authorization_failure_is_logged_without_token(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            response = skill.process(event(), {'h2': 'Мансарда'}, '123', FakeServices(),
                                     authorize=lambda *args: False)
        self.assertEqual(json.loads(output.getvalue())['stage'], 'authorization_failed')
        self.assertNotIn('fixture', output.getvalue())
        self.assertEqual(response['payload']['error_code'], 'ACCOUNT_LINKING_ERROR')

    def test_owner_login_is_explicit_and_uid_takes_priority(self):
        self.assertEqual(skill.owner_identifier({'HEATBRIDGE_OWNER_LOGIN': 'Owner.Name'}), 'login:owner.name')
        self.assertEqual(skill.owner_identifier({'HEATBRIDGE_OWNER_LOGIN': 'Owner.Name', 'HEATBRIDGE_OWNER_UID': '123'}), '123')
        for settings in ({}, {'HEATBRIDGE_OWNER_LOGIN': 'owner@yandex.ru'},
                         {'HEATBRIDGE_OWNER_UID': 'invalid', 'HEATBRIDGE_OWNER_LOGIN': 'owner'}):
            with self.assertRaises(ValueError):
                skill.owner_identifier(settings)

    def test_login_authorization_checks_actual_yandex_identity_and_rejects_invalid_info(self):
        deadline = time.monotonic() + 2
        def info(data):
            return lambda req, timeout: io.BytesIO(json.dumps(data).encode())
        self.assertTrue(owner_authorized('Bearer fixture', 'login:owner.name', deadline,
                                        info({'id': '123', 'login': 'Owner.Name'})))
        self.assertFalse(owner_authorized('Bearer fixture', 'login:owner.name', deadline,
                                         info({'id': '999', 'login': 'another.owner'})))
        for data in ({'login': 'owner.name'}, [], {'id': '123'}, {'id': 'invalid', 'login': 'owner.name'}):
            self.assertFalse(owner_authorized('Bearer fixture', 'login:owner.name', deadline, info(data)))

    def test_authentication_checks_yandex_uid_not_just_token_presence(self):
        captured = []
        def opener(req, timeout):
            captured.append(req)
            return io.BytesIO(b'{"id":"123"}')
        deadline = time.monotonic() + 2
        self.assertTrue(owner_authorized('Bearer fixture', '123', deadline, opener))
        self.assertFalse(owner_authorized('Bearer fixture', '999', deadline, opener))
        self.assertFalse(owner_authorized('Bearer token\r\nInjected: value', '123', deadline, opener))
        self.assertEqual(captured[0].get_header('Authorization'), 'OAuth fixture')

    def test_queue_uses_signed_query_protocol_and_base64_message(self):
        calls = []
        def opener(req, timeout):
            calls.append(req)
            return io.BytesIO(b'<SendMessageResponse xmlns="test"><SendMessageResult><MessageId>1</MessageId></SendMessageResult></SendMessageResponse>')
        client = Services('https://message-queue.api.cloud.yandex.net/folder/id/jobs', 'private-bucket', 'fixture-key', 'fixture-secret', opener)
        client.submit({'id': 'a' * 64, 'heater': 'h2'}, time.monotonic()+2)
        fields = urllib.parse.parse_qs(calls[0].data.decode())
        self.assertEqual(fields['Action'], ['SendMessage'])
        self.assertEqual(fields['Version'], ['2012-11-05'])
        self.assertNotIn('fixture-secret', str(calls[0].headers))
        self.assertIn('AWS4-HMAC-SHA256', calls[0].get_header('Authorization'))

    def test_missing_ack_is_pending_but_access_denied_is_an_error(self):
        def fail(code):
            def opener(req, timeout):
                raise urllib.error.HTTPError(req.full_url, code, 'fixture', {}, None)
            return opener
        args = ('https://message-queue.api.cloud.yandex.net/folder/id/jobs', 'private-bucket', 'fixture-key', 'fixture-secret')
        self.assertIsNone(Services(*args, fail(404)).acknowledgement('a'*64, time.monotonic()+2))
        with self.assertRaises(ServiceError):
            Services(*args, fail(403)).acknowledgement('a'*64, time.monotonic()+2)


class RouterWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Make the host jq regex engine unusable, as in the owner's Entware build.
        cls.regex_tmp = tempfile.TemporaryDirectory()
        root = Path(cls.regex_tmp.name)
        source = root/'no-regex.c'
        source.write_text('int onig_new(void *unused, ...){(void)unused;return -5;}\n')
        cls.no_regex = root/'no-regex.so'
        subprocess.run(['gcc', '-shared', '-fPIC', '-Os', str(source), '-o', str(cls.no_regex)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.regex_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base/'etc/heatbridge').mkdir(parents=True)
        (self.base/'etc/heatbridge/config.json').write_text(json.dumps({'heaters': {'h2': {'verified': True}}}))
        (self.base/'bin').mkdir()
        worker = self.base/'bin/heatbridge-device'
        worker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$HEATBRIDGE_PREFIX/calls"
if [ -f "$HEATBRIDGE_PREFIX/fail" ]; then exit 1; fi
printf '{"encrypted_session":true,"command_acknowledged":true,"state_echo_matches_request":true,"reported_target":22,"device_mode":1}\\n'
''');worker.chmod(0o755)

    def job(self, **changes):
        return {'id': 'a'*64, 'heater': 'h2', 'action': 'set', 'value': 22, 'expires_at': time.time()+2.3, **changes}

    def call(self, job):
        return subprocess.run(['sh', str(ROOT/'cloud/router/heatbridge-alice-worker')],
                              input=json.dumps(job), text=True, capture_output=True,
                              env={**os.environ, 'HEATBRIDGE_PREFIX': str(self.base)})

    def test_confirmed_job_runs_once_and_persists_private_claim(self):
        job = self.job()
        first, duplicate = self.call(job), self.call(job)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)['ok'])
        self.assertEqual(first.stdout, duplicate.stdout)
        self.assertEqual((self.base/'calls').read_text().splitlines(), ['set h2 22'])
        claim = self.base/'var/lib/heatbridge-alice/requests'/job['id']
        self.assertEqual(claim.stat().st_mode & 0o777, 0o700)
        self.assertEqual((claim/'result.json').stat().st_mode & 0o777, 0o600)

    def test_expired_jobs_invalid_ids_or_values_never_reach_hardware(self):
        for changes in ({'expires_at': time.time()-1}, {'expires_at': time.time()+60},
                        {'id': '../../etc/passwd'}, {'heater': 'h5'}, {'value': 31},
                        {'id': 'a'*63}, {'id': 'A'*64}, {'id': 'g'*64},
                        {'value': True}, {'action': 'reboot'}, {'action': 'mode', 'value': 3}):
            self.assertNotEqual(self.call(self.job(**changes)).returncode, 0)
        self.assertFalse((self.base/'calls').exists())

    def test_failed_or_interrupted_claim_is_not_retried(self):
        (self.base/'fail').touch()
        job = self.job()
        self.assertFalse(json.loads(self.call(job).stdout)['ok'])
        (self.base/'fail').unlink()
        self.assertFalse(json.loads(self.call(job).stdout)['ok'])
        self.assertEqual(len((self.base/'calls').read_text().splitlines()), 1)
        claim = self.base/'var/lib/heatbridge-alice/requests'/('b'*64)
        claim.mkdir()
        result = self.call(self.job(id='b'*64))
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertEqual(len((self.base/'calls').read_text().splitlines()), 1)

    def test_unverified_heater_is_not_controlled(self):
        result = self.call(self.job(heater='h1'))
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertFalse((self.base/'calls').exists())

    def agent(self, delete_ok=True, config_changes=None):
        config = {'queue_url': 'https://message-queue.api.cloud.yandex.net/folder/id/jobs',
                  'ack_bucket': 'private-bucket', 'access_key': 'fixture-access-key',
                  'secret_key': 'fixture-secret-key'}
        config.update(config_changes or {})
        (self.base/'etc/heatbridge/alice.json').write_text(json.dumps(config))
        wrapper = self.base/'bin/heatbridge-alice-worker'
        wrapper.write_text('#!/bin/sh\nexec sh "'+str(ROOT/'cloud/router/heatbridge-alice-worker')+'"\n')
        wrapper.chmod(0o755)
        encoded = base64.b64encode(json.dumps(self.job()).encode()).decode()
        (self.base/'reply.xml').write_text('<ReceiveMessageResponse><ReceiveMessageResult><Message>'
          '<ReceiptHandle>EAEgrOGOhogtKAA</ReceiptHandle><Body>'+encoded+'</Body>'
          '</Message></ReceiveMessageResult></ReceiveMessageResponse>')
        mock = '''
curl() {
 case "$*" in
  *'--help all'*) printf '%s\\n' '--aws-sigv4';;
  *Action=ReceiveMessage*)
    [ ! -f "$HEATBRIDGE_PREFIX/polled" ] || exit 0
    touch "$HEATBRIDGE_PREFIX/polled"
    cat "$HEATBRIDGE_PREFIX/reply.xml";;
  *Action=DeleteMessage*) DELETE_RESULT;;
  *aws:amz:ru-central1:s3*)
    cp "$HEATBRIDGE_ALICE_RUNTIME/ack.json" "$HEATBRIDGE_PREFIX/uploaded.json";;
  *) exit 9;;
 esac
}
. "AGENT_PATH"
'''.replace('DELETE_RESULT', "printf '<DeleteMessageResponse/>'" if delete_ok else 'return 28')
        mock = mock.replace('AGENT_PATH', str(ROOT/'cloud/router/heatbridge-alice'))
        return subprocess.run(['sh', '-c', mock], capture_output=True, text=True, timeout=3,
          env={**os.environ, 'HEATBRIDGE_PREFIX': str(self.base),
               'HEATBRIDGE_ALICE_RUNTIME': str(self.base/'runtime')})

    def test_agent_receives_deletes_executes_and_uploads_correlated_ack(self):
        result = self.agent()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads((self.base/'uploaded.json').read_text())['ok'])
        self.assertEqual((self.base/'calls').read_text().splitlines(), ['set h2 22'])
        self.assertNotIn('fixture-secret-key', result.stdout+result.stderr)
        self.assertEqual((self.base/'runtime/curl.conf').stat().st_mode & 0o777, 0o600)

    def test_agent_never_controls_heater_when_queue_delete_failed(self):
        result = self.agent(delete_ok=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.base/'calls').exists())
        self.assertFalse((self.base/'uploaded.json').exists())

    def test_agent_and_worker_work_with_unusable_jq_regex_engine(self):
        with patch.dict(os.environ, {'LD_PRELOAD': str(self.no_regex)}):
            control = subprocess.run(['jq', '-n', '"fixture"|test("fixture")'], capture_output=True)
            self.assertNotEqual(control.returncode, 0, 'Regex failure injection was ineffective')
            result = self.agent()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads((self.base/'uploaded.json').read_text())['ok'])
        self.assertEqual((self.base/'calls').read_text().splitlines(), ['set h2 22'])

    def test_agent_rejects_unsafe_cloud_configuration_before_hardware_io(self):
        for changes in (
            {'queue_url': 'http://message-queue.api.cloud.yandex.net/folder/id/jobs'},
            {'queue_url': 'https://message-queue.api.cloud.yandex.net.evil.example/jobs'},
            {'queue_url': 'https://message-queue.api.cloud.yandex.net/'},
            {'ack_bucket': 'UPPERCASE'}, {'ack_bucket': '-invalid'}, {'ack_bucket': 'invalid.'},
            {'ack_bucket': 'a'*64}, {'ack_bucket': None},
            {'access_key': 'short'}, {'secret_key': 'quote"injection'},
            {'secret_key': 'newline\ninjection'}, {'secret_key': 'а'*40},
        ):
            with self.subTest(changes=changes):
                result = self.agent(config_changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.base/'calls').exists())
                self.assertFalse((self.base/'uploaded.json').exists())


if __name__ == '__main__':
    unittest.main()
