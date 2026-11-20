"""Regression tests for opt-in, Disk-only, push state without extra heater reads."""
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
import fast_push
import notify
import skill
from disk_services import DiskServices
from test_alice_disk import FileConnection
from test_alice_state import snapshot

CONFIG = {
    'HEATBRIDGE_OWNER_LOGIN': 'owner',
    'HEATBRIDGE_DEVICES_JSON': '{"h2":"Мансарда"}',
    'HEATBRIDGE_DIALOGS_SKILL_ID': 'test-skill-123',
    'HEATBRIDGE_DIALOGS_OAUTH_TOKEN': 'fixture-dialogs-owner-oauth-token',
    'HEATBRIDGE_TRANSPORT': 'disk',
    'HEATBRIDGE_PUSH_STATE': '1',
    'HEATBRIDGE_ASYNC_ACTIONS': '1',
    'HEATBRIDGE_FAST_NOTIFY_FUNCTION_ID': 'd4testprivatewatch',
}


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('commands','acks','probes'):
            (self.root/'heatbridge-v1'/name).mkdir(parents=True)
        self.services = DiskServices('fixture-user', 'fixture-password',
                                     connection=FileConnection(self.root))
        self.notifications = []

    def cb(self, skill_id, token, owner, changes, ts):
        self.notifications.append((changes,ts))
        return 202

    def test_catalog_sends_only_on_push_and_never_reads_fields(self):
        legacy = skill.catalog({'h2':'Мансарда'}, True)[0]
        new = skill.catalog({'h2':'Мансарда'}, True, True)[0]
        self.assertTrue(legacy['capabilities'][1]['retrievable'])
        for cap in new['capabilities'] + new['properties']:
            self.assertFalse(cap['retrievable'])
            self.assertTrue(cap['reportable'])

    def test_verified_ack_is_callback_without_any_heater_query(self):
        batch = '1795200000-' + 'a'*64
        job = {'id':'b'*64,'heater':'h2','action':'set','value':27}
        ack = {**job, 'ok': True, 'observed_at': time.time(),
               'result': {'encrypted_session':True,
                          'command_acknowledged':True,
                          'state_echo_matches_request':True,
                          'reported_target':27}}
        (self.root/'heatbridge-v1/acks'/f'{batch}.json').write_text(json.dumps(
            {'batch':batch,'acks':{job['id']:ack}}))
        result = notify.watch_batch({'heatbridge_watch_batch':batch,'jobs':[job]},
                                    CONFIG,services=self.services,callback=self.cb,seconds=1)
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(len(self.notifications),1)
        self.assertEqual(self.notifications[0][0][0]['capabilities'][0]['state']['value'],27)
        self.assertFalse(self.notifications[0][0][0].get('properties'))
        # Duplicate result is deduplicated by verified timestamp+value.
        notify.watch_batch({'heatbridge_watch_batch':batch,'jobs':[job]},
                           CONFIG,services=self.services,callback=self.cb,seconds=1)
        self.assertEqual(len(self.notifications),1)

    def test_bad_or_missing_ack_never_reports_success(self):
        batch = '1795200000-'+'a'*64
        job = {'id':'c'*64,'heater':'h2','action':'set','value':27}
        ack = {**job,'ok':True,'observed_at':time.time(),
               'result':{'encrypted_session':True,'command_acknowledged':True,
                         'state_echo_matches_request':False,'reported_target':27}}
        (self.root/'heatbridge-v1/acks'/f'{batch}.json').write_text(json.dumps(
            {'batch':batch,'acks':{job['id']:ack}}))
        result=notify.watch_batch({'heatbridge_watch_batch':batch,'jobs':[job]},
                                  CONFIG,services=self.services,callback=self.cb,seconds=1)
        self.assertEqual(result['status'],'NO_CONFIRMED_ACTION')
        self.assertFalse(self.notifications)

    def test_fast_callback_cannot_be_undone_by_older_snapshot(self):
        now=time.time()
        result=notify.push_changes(CONFIG,self.services,{'h2':{'target':(27,now)}},self.cb)
        self.assertEqual(result['status'],'OK')
        self.assertEqual(notify.push_changes(CONFIG,self.services,
                       {'h2':{'target':(28,now-3)}},self.cb)['status'],'UNCHANGED')
        self.assertEqual(len(self.notifications),1)
        self.assertEqual(notify.push_changes(CONFIG,self.services,
                       {'h2':{'target':(28,now+0.1)}},self.cb)['status'],'OK')
        self.assertEqual(len(self.notifications),2)

    def test_timer_does_not_resend_setpoint_after_ack(self):
        now=time.time()
        self.assertEqual(notify.push_changes(CONFIG,self.services,
                       {'h2':{'target':(27,now)}},self.cb)['status'],'OK')
        (self.root/'heatbridge-v1/acks/state-v1.json').write_text(json.dumps(
            {'schema':1,'published_at':now,
             'devices':{'h2':snapshot(target=27,measured=19.5)}}))
        result=notify.push_from_snapshot(CONFIG,self.services,self.cb)
        self.assertEqual(result['status'],'OK')
        reported=self.notifications[-1][0][0]
        self.assertFalse(any(item['type']=='devices.capabilities.range'
                        for item in reported.get('capabilities',[])))
        self.assertEqual(notify.push_from_snapshot(CONFIG,self.services,self.cb)['status'],'UNCHANGED')

    def test_no_undocumented_action_as_success(self):
        class Batch:
            transport='disk'
            batch='1795200000-'+'a'*64
            def submit_many(self,jobs,deadline):pass
        from test_alice import event
        with patch.dict(os.environ, CONFIG),patch('fast_push.invoke',return_value=False) as schedule:
            result=skill.process(event(value=27),{'h2':'Мансарда'},'123',Batch(),
                                authorize=lambda *args:True)
        self.assertEqual(result['payload']['devices'][0]['capabilities'][0]
                         ['state']['action_result']['status'],'DONE')
        schedule.assert_called_once()


if __name__ == '__main__':
    unittest.main()
