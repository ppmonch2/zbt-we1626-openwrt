"""Real observed state mapping and router polling/preemption without hardware."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cloud/alice'))
import skill
from device_state import observed_state, supported
from yandex_services import Services, ServiceError
from test_alice import FakeServices, event


def snapshot(heater='h2', target=24, measured=21.5, mode=1):
    stamp = time.time()
    return {'heater': heater, 'online': True, 'observed_at': stamp,
            'field_observed_at': {name: stamp for name in
                ('reported_target', 'current_temperature', 'device_mode')},
            'result': {'encrypted_session': True, 'reported_target': target,
                       'current_temperature': measured, 'device_mode': mode}}


class StateServices(FakeServices):
    def __init__(self, bundle):
        super().__init__()
        self.bundle, self.reads = bundle, 0

    def state_snapshot(self, deadline):
        self.reads += 1
        return self.bundle


class AliceStateTest(unittest.TestCase):
    def request(self, client, kind='query', heaters=('h2',)):
        request = event()
        request['request_type'] = kind
        request['payload']['devices'] = [{'id': heater} for heater in heaters]
        return skill.process(request, {'h1': 'Окно', 'h2': 'Мансарда', 'h3': 'Лестница', 'h4': 'Кухня'},
                             '123', client, authorize=lambda *args: True)

    def test_discovery_advertises_distinct_target_and_sensor_only_with_publisher(self):
        client = StateServices({'schema': 1, 'devices': {}})
        response = self.request(client, 'discovery')
        for device in response['payload']['devices']:
            self.assertTrue(all(cap['retrievable'] for cap in device['capabilities']))
            self.assertEqual(device['capabilities'][1]['type'], skill.RANGE)
            self.assertEqual(device['properties'], [{'type': 'devices.properties.float',
                'retrievable': True, 'reportable': False,
                'parameters': {'instance': 'temperature', 'unit': 'unit.temperature.celsius'}}])
        self.assertEqual(client.reads, 1)
        old_router = self.request(StateServices(None), 'discovery')['payload']['devices'][0]
        self.assertNotIn('properties', old_router)
        self.assertFalse(old_router['capabilities'][1]['retrievable'])

    def test_disk_discovery_is_stable_without_webdav_state_or_publisher(self):
        class UnavailableDisk(StateServices):
            transport = 'disk'
            def state_snapshot(self, deadline):
                raise ServiceError('cloud_connection_error', service='webdav')
        client = UnavailableDisk(None)
        discovery = self.request(client, 'discovery')['payload']['devices']
        self.assertEqual(client.reads, 0)
        self.assertEqual(len(discovery), 4)
        self.assertTrue(all(all(c['retrievable'] for c in d['capabilities'])
                            for d in discovery))
        self.assertTrue(all(d['properties'][0]['retrievable'] for d in discovery))
        # A failed current-state read still marks query as unreachable. We
        # must not synthesize values, but device capabilities remain registered.
        result = self.request(client, 'query')['payload']['devices'][0]
        self.assertEqual(result['error_code'], 'DEVICE_UNREACHABLE')

    def test_disk_discovery_is_retrievable_before_first_router_publication(self):
        client = StateServices(None)
        client.transport = 'disk'
        d = self.request(client, 'discovery')['payload']['devices'][0]
        self.assertEqual(client.reads, 0)
        self.assertTrue(d['capabilities'][1]['retrievable'])
        self.assertIn('properties', d)

    def test_query_reads_all_four_heaters_in_one_get_and_never_submits_commands(self):
        states = {f'h{i}': snapshot(f'h{i}', 20+i, 18+i/10, 0 if i == 1 else i-1)
                  for i in range(1, 5)}
        client = StateServices({'schema': 1, 'devices': states})
        response = self.request(client, heaters=tuple(states))
        for i, device in enumerate(response['payload']['devices'], 1):
            self.assertEqual(device['capabilities'][1]['state']['value'], 20+i)
            self.assertEqual(device['properties'][0]['state']['value'], 18+i/10)
            self.assertEqual(device['capabilities'][0]['state']['value'], i != 1)
        self.assertEqual(client.reads, 1)
        self.assertEqual(client.jobs, [])

    def test_local_or_remote_control_changes_are_observed_not_replaced_with_saved_command(self):
        client = StateServices({'schema': 1, 'devices': {'h2': snapshot(target=18, measured=23.7)}})
        device = self.request(client)['payload']['devices'][0]
        self.assertEqual(device['capabilities'][1]['state']['value'], 18)
        self.assertEqual(device['properties'][0]['state']['value'], 23.7)

    def test_stale_future_offline_wrong_heater_or_partial_read_is_not_current_state(self):
        good = snapshot()
        variants = []
        for key, value in [('observed_at', time.time()-91), ('observed_at', time.time()+10),
                           ('observed_at', True), ('online', False), ('heater', 'h1')]:
            item = copy.deepcopy(good); item[key] = value; variants.append(item)
        for key, value in [('reported_target', None), ('current_temperature', float('nan')),
                           ('current_temperature', True), ('device_mode', 7), ('encrypted_session', False)]:
            item = copy.deepcopy(good); item['result'][key] = value; variants.append(item)
        item = copy.deepcopy(good)
        item['field_observed_at']['current_temperature'] = time.time()-91
        variants.append(item)
        for item in variants:
            client = StateServices({'schema': 1, 'devices': {'h2': item}})
            with self.subTest(item=item):
                self.assertEqual(self.request(client)['payload']['devices'][0],
                                 {'id': 'h2', 'error_code': 'DEVICE_UNREACHABLE'})

    def test_one_offline_heater_does_not_hide_other_heaters(self):
        client = StateServices({'schema': 1, 'devices': {'h2': snapshot()}})
        devices = self.request(client, heaters=('h1', 'h2'))['payload']['devices']
        self.assertEqual(devices[0]['error_code'], 'DEVICE_UNREACHABLE')
        self.assertIn('properties', devices[1])

    def test_read_failure_is_not_treated_as_old_router_without_publisher(self):
        class Failed(StateServices):
            def state_snapshot(self, deadline):
                raise ServiceError('cloud_connection_error')
        self.assertEqual(self.request(Failed(None))['payload']['devices'][0]['error_code'],
                         'DEVICE_UNREACHABLE')
        self.assertEqual(self.request(Failed(None), 'discovery')['payload']['error_code'],
                         'INTERNAL_ERROR')

    def test_relative_change_uses_fresh_observed_target_and_never_room_temperature(self):
        client = StateServices({'schema': 1, 'devices': {'h2': snapshot(target=24, measured=18)}})
        request = event(1)
        request['payload']['devices'][0]['capabilities'][0]['state']['relative'] = True
        response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                 authorize=lambda *args: True)
        self.assertEqual(client.jobs[0]['value'], 25)
        self.assertEqual(response['payload']['devices'][0]['capabilities'][0]['state']['action_result'],
                         {'status': 'DONE'})
        client = StateServices({'schema': 1, 'devices': {}})
        response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                 authorize=lambda *args: True)
        self.assertFalse(client.jobs)
        self.assertEqual(response['payload']['devices'][0]['capabilities'][0]['state']['action_result']['error_code'],
                         'DEVICE_UNREACHABLE')

    def test_alice_plus_minus_buttons_use_fresh_setpoint_even_without_room_sensor(self):
        # Yandex sends relative:true +1/-1 for thermostat controls.
        for delta, expected in ((1, 22), (-1, 20)):
            with self.subTest(delta=delta):
                state = snapshot(target=21, measured=18.5)
                state['result']['current_temperature'] = None
                state['field_observed_at']['current_temperature'] = time.time() - 200
                client = StateServices({'schema': 1, 'devices': {'h2': state}})
                request = event(delta)
                request['payload']['devices'][0]['capabilities'][0]['state']['relative'] = True
                response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                         authorize=lambda *args: True)
                self.assertEqual([job['value'] for job in client.jobs], [expected])
                self.assertEqual(response['payload']['devices'][0]['capabilities'][0]
                                 ['state']['action_result']['status'], 'DONE')

    def test_alice_relative_buttons_refuse_stale_or_boundary_setpoints(self):
        for target, stamp, delta in ((35, time.time(), 1), (10, time.time(), -1),
                                      (22, time.time()-120, 1)):
            with self.subTest(target=target, delta=delta):
                state = snapshot(target=target)
                state['observed_at'] = stamp
                state['field_observed_at']['reported_target'] = stamp
                client = StateServices({'schema': 1, 'devices': {'h2': state}})
                request = event(delta)
                request['payload']['devices'][0]['capabilities'][0]['state']['relative'] = True
                response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                         authorize=lambda *args: True)
                self.assertFalse(client.jobs)
                self.assertEqual(response['payload']['devices'][0]['capabilities'][0]
                                 ['state']['action_result']['status'], 'ERROR')

    def test_relative_buttons_can_reach_35_from_34_and_10_from_11(self):
        for old, delta, expected in ((34, 1, 35), (11, -1, 10)):
            with self.subTest(old=old, delta=delta):
                client = StateServices({'schema': 1, 'devices': {'h2': snapshot(target=old)}})
                request = event(delta)
                request['payload']['devices'][0]['capabilities'][0]['state']['relative'] = True
                response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                         authorize=lambda *args: True)
                self.assertEqual(client.jobs[0]['value'], expected)
                self.assertEqual(response['payload']['devices'][0]['capabilities'][0]
                                 ['state']['action_result']['status'], 'DONE')

    def test_state_reads_are_authorized_and_only_signed_gets_are_needed(self):
        client = StateServices({'schema': 1, 'devices': {'h2': snapshot()}})
        request = event(); request['request_type'] = 'query'
        response = skill.process(request, {'h2': 'Мансарда'}, '123', client,
                                 authorize=lambda *args: False)
        self.assertEqual(client.reads, 0)
        self.assertEqual(response['payload']['error_code'], 'ACCOUNT_LINKING_ERROR')
        calls = []
        def opener(request, timeout):
            calls.append(request)
            return io.BytesIO(b'{"schema":1,"devices":{}}')
        service = Services('https://message-queue.api.cloud.yandex.net/folder/id/jobs',
                           'private-bucket', 'fixture-key', 'fixture-secret', opener)
        self.assertTrue(supported(service.state_snapshot(time.monotonic()+1)))
        self.assertEqual(calls[0].get_method(), 'GET')
        self.assertTrue(calls[0].full_url.endswith('/acks/state-v1.json'))
        self.assertIn('AWS4-HMAC-SHA256', calls[0].get_header('Authorization'))


class RouterStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for name in ('bin', 'libexec', 'etc/heatbridge', 'runtime', 'device-runtime/states'):
            (self.base/name).mkdir(parents=True, exist_ok=True)
        config = {'lan_ip': '192.168.1.1', 'heaters': {f'h{i}': {
            'verified': True, 'device_id': f'{i:012x}', 'token': 'fixture-private-token'} for i in range(1, 5)}}
        (self.base/'etc/heatbridge/config.json').write_text(json.dumps(config))
        (self.base/'etc/heatbridge/alice.json').write_text('{"ack_bucket":"private-bucket"}')
        (self.base/'runtime/curl.conf').write_text('fixture-private-curl-config')
        wrapper = self.base/'bin/heatbridge-device'
        wrapper.write_text((ROOT/'entware/bin/heatbridge-device').read_text().replace('#!/opt/bin/sh', '#!/bin/sh', 1))
        wrapper.chmod(0o755)
        native = self.base/'libexec/heatbridge-syncleo'
        native.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys,time
p=pathlib.Path(os.environ['HEATBRIDGE_PREFIX'])
action=sys.argv[1]
if action=='discover':
 print(json.dumps({'ip':'192.168.1.2','port':1234,'public_key':'fixture-public-key'}));sys.exit(0)
sys.stdin.read()
if action=='read' and (p/'block-read').exists():
 (p/'reading').write_text(str(os.getpid()))
 time.sleep(20)
if (p/'fail').exists():sys.exit(2)
print(json.dumps({'ok':True,'encrypted_session':True,'command_acknowledged':action!='read',
 'state_echo_matches_request':action!='read','reported_target':int(sys.argv[2]) if action=='set' else 22,
 'current_temperature':21.5 if action=='read' else None,'device_mode':1}))
''')
        native.chmod(0o755)
        self.env = {**os.environ, 'HEATBRIDGE_PREFIX': str(self.base),
                    'HEATBRIDGE_ALICE_RUNTIME': str(self.base/'runtime'),
                    'HEATBRIDGE_DEVICE_RUNTIME': str(self.base/'device-runtime')}

    def call(self, action='read', heater='h2', value=None):
        args = [str(self.base/'bin/heatbridge-device'), action, heater]
        if value is not None: args.append(str(value))
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=3)

    def read_snapshot(self, heater='h2'):
        return json.loads((self.base/f'device-runtime/states/{heater}.json').read_text())

    def test_local_reader_publishes_real_values_and_no_credentials(self):
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.read_snapshot()
        self.assertEqual(data['result']['reported_target'], 22)
        self.assertEqual(data['result']['current_temperature'], 21.5)
        self.assertNotIn('fixture', json.dumps(data))
        self.assertEqual((self.base/'device-runtime/states/h2.json').stat().st_mode & 0o777, 0o600)

    def test_partial_command_echo_preserves_sensor_timestamp(self):
        self.assertEqual(self.call().returncode, 0)
        before = self.read_snapshot()
        result = self.call('set', value=24)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.read_snapshot()
        self.assertEqual(after['result']['reported_target'], 24)
        self.assertEqual(after['result']['current_temperature'], 21.5)
        self.assertEqual(before['field_observed_at']['current_temperature'],
                         after['field_observed_at']['current_temperature'])

    def start_blocked_read(self, background=True):
        (self.base/'block-read').touch()
        reader = subprocess.Popen([str(self.base/'bin/heatbridge-device'), 'read', 'h2'],
            env={**self.env, 'HEATBRIDGE_BACKGROUND_READ': '1' if background else '0'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if reader.poll() is None: reader.terminate()
            reader.wait(timeout=3)
        self.addCleanup(cleanup)
        until = time.monotonic()+2
        while not (self.base/'reading').exists() and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue((self.base/'reading').exists())
        return reader

    def test_command_preempts_only_background_read_and_cleans_up_its_native_child(self):
        reader = self.start_blocked_read()
        native_pid = int((self.base/'reading').read_text())
        result = self.call('set', value=24)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reader.wait(timeout=2), 75)
        self.assertFalse(Path(f'/proc/{native_pid}').exists())
        self.assertEqual(self.read_snapshot()['result']['reported_target'], 24)
        self.assertFalse((self.base/'device-runtime/device-h2.lock').exists())

    def test_foreground_read_is_not_killed_and_other_heater_can_be_controlled(self):
        reader = self.start_blocked_read(background=False)
        self.assertEqual(self.call('set', value=24).returncode, 73)
        self.assertIsNone(reader.poll())
        self.assertEqual(self.call('set', 'h1', 24).returncode, 0)

    def test_failed_reader_does_not_create_fabricated_observation(self):
        (self.base/'fail').touch()
        self.assertNotEqual(self.call().returncode, 0)
        self.assertFalse((self.base/'device-runtime/states/h2.json').exists())

    def test_abandoned_per_device_lock_is_recovered(self):
        lock = self.base/'device-runtime/device-h2.lock'
        lock.mkdir(); (lock/'owner').write_text('99999999 background\n')
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())

    def test_new_device_binding_does_not_inherit_previous_devices_sensor(self):
        self.assertEqual(self.call().returncode, 0)
        config_path = self.base/'etc/heatbridge/config.json'
        config = json.loads(config_path.read_text())
        config['heaters']['h2']['verified'] = False
        config_path.write_text(json.dumps(config))
        native = self.base/'libexec/heatbridge-syncleo'
        native.write_text(native.read_text().replace("21.5 if action=='read' else None", 'None'))
        self.assertEqual(self.call().returncode, 0)
        self.assertIsNone(self.read_snapshot()['result']['current_temperature'])

    def test_poller_uploads_both_temperatures_from_existing_reader(self):
        script = '''curl() {
 cp "$HEATBRIDGE_ALICE_RUNTIME/telemetry/upload.json" "$HEATBRIDGE_PREFIX/uploaded.json"
}
. "''' + str(ROOT/'cloud/router/heatbridge-alice-state') + '"\n'
        worker = subprocess.Popen(['sh', '-c', script], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = time.monotonic()+3
            while not (self.base/'uploaded.json').exists() and time.monotonic() < until:
                time.sleep(.01)
            bundle = json.loads((self.base/'uploaded.json').read_text())
            self.assertEqual(bundle['schema'], 1)
            state = observed_state(bundle, 'h1')
            self.assertEqual(state['capabilities'][1]['state']['value'], 22)
            self.assertEqual(state['properties'][0]['state']['value'], 21.5)
            self.assertNotIn('fixture', json.dumps(bundle))
        finally:
            worker.terminate(); worker.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
