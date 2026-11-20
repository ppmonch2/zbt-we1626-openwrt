"""Disk cloud/router contract against an isolated WebDAV filesystem, no heaters/network."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'cloud/alice'))
from disk_services import DiskServices
from yandex_services import ServiceError, from_environment
import skill
from test_alice import ack, event


class FileConnection:
    sock = None

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.forced_status = None

    def close(self):
        pass

    def request(self, method, path, body=b'', headers=None):
        self.calls.append((method, path, body, headers))
        self.last = method, path, body

    def getresponse(self):
        method, path, body = self.last
        target = self.root/path.lstrip('/')
        status, data = 200, b''
        if self.forced_status:
            status, data = self.forced_status, b'fixture-secret server body'
        elif method == 'PUT':
            if not target.parent.exists():
                status = 409
            else:
                temp = target.with_suffix('.new')
                temp.write_bytes(body)
                temp.replace(target)
                status = 201
        elif method == 'GET':
            if target.exists(): data = target.read_bytes()
            else: status = 404
        elif method == 'DELETE':
            if target.exists(): target.unlink(); status = 204
            else: status = 404
        response = io.BytesIO(data)
        response.status = status
        return response


class DiskCloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('commands', 'acks', 'probes'):
            (self.root/'heatbridge-v1'/name).mkdir(parents=True)
        self.connection = FileConnection(self.root)
        self.client = DiskServices('fixture-user', 'fixture-password', connection=self.connection)

    def test_batch_uses_one_private_put_and_one_ack_read_for_four_devices(self):
        jobs = [{'id': str(i)*64, 'heater': f'h{i}', 'action': 'set', 'value': 22,
                 'expires_at': time.time()+2} for i in range(1, 5)]
        self.client.submit_many(jobs, time.monotonic()+2)
        self.assertEqual(len(self.connection.calls), 1)
        method, path, body, headers = self.connection.calls[0]
        self.assertEqual(method, 'PUT')
        self.assertEqual(json.loads(body)['jobs'], jobs)
        self.assertTrue(headers['Authorization'].startswith('Basic '))
        self.assertNotIn('fixture-password', path+body.decode())
        target = self.root/'heatbridge-v1/acks'/f'{self.client.batch}.json'
        target.write_text(json.dumps({'batch': self.client.batch, 'acks': {j['id']: ack(j) for j in jobs}}))
        self.assertEqual(len(self.client.acknowledgements(time.monotonic()+2)), 4)
        self.assertEqual(len(self.connection.calls), 2)

    def test_redirect_and_auth_failure_never_follow_or_leak_secret(self):
        for status in (301, 302, 401, 403, 429, 507):
            self.connection.forced_status = status
            with self.assertRaises(ServiceError) as caught:
                self.client.state_snapshot(time.monotonic()+1)
            self.assertEqual(caught.exception.http_status, status)
            self.assertNotIn('fixture', str(caught.exception))
        self.assertEqual(len(self.connection.calls), 6)

    def test_invalid_credentials_paths_and_deadline_rejected_before_request(self):
        for changes in ({'login': 'user:password'}, {'password': 'bad\nheader'},
                        {'folder': '../escape'}, {'folder': 'folder/path'}):
            args = {'login': 'owner', 'password': 'fixture-password', 'folder': 'heatbridge-v1'}
            with self.assertRaises(ValueError): DiskServices(**{**args, **changes})
        with self.assertRaises(ValueError):
            self.client.request('GET', '//evil.example/', time.monotonic()+1)
        with self.assertRaises(ServiceError): self.client.state_snapshot(time.monotonic()-1)
        self.assertEqual(self.connection.calls, [])

    def test_missing_malformed_oversized_and_wrong_batch_ack_is_not_success(self):
        self.client.batch = '1234567890-'+'a'*64
        self.assertEqual(self.client.acknowledgements(time.monotonic()+1), {})
        target = self.root/'heatbridge-v1/acks'/f'{self.client.batch}.json'
        for content in ('broken', '{"batch":"wrong","acks":{}}', 'x'*65537):
            target.write_text(content)
            with self.assertRaises(ServiceError): self.client.acknowledgements(time.monotonic()+1)

    def test_probe_validates_write_and_read_without_unnecessary_delete(self):
        result = self.client.probe(time.monotonic()+2)
        self.assertEqual(result['status'], 'OK')
        self.assertTrue(result['write_read'])
        self.assertFalse(result['heater_control'])
        self.assertEqual([call[0] for call in self.connection.calls], ['PUT', 'GET'])
        self.assertTrue(all('/probes/cloud-check.json' in call[1] for call in self.connection.calls))
        probe_file = self.root/'heatbridge-v1/probes/cloud-check.json'
        original_nonce = json.loads(probe_file.read_text())['probe']
        self.client.probe(time.monotonic()+2)
        self.assertNotEqual(json.loads(probe_file.read_text())['probe'], original_nonce)
        self.assertEqual(len(list((self.root/'heatbridge-v1/probes').iterdir())), 1)

    def test_probe_succeeds_even_if_webdav_delete_is_unavailable(self):
        class DeleteUnavailable(FileConnection):
            def getresponse(self):
                if self.last[0] == 'DELETE':
                    raise TimeoutError('delete is slow')
                return super().getresponse()
        connection = DeleteUnavailable(self.root)
        client = DiskServices('fixture-user', 'fixture-password', connection=connection)
        with patch('skill.from_environment', return_value=client):
            result = skill.handler({'heatbridge_diagnostic': 'disk_probe'}, None)
        self.assertEqual(result['status'], 'OK')
        self.assertTrue(result['write_read'])
        self.assertEqual([call[0] for call in connection.calls], ['PUT', 'GET'])

    def test_probe_diagnostic_identifies_write_or_read_failure(self):
        self.connection.forced_status = 403
        with patch('skill.from_environment', return_value=self.client):
            result = skill.handler({'heatbridge_diagnostic': 'disk_probe'}, None)
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['phase'], 'write')
        self.assertEqual(result['http_status'], 403)
        self.assertNotIn('fixture-password', json.dumps(result))

        class TimeoutOnRead(FileConnection):
            def getresponse(self):
                if self.last[0] == 'GET':
                    raise TimeoutError('simulated WebDAV timeout')
                return super().getresponse()

        client = DiskServices('fixture-user', 'fixture-password',
                              connection=TimeoutOnRead(self.root))
        with patch('skill.from_environment', return_value=client):
            result = skill.handler({'heatbridge_diagnostic': 'disk_probe'}, None)
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['phase'], 'read')
        self.assertEqual(result['reason'], 'cloud_connection_error')
        self.assertEqual(result['network_stage'], 'response')
        self.assertEqual(result['network_kind'], 'TimeoutError')
        self.assertGreaterEqual(result['elapsed_ms'], 0)
        self.assertNotIn('fixture-password', json.dumps(result))

    def test_environment_selects_disk_without_aws_credentials(self):
        with patch.dict(os.environ, {'HEATBRIDGE_TRANSPORT': 'disk',
                'HEATBRIDGE_DISK_LOGIN': 'owner', 'HEATBRIDGE_DISK_APP_PASSWORD': 'fixture-password'}, clear=True):
            self.assertIsInstance(from_environment(), DiskServices)
        with patch.dict(os.environ, {'HEATBRIDGE_TRANSPORT': 'unknown'}, clear=True):
            with self.assertRaises(ValueError): from_environment()

    def test_group_provider_submits_once_and_requires_individual_confirmations(self):
        class BatchClient:
            def submit_many(self, jobs, deadline): self.jobs = jobs
            def acknowledgements(self, deadline):
                result = {j['id']: ack(j) for j in self.jobs}
                result[self.jobs[1]['id']]['ok'] = False
                return result
        client = BatchClient()
        request = event()
        request['payload']['devices'] = [event(heater=f'h{i}')['payload']['devices'][0] for i in range(1, 5)]
        response = skill.process(request, {f'h{i}': str(i) for i in range(1, 5)}, '123', client,
                                 authorize=lambda *args: True)
        self.assertEqual(len(client.jobs), 4)
        self.assertEqual([d['capabilities'][0]['state']['action_result']['status']
                          for d in response['payload']['devices']], ['DONE', 'ERROR', 'DONE', 'DONE'])

    def test_scene_setpoint_and_power_for_one_heater_are_batched_and_confirmed(self):
        class SceneClient:
            def submit_many(self, jobs, deadline): self.jobs = jobs
            def acknowledgements(self, deadline):
                return {job['id']: ack(job) for job in self.jobs}
        request = event(value=19)
        request['payload']['devices'][0]['capabilities'].append({
            'type': skill.POWER, 'state': {'instance': 'on', 'value': True}})
        client = SceneClient()
        response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                 authorize=lambda *args: True)
        self.assertEqual([(job['action'], job['value']) for job in client.jobs],
                         [('set', 19), ('mode', 1)])
        capabilities = response['payload']['devices'][0]['capabilities']
        self.assertEqual([item['state']['action_result']['status'] for item in capabilities],
                         ['DONE', 'DONE'])

    def test_status_diagnostic_reads_real_sensor_and_setpoint_without_commands(self):
        from test_alice_state import snapshot
        target = self.root/'heatbridge-v1/acks/state-v1.json'
        target.write_text(json.dumps({'schema': 1, 'published_at': time.time(),
                                     'devices': {'h2': snapshot(target=24, measured=18.5)}}))
        with patch('skill.from_environment', return_value=self.client):
            result = skill.handler({'heatbridge_diagnostic': 'disk_status'}, None)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['devices']['h2']['capabilities'][1]['state']['value'], 24)
        self.assertEqual(result['devices']['h2']['properties'][0]['state']['value'], 18.5)
        self.assertEqual(result['devices']['h1']['error_code'], 'DEVICE_UNREACHABLE')
        self.assertEqual([call[0] for call in self.connection.calls], ['GET'])


FAKE_CURL = r'''#!/usr/bin/env python3
import json,os,pathlib,sys,time
from urllib.parse import urlsplit
args=sys.argv[1:]
def option(name,default=None):
 return args[args.index(name)+1] if name in args else default
root=pathlib.Path(os.environ['FAKE_DISK'])
url=args[-1]
assert urlsplit(url).hostname=='webdav.yandex.ru'
assert args[0]=='-q' and option('--noproxy')=='*' and option('--proto')=='=https'
assert '-k' not in args and '-L' not in args
assert 'fixture-password' not in ' '.join(args)
path=urlsplit(url).path
target=root/path.lstrip('/')
method=option('-X','GET')
status=200;data=b''
if (root/'failure').exists():status=403
elif method=='PUT' and '/acks/' in path and (root/'ack-failure').exists():
 (root/'ack-attempt').write_text('failed');status=403
elif method=='MKCOL':
 if target.exists():status=405
 else:
  try:target.mkdir();status=201
  except OSError:status=409
elif method=='PROPFIND':
 if target.is_dir():
  hrefs=['https://evil.example/heatbridge-v1/commands/'+'0'*10+'-'+'a'*64+'.json']
  hrefs += ['/'+str(p.relative_to(root)) for p in target.iterdir()]
  data=('<d:multistatus xmlns:d="DAV:">'+''.join('<d:response><d:href>'+h+'</d:href></d:response>' for h in hrefs)+'</d:multistatus>').encode();status=207
 else:status=404
elif method=='GET':
 if target.is_file():data=target.read_bytes()
 else:status=404
elif method=='PUT':
 body=option('--data-binary')
 assert body.startswith('@')
 if not target.parent.is_dir():status=409
 else:
  temp=target.with_name(target.name+'.'+str(os.getpid())+'.tmp')
  temp.write_bytes(pathlib.Path(body[1:]).read_bytes());temp.replace(target);status=201
elif method=='DELETE':
 if target.is_file():target.unlink();status=204
 else:status=404
else:raise SystemExit(9)
pathlib.Path(option('-o')).write_bytes(data)
print(status,end='')
'''


class DiskRouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for name in ('bin', 'etc/heatbridge', 'etc/init.d', 'runtime', 'remote'):
            (self.base/name).mkdir(parents=True)
        self.env = {**os.environ, 'HEATBRIDGE_PREFIX': str(self.base),
                    'HEATBRIDGE_ALICE_RUNTIME': str(self.base/'runtime'),
                    'HEATBRIDGE_ALICE_PIDFILE': str(self.base/'agent.pid'),
                    'FAKE_DISK': str(self.base/'remote')}
        for path in (ROOT/'cloud/router').iterdir():
            if path.name == 'install.sh': continue
            target = self.base/('etc/init.d' if path.name.startswith('S85') else 'bin')/path.name
            text = path.read_text().replace('#!/opt/bin/sh', '#!/bin/sh', 1)
            text = text.replace('export PATH=', f'export PATH={self.base}/bin:')
            target.write_text(text); target.chmod(0o755)
        # No background physical observations are needed for command-path tests.
        (self.base/'bin/heatbridge-alice-state').write_text('#!/bin/sh\nexit 0\n')
        (self.base/'bin/curl').write_text(FAKE_CURL)
        (self.base/'bin/curl').chmod(0o755)
        self.config = {'login': 'fixture-user', 'password': 'fixture-password',
                       'folder': 'heatbridge-v1', 'enabled': False}
        (self.base/'etc/heatbridge/disk.json').write_text(json.dumps(self.config))
        (self.base/'etc/heatbridge/config.json').write_text(json.dumps({'lan_ip': '192.168.1.1',
            'heaters': {f'h{i}': {'verified': True} for i in range(1, 5)}}))
        device = self.base/'bin/heatbridge-device'
        device.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys,time
p=pathlib.Path(os.environ['HEATBRIDGE_PREFIX'])
action,heater,value=sys.argv[1:]
with (p/'device-calls').open('a') as f:f.write(json.dumps([heater,action,int(value),time.time()])+'\\n')
time.sleep(.12)
print(json.dumps({'encrypted_session':True,'command_acknowledged':True,'state_echo_matches_request':True,
 'reported_target':int(value) if action=='set' else 22,'device_mode':int(value) if action=='mode' else 1}))
''')
        device.chmod(0o755)
        for name in ('commands', 'acks', 'probes'):
            (self.base/'remote/heatbridge-v1'/name).mkdir(parents=True)

    def control(self, action, **values):
        result = subprocess.run([str(self.base/'bin/heatbridge-disk-control')],
            input=json.dumps({'action': action, **values}), text=True, capture_output=True,
            env=self.env, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fixture-password', result.stdout+result.stderr)
        return json.loads(result.stdout)

    def batch(self, jobs):
        batch = f'{int(time.time())}-'+'a'*64
        path = self.base/'batch.json'
        path.write_text(json.dumps({'schema': 1, 'batch': batch, 'jobs': jobs}))
        return subprocess.run([str(self.base/'bin/heatbridge-disk-batch'), str(path), batch],
                              capture_output=True, text=True, env=self.env, timeout=5)

    def jobs(self, expiry=None):
        return [{'id': str(i)*64, 'heater': f'h{i}', 'action': 'set', 'value': 20+i,
                 'expires_at': expiry if expiry is not None else time.time()+2.7} for i in range(1, 5)]

    def test_four_heaters_execute_concurrently_and_duplicates_only_return_saved_acks(self):
        jobs = self.jobs()
        result = self.batch(jobs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(a['ok'] for a in json.loads(result.stdout)['acks'].values()))
        calls = [json.loads(line) for line in (self.base/'device-calls').read_text().splitlines()]
        self.assertEqual({line[0] for line in calls}, {'h1', 'h2', 'h3', 'h4'})
        self.assertLess(max(c[3] for c in calls)-min(c[3] for c in calls), .3)
        for job in jobs: job['expires_at'] = 0
        self.assertEqual(len(json.loads(self.batch(jobs).stdout)['acks']), 4)
        self.assertEqual(len((self.base/'device-calls').read_text().splitlines()), 4)

    def test_expired_and_invalid_batches_never_control_heaters(self):
        self.assertEqual(json.loads(self.batch(self.jobs(0)).stdout)['acks'], {})
        for changes in ({'id': '../escape'}, {'heater': 'h5'}, {'action': 'reboot'}):
            jobs = self.jobs(); jobs[0].update(changes)
            self.assertNotEqual(self.batch(jobs).returncode, 0)
        jobs = self.jobs(); jobs.append(jobs[0])
        self.assertNotEqual(self.batch(jobs).returncode, 0)
        self.assertFalse((self.base/'device-calls').exists())

    def test_webdav_batches_enforce_actual_10_to_35_range(self):
        for value in (10, 31, 35):
            job = self.jobs()[0]
            job['id'] = f'{value:064x}'
            job['value'] = value
            result = self.batch([job])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['acks'][job['id']]['ok'])
            self.assertEqual(json.loads(result.stdout)['acks'][job['id']]['value'], value)
        for value in (9, 36):
            job = self.jobs()[0]
            job['id'] = str(value % 10) * 64
            job['value'] = value
            result = self.batch([job])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['acks'])

    def test_same_heater_power_and_temperature_stay_ordered(self):
        jobs = [self.jobs()[0], {**self.jobs()[0], 'id': 'f'*64, 'action': 'mode', 'value': 1}]
        result = self.batch(jobs)
        self.assertEqual(len(json.loads(result.stdout)['acks']), 2)
        calls = [json.loads(line) for line in (self.base/'device-calls').read_text().splitlines()]
        self.assertEqual([c[1] for c in calls], ['set', 'mode'])

    def test_settings_save_is_private_and_network_check_never_touches_heaters(self):
        result = self.control('save', **self.config)
        self.assertFalse(result['enabled'])
        self.assertEqual(result['message'], 'saved_not_started')
        self.assertNotIn('password', result)
        self.assertEqual((self.base/'etc/heatbridge/disk.json').stat().st_mode & 0o777, 0o600)
        result = self.control('check')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['message'], 'write_read_delete_ok')
        self.assertEqual(list((self.base/'remote/heatbridge-v1/probes').iterdir()), [])
        self.assertFalse((self.base/'device-calls').exists())
        self.assertEqual((self.base/'runtime/disk/curl.conf').stat().st_mode & 0o777, 0o600)
        (self.base/'remote/failure').touch()
        self.assertEqual(self.control('check')['error'], 'webdav_http_403')
        self.assertEqual(self.control('start')['error'], 'check_first')

    def test_start_requires_check_and_bad_input_does_not_replace_settings(self):
        self.assertEqual(self.control('start')['error'], 'check_first')
        before = (self.base/'etc/heatbridge/disk.json').read_bytes()
        self.assertEqual(self.control('save', **{**self.config, 'password': 'bad\nconfig'})['error'],
                         'invalid_credentials')
        self.assertEqual((self.base/'etc/heatbridge/disk.json').read_bytes(), before)

    def test_start_status_stop_recognizes_dispatched_disk_process(self):
        self.assertTrue(self.control('check')['ok'])
        try:
            result = self.control('start')
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['enabled'])
            self.assertTrue(result['running'])
            self.assertTrue(self.control('status')['running'])
        finally:
            self.assertFalse(self.control('stop')['running'])

    def test_end_to_end_cloud_file_agent_four_heaters_ack_and_command_deletion(self):
        self.config['enabled'] = True
        (self.base/'etc/heatbridge/disk.json').write_text(json.dumps(self.config))
        agent = subprocess.Popen([str(self.base/'bin/heatbridge-alice')], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            client = DiskServices('fixture-user', 'fixture-password',
                                  connection=FileConnection(self.base/'remote'))
            jobs = self.jobs()
            client.submit_many(jobs, time.monotonic()+2)
            until = time.monotonic()+4
            acknowledgements = {}
            while time.monotonic() < until:
                acknowledgements = client.acknowledgements(time.monotonic()+1)
                if len(acknowledgements) == 4: break
                time.sleep(.02)
            self.assertEqual(len(acknowledgements), 4)
            self.assertTrue(all(skill.confirmed(acknowledgements[j['id']], j) for j in jobs))
            until = time.monotonic()+1
            while list((self.base/'remote/heatbridge-v1/commands').iterdir()) and time.monotonic() < until:
                time.sleep(.02)
            self.assertEqual(list((self.base/'remote/heatbridge-v1/commands').iterdir()), [])
            self.assertEqual(len((self.base/'device-calls').read_text().splitlines()), 4)
        finally:
            agent.terminate()
            _, errors = agent.communicate(timeout=5)
            self.assertNotIn(b'fixture-password', errors)

    def test_cgi_rejects_wrong_origin_before_saving_password(self):
        data = json.dumps({'action': 'save', **self.config})
        script = (ROOT/'entware/www/cgi-bin/disk-api').read_text().replace('export PATH=',
                 f'export PATH={self.base}/bin:')
        response = subprocess.run(['sh', '-c', script], input=data, text=True, capture_output=True,
            env={**self.env, 'REQUEST_METHOD': 'POST', 'HTTP_HOST': '192.168.1.1:8765',
                 'HTTP_ORIGIN': 'https://evil.example', 'CONTENT_TYPE': 'application/json',
                 'CONTENT_LENGTH': str(len(data))})
        self.assertIn('invalid_origin', response.stdout)
        self.assertNotIn('fixture-password', response.stdout)

    def test_failed_ack_upload_can_recover_without_repeating_expired_command(self):
        (self.base/'remote/ack-failure').touch()
        agent = subprocess.Popen([str(self.base/'bin/heatbridge-disk-agent')], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            client = DiskServices('fixture-user', 'fixture-password',
                                  connection=FileConnection(self.base/'remote'))
            job = self.jobs()[0]
            client.submit_many([job], time.monotonic()+2)
            until = time.monotonic()+3
            while not (self.base/'remote/ack-attempt').exists() and time.monotonic() < until:
                time.sleep(.02)
            self.assertTrue((self.base/'remote/ack-attempt').exists())
            command = self.base/'remote/heatbridge-v1/commands'/f'{client.batch}.json'
            batch = json.loads(command.read_text()); batch['jobs'][0]['expires_at'] = 0
            command.write_text(json.dumps(batch))
            (self.base/'remote/ack-failure').unlink()
            until = time.monotonic()+3
            result = {}
            while time.monotonic() < until:
                result = client.acknowledgements(time.monotonic()+1)
                if result: break
                time.sleep(.02)
            self.assertTrue(skill.confirmed(result[job['id']], job))
            self.assertEqual(len((self.base/'device-calls').read_text().splitlines()), 1)
        finally:
            agent.terminate(); agent.wait(timeout=5)

    def test_observed_state_publisher_uploads_two_temperatures_via_disk(self):
        from test_alice_state import snapshot
        state_dir = self.base/'device-runtime/states'
        state_dir.mkdir(parents=True)
        # Existing state-reader tests cover real wrapper/native protocol handling.
        # Here exercise the new publication transport with an observed fixture.
        (state_dir/'h1.json').write_text(json.dumps(snapshot('h1', target=24, measured=17.5)))
        (self.base/'bin/heatbridge-device').write_text('#!/bin/sh\nexit 73\n')
        path = self.base/'bin/heatbridge-alice-state'
        path.write_text((ROOT/'cloud/router/heatbridge-alice-state').read_text()
                        .replace('#!/opt/bin/sh', '#!/bin/sh', 1)
                        .replace('export PATH=', f'export PATH={self.base}/bin:'))
        agent = subprocess.Popen([str(path)], env={**self.env, 'HEATBRIDGE_STATE_TRANSPORT': 'disk',
             'HEATBRIDGE_DEVICE_RUNTIME': str(self.base/'device-runtime')},
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            uploaded = self.base/'remote/heatbridge-v1/acks/state-v1.json'
            until = time.monotonic()+3
            while not uploaded.exists() and time.monotonic() < until: time.sleep(.02)
            data = json.loads(uploaded.read_text())
            self.assertEqual(data['devices']['h1']['result']['reported_target'], 24)
            self.assertEqual(data['devices']['h1']['result']['current_temperature'], 17.5)
            self.assertNotIn('password', uploaded.read_text())
        finally:
            agent.terminate(); agent.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
