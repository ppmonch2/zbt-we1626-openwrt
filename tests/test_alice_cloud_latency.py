"""Cloud-only latency changes: no network, credentials, or real heater actions."""
import io
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud/alice'))
import skill
import yandex_services as cloud


def response(data):
    result = io.BytesIO(data)
    result.status, result.headers = 200, {}
    return result


class CloudLatencyTest(unittest.TestCase):
    def setUp(self):
        cloud._owner_checks.clear()

    def test_s3_connection_opens_during_queue_post_and_is_reused_for_ack(self):
        connecting, queued = threading.Event(), threading.Event()
        sqs, s3 = MagicMock(), MagicMock()
        def connect():
            connecting.set()
            if not queued.wait(1):
                raise OSError('queue did not start concurrently')
        s3.connect.side_effect = connect
        def send(*args, **kwargs):
            self.assertTrue(connecting.wait(1))
            queued.set()
        sqs.request.side_effect = send
        sqs.getresponse.return_value = response(b'<SendMessageResponse><MessageId>x</MessageId></SendMessageResponse>')
        s3.getresponse.return_value = response(b'{"ok":true}')
        def connection(host, timeout):
            return s3 if host == 'storage.yandexcloud.net' else sqs
        with patch.object(cloud.http.client, 'HTTPSConnection', side_effect=connection) as factory:
            client = cloud.Services('https://message-queue.api.cloud.yandex.net/folder/id/jobs',
                                    'private-bucket', 'fixture-access', 'fixture-secret')
            deadline = time.monotonic() + 2
            client.submit({'id': 'a'*64}, deadline)
            self.assertEqual(client.acknowledgement('a'*64, deadline), {'ok': True})
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(s3.request.call_args.args[0], 'GET')
            sqs.request.assert_called_once()
            client.close()
            s3.close.assert_called_once()

    def test_failed_preconnect_falls_back_to_regular_signed_get(self):
        failed, normal = MagicMock(), MagicMock()
        failed.connect.side_effect = OSError('fixture')
        normal.getresponse.return_value = response(b'{}')
        with patch.object(cloud.http.client, 'HTTPSConnection', side_effect=[failed, normal]):
            client = cloud.Services('https://message-queue.api.cloud.yandex.net/folder/id/jobs',
                                    'private-bucket', 'fixture-access', 'fixture-secret')
            deadline = time.monotonic() + 2
            client.opener.prepare_acknowledgements(deadline)
            self.assertEqual(client.acknowledgement('a'*64, deadline), {})
            failed.close.assert_called_once()
            normal.request.assert_called_once()
            client.close()

    def test_late_preconnect_cannot_leak_connection_after_handler_closes(self):
        entered, release = threading.Event(), threading.Event()
        connection = MagicMock()
        def connect():
            entered.set()
            release.wait(1)
        connection.connect.side_effect = connect
        with patch.object(cloud.http.client, 'HTTPSConnection', return_value=connection):
            transport = cloud.KeepAliveHTTPS()
            transport.prepare_acknowledgements(time.monotonic() + 2)
            self.assertTrue(entered.wait(1))
            transport.close()
            release.set()
            transport.preparing.join(1)
            self.assertFalse(transport.preparing.is_alive())
            self.assertEqual(transport.connections, {})
            connection.close.assert_called_once()

    def test_preconnect_never_makes_poll_wait_beyond_request_budget(self):
        transport = cloud.KeepAliveHTTPS()
        transport.preparing = MagicMock()
        transport.preparing.is_alive.return_value = True
        with patch.object(cloud.time, 'monotonic', return_value=100):
            with self.assertRaises(cloud.ServiceError):
                transport.await_acknowledgements(100.1)
        self.assertAlmostEqual(transport.preparing.join.call_args.args[0], .1)

    def test_relative_action_reuses_connection_already_used_for_state_query(self):
        transport = cloud.KeepAliveHTTPS()
        connection = MagicMock()
        transport.connections['storage.yandexcloud.net'] = connection
        with patch.object(cloud.http.client, 'HTTPSConnection') as factory:
            transport.prepare_acknowledgements(time.monotonic()+1)
        factory.assert_not_called()
        self.assertIsNone(transport.preparing)
        transport.close()
        connection.close.assert_called_once()

    def test_successful_authorization_reused_only_for_same_owner_token_and_ttl(self):
        with patch.object(cloud.time, 'monotonic', return_value=100) as clock, \
                patch.object(cloud, 'owner_authorized', return_value=True) as verify:
            self.assertTrue(cloud.cached_owner_authorized('Bearer fixture', '123', 200))
            clock.return_value = 129
            self.assertTrue(cloud.cached_owner_authorized('Bearer fixture', '123', 200))
            self.assertEqual(verify.call_count, 1)
            self.assertNotIn('fixture', repr(cloud._owner_checks))
            clock.return_value = 130
            self.assertTrue(cloud.cached_owner_authorized('Bearer fixture', '123', 200))
            self.assertTrue(cloud.cached_owner_authorized('Bearer different', '123', 200))
            self.assertTrue(cloud.cached_owner_authorized('Bearer fixture', '456', 200))
            self.assertEqual(verify.call_count, 4)
            self.assertFalse(cloud.cached_owner_authorized('Bearer fixture', '123', 129))

    def test_failed_or_malformed_authorization_is_never_cached(self):
        with patch.object(cloud, 'owner_authorized', return_value=False) as verify:
            for _ in range(2):
                self.assertFalse(cloud.cached_owner_authorized('Bearer fixture', '123', time.monotonic()+1))
            self.assertEqual(verify.call_count, 2)
            self.assertFalse(cloud.cached_owner_authorized('Bearer x\nInjected', '123', time.monotonic()+1))
            self.assertEqual(cloud._owner_checks, {})

    def test_cache_stays_bounded(self):
        with patch.object(cloud, 'owner_authorized', return_value=True):
            for index in range(140):
                self.assertTrue(cloud.cached_owner_authorized(f'Bearer fixture-{index}', '123', time.monotonic()+1))
        self.assertLessEqual(len(cloud._owner_checks), 128)

    def test_version_probe_has_no_cloud_or_heater_side_effects(self):
        with patch.object(skill, 'from_environment') as factory:
            result = skill.handler('{"heatbridge_diagnostic":"version"}', None)
        self.assertEqual(result['version'], skill.VERSION)
        self.assertFalse(result['heater_control'])
        self.assertEqual(result['state_reporting'], 'auto')
        factory.assert_not_called()

    def test_confirmation_at_2700ms_is_accepted_but_missing_ack_is_not_success(self):
        from test_alice import FakeServices, event
        ticks = [100.0]
        class Delayed(FakeServices):
            def acknowledgement(self, identity, deadline):
                if ticks[0] == 100:
                    ticks[0] = 102.7
                    return None
                return super().acknowledgement(identity, deadline)
        with patch.object(skill.time, 'monotonic', side_effect=lambda: ticks[0]), \
                patch.object(skill.time, 'sleep'):
            result = skill.process(event(), {'h2': 'Мансарда'}, '123', Delayed(),
                                   authorize=lambda *args: True)
        action = result['payload']['devices'][0]['capabilities'][0]['state']['action_result']
        self.assertEqual(action['status'], 'DONE')
        self.assertLessEqual(skill.RESPONSE_BUDGET, 2.8)


if __name__ == '__main__':
    unittest.main()
