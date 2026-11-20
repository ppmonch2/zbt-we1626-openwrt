"""Safe asynchronous Alice actions + confirmed state callbacks, offline tests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'cloud/alice'))
import notify
import skill
from disk_services import DiskServices
from test_alice import event
from test_alice_disk import FileConnection
from test_alice_state import snapshot


CONFIG = {
    'HEATBRIDGE_OWNER_LOGIN': 'owner',
    'HEATBRIDGE_DEVICES_JSON': '{"h2":"Мансарда","h1":"Спальня"}',
    'HEATBRIDGE_DIALOGS_SKILL_ID': 'test-skill-123',
    'HEATBRIDGE_DIALOGS_OAUTH_TOKEN': 'fixture-dialogs-owner-oauth-token',
    'HEATBRIDGE_ASYNC_ACTIONS': '1',
    'HEATBRIDGE_TRANSPORT': 'disk',
}


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('commands', 'acks', 'probes'):
            (self.root/'heatbridge-v1'/name).mkdir(parents=True)
        self.client = DiskServices('fixture-user', 'fixture-password',
                                   connection=FileConnection(self.root))
        self.events = []

    def publish(self, target=24, measured=18.5):
        p = self.root/'heatbridge-v1/acks/state-v1.json'
        p.write_text(json.dumps({'schema': 1, 'published_at': time.time(),
                                 'devices': {'h2': snapshot(target=target, measured=measured)}}))

    def callback(self, skill_id, token, owner, changes, ts):
        self.events.append((skill_id, token, owner, changes, ts))
        return 202

    def test_only_real_observations_sent_and_no_duplicate_notifications(self):
        self.publish()
        result = notify.run(CONFIG, callback=self.callback, services=self.client)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['reported'], ['h2'])
        self.assertEqual(len(self.events), 1)
        skill_id, token, owner, changes, timestamp = self.events[0]
        self.assertEqual(skill_id, 'test-skill-123')
        self.assertEqual(owner, 'login:owner')
        self.assertEqual(changes[0]['id'], 'h2')
        self.assertEqual(changes[0]['capabilities'][1]['state']['value'], 24)
        self.assertEqual(changes[0]['properties'][0]['state']['value'], 18.5)
        self.assertEqual(changes[0]['status'], 'online')
        self.assertAlmostEqual(timestamp, changes and snapshot()['observed_at'], delta=2)
        self.assertEqual(notify.run(CONFIG, callback=self.callback, services=self.client)['status'],
                         'UNCHANGED')
        self.assertEqual(len(self.events), 1)
        self.publish(target=27)
        self.assertEqual(notify.run(CONFIG, callback=self.callback,
                                    services=self.client)['status'], 'OK')
        self.assertEqual(self.events[-1][3][0]['capabilities'][1]['state']['value'], 27)
        self.assertEqual(len(self.events), 2)

    def test_callback_error_does_not_advance_deduplication_ledger(self):
        self.publish()
        fail = lambda *args: 403
        self.assertEqual(notify.run(CONFIG, callback=fail, services=self.client)['status'],
                         'CALLBACK_REJECTED')
        ledger = self.root/'heatbridge-v1/probes/alice-notify-ledger.json'
        self.assertFalse(ledger.exists())
        self.assertEqual(notify.run(CONFIG, callback=self.callback, services=self.client)['status'],
                         'OK')
        self.assertTrue(ledger.exists())

    def test_stale_state_is_not_reported_as_new_or_success(self):
        self.publish()
        p = self.root/'heatbridge-v1/acks/state-v1.json'
        data = json.loads(p.read_text())
        data['devices']['h2']['observed_at'] = time.time()-250
        p.write_text(json.dumps(data))
        self.assertEqual(notify.run(CONFIG, callback=self.callback,
                                    services=self.client)['status'], 'UNCHANGED')
        self.assertFalse(self.events)

    def test_async_batch_claims_only_verified_webdav_queue_acceptance(self):
        class Batch:
            transport = 'disk'
            def __init__(self): self.jobs=[];self.reads=0
            def submit_many(self, jobs, deadline): self.jobs=jobs
            def acknowledgements(self, deadline):
                self.reads+=1
                return {}
        client=Batch()
        req=event(value=22)
        with patch.dict(os.environ, CONFIG):
            result=skill.process(req, {'h2':'Мансарда'}, '123', client,
                                 authorize=lambda *args: True)
        self.assertEqual(result['payload']['devices'][0]['capabilities'][0]
                         ['state']['action_result']['status'], 'DONE')
        self.assertEqual(client.reads, 0)
        self.assertEqual(client.jobs[0]['value'],22)

    def test_async_failed_queue_submission_never_claims_done(self):
        from yandex_services import ServiceError
        class Failed:
            transport='disk'
            def submit_many(self,jobs,deadline):
                raise ServiceError('cloud_connection_error',service='webdav')
            def acknowledgements(self,deadline):return {}
        with patch.dict(os.environ, CONFIG):
            response = skill.process(event(value=22),{'h2':'Мансарда'},'123',Failed(),
                                     authorize=lambda *args: True)
        result = response['payload']['devices'][0]['capabilities'][0]['state']['action_result']
        self.assertEqual(result['status'],'ERROR')

    def test_missing_callback_credentials_prevent_async_handler_success(self):
        class Disk:
            transport = 'disk'
            def close(self): pass
        from copy import deepcopy
        request = event(value=22)
        settings = deepcopy(CONFIG)
        settings.pop('HEATBRIDGE_DIALOGS_OAUTH_TOKEN')
        with patch.dict(os.environ,settings,clear=True),patch('skill.from_environment',return_value=Disk()):
            response=skill.handler(request,None)
        self.assertEqual(response['payload']['error_code'],'INTERNAL_ERROR')

    def test_rejects_missing_skill_and_token_without_sending_callbacks(self):
        for field in ('HEATBRIDGE_DIALOGS_SKILL_ID','HEATBRIDGE_DIALOGS_OAUTH_TOKEN'):
            settings=dict(CONFIG);settings.pop(field)
            with self.assertRaises(ValueError):
                notify.run(settings,callback=self.callback,services=self.client)
        self.assertFalse(self.events)


if __name__ == '__main__':
    unittest.main()
