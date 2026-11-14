"""Exercise actual shell transport with isolated MQTT/UCI stand-ins; no devices."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.env = dict(os.environ, PATH=str(self.path) + ':' + os.environ['PATH'],
                        TEST_DIR=str(self.path))
        self.config = {
            'main.mode': 'mqtt', 'main.mqtt_host': '127.0.0.1',
            'main.mqtt_port': '1883', 'h1.protocol_verified': '1',
            'h1.command_topic': 'test/control/temperature',
            'h1.state_topic': 'test/state/temperature',
        }
        self.write_config()
        for name in ['uci', 'jsonfilter', 'mosquitto_pub', 'mosquitto_sub']:
            p = self.path / name
            p.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['TEST_DIR'])
name = pathlib.Path(sys.argv[0]).name
if name == 'jsonfilter':
    args = sys.argv
    value = json.loads(args[args.index('-s') + 1]).get(args[args.index('-e') + 1][2:])
    if value is not None: print(value)
elif name == 'uci':
    key = sys.argv[-1].removeprefix('heatbridge.')
    config = json.loads((p / 'config').read_text())
    if 'commit' in sys.argv: sys.exit(0)
    if 'set' in sys.argv:
        key, value = key.split('=', 1)
        config[key] = value
        (p / 'config').write_text(json.dumps(config))
        sys.exit(0)
    if key not in config: sys.exit(1)
    print(config[key])
else:
    with (p / 'calls').open('a') as f: f.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
    if (p / 'fail').exists(): sys.exit(1)
    if name == 'mosquitto_sub': print(os.environ.get('TEST_STATE', '22'))
''')
            p.chmod(0o755)

    def write_config(self):
        (self.path / 'config').write_text(json.dumps(self.config))

    def run_transport(self, *args):
        self.write_config()
        return subprocess.run(['sh', str(ROOT / 'overlay/usr/sbin/heatbridge-mqtt'),
                               *args], env=self.env, capture_output=True, text=True)

    def calls(self):
        p = self.path / 'calls'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_publish_real_setpoint_without_retain(self):
        result = self.run_transport('send', 'h1', '22')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[0]
        self.assertEqual(args[args.index('-m') + 1], '22')
        self.assertEqual(args[args.index('-q') + 1], '1')
        self.assertNotIn('-r', args)

    def test_unverified_or_simulated_never_publishes(self):
        for key, value in [('h1.protocol_verified', '0'), ('main.mode', 'simulation')]:
            original = self.config[key]
            self.config[key] = value
            self.assertNotEqual(self.run_transport('send', 'h1', '22').returncode, 0)
            self.config[key] = original
        self.assertEqual(self.calls(), [])

    def test_bad_temperature_and_wildcard_topic_never_publish(self):
        for value in ['0', '31', '-1', '22;echo bad', 'NaN', '22.5', '']:
            self.assertNotEqual(self.run_transport('send', 'h1', value).returncode, 0)
        self.config['h1.command_topic'] = 'test/#'
        self.assertNotEqual(self.run_transport('send', 'h1', '22').returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_tls_required_for_non_loopback_by_default(self):
        self.config['main.mqtt_host'] = 'broker.example'
        self.assertNotEqual(self.run_transport('send', 'h1', '22').returncode, 0)
        self.assertEqual(self.calls(), [])
        self.config['main.mqtt_tls'] = '1'
        self.assertEqual(self.run_transport('send', 'h1', '22').returncode, 0)
        self.assertIn('--cafile', self.calls()[0])
        self.assertNotIn('--insecure', self.calls()[0])

    def test_read_requires_fresh_numeric_state(self):
        result = self.run_transport('read', 'h1')
        self.assertEqual(result.stdout.strip(), '22')
        self.assertIn('-R', self.calls()[0])
        self.env['TEST_STATE'] = '{"temperature":22}'
        self.assertNotEqual(self.run_transport('read', 'h1').returncode, 0)

    def test_broker_failure_is_not_success(self):
        (self.path / 'fail').touch()
        self.assertNotEqual(self.run_transport('send', 'h1', '22').returncode, 0)

    def run_api(self, payload):
        self.write_config()
        script = (ROOT / 'overlay/www/heatbridge/cgi-bin/api').read_text()
        script = script.replace("export PATH='/usr/sbin:/usr/bin:/sbin:/bin'", '')
        script = script.replace('/usr/sbin/heatbridge-"$mode"', str(self.path / 'transport'))
        (self.path / 'transport').write_text(
            (ROOT / 'overlay/usr/sbin/heatbridge-mqtt').read_text())
        (self.path / 'transport').chmod(0o755)
        text = json.dumps(payload)
        env = dict(self.env, REQUEST_METHOD='POST', CONTENT_TYPE='application/json',
                   CONTENT_LENGTH=str(len(text.encode())))
        result = subprocess.run(['sh', '-c', script], input=text, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.split('\n\n', 1)[1])

    def test_api_does_not_claim_device_confirmation(self):
        data = self.run_api({'action': 'set', 'id': 'h1', 'temp': 22})
        self.assertTrue(data['real_commands_sent'])
        self.assertEqual(data['heaters'][0]['delivery'], 'published')
        self.assertFalse(data['heaters'][0]['confirmed'])

    def test_api_does_not_save_failed_setpoint(self):
        self.config['h1.temp'] = '18'
        (self.path / 'fail').touch()
        data = self.run_api({'action': 'set', 'id': 'h1', 'temp': 22})
        self.assertFalse(data['real_commands_sent'])
        self.assertEqual(data['heaters'][0]['delivery'], 'failed')
        self.assertEqual(data['heaters'][0]['temp'], 18)

    def test_preset_reports_partial_delivery_per_device(self):
        data = self.run_api({'action': 'mode', 'preset': 'arrive'})
        self.assertTrue(data['real_commands_sent'])
        self.assertEqual([h['delivery'] for h in data['heaters']],
                         ['published', 'failed', 'failed', 'failed'])

    def test_update_archive_excludes_config_and_firmware(self):
        import tarfile
        subprocess.run(['python3', str(ROOT / 'boards/make_heatbridge_update.py')],
                       check=True, capture_output=True)
        with tarfile.open(ROOT / 'output-custom/heatbridge-app.tar.gz') as tar:
            names = tar.getnames()
            self.assertIn('usr/sbin/heatbridge-mqtt', names)
            self.assertTrue(all(n.startswith(('www/heatbridge/', 'usr/sbin/heatbridge-')) or n == 'usr/bin/heatbridge-syncleo'
                                for n in names))
            self.assertFalse(any('..' in Path(n).parts or n.startswith('/') for n in names))
