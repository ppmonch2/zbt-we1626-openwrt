"""Optional fast Yandex Cloud-to-Cloud wake-up; router still uses WebDAV only.

An authenticated, non-public Cloud Function is asynchronously invoked after
a durable Disk command PUT. Its IAM token comes from the attached service
account's metadata, never from the router or Dialogs OAuth credentials.
"""
import http.client
import json
import re
import time

METADATA_HOST = '169.254.169.254'
FUNCTION_HOST = 'functions.yandexcloud.net'
TOKEN_PATH = '/computeMetadata/v1/instance/service-accounts/default/token'


def invoke(batch, jobs, function_id, deadline):
    """Best effort only: the minute-based notifier remains recovery fallback."""
    if not isinstance(batch, str) or not re.fullmatch(r'[0-9]{10}-[a-f0-9]{64}', batch):
        raise ValueError('Invalid Disk batch')
    if not isinstance(function_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{6,128}', function_id):
        raise ValueError('Invalid private function ID')
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 8:
        raise ValueError('Invalid batch jobs')
    if deadline - time.monotonic() < 0.35:
        return False
    metadata = http.client.HTTPConnection(METADATA_HOST, timeout=min(.65, max(.1, deadline-time.monotonic()-.1)))
    try:
        metadata.request('GET', TOKEN_PATH, headers={'Metadata-Flavor': 'Google'})
        res = metadata.getresponse()
        data = res.read(4096)
        if res.status != 200:
            return False
        token = json.loads(data)['access_token']
        if not isinstance(token, str) or not 16 <= len(token) <= 4096:
            return False
    finally:
        metadata.close()
    if deadline - time.monotonic() < .15:
        return False
    body = json.dumps({'heatbridge_watch_batch':batch,
                       'jobs': [{key: job[key] for key in ('id','heater','action','value')}
                                for job in jobs]},
                      separators=(',', ':')).encode()
    if len(body) > 4096:
        return False
    connection = http.client.HTTPSConnection(FUNCTION_HOST,
                    timeout=min(.85, max(.1, deadline-time.monotonic()-.1)))
    try:
        connection.request('POST', f'/{function_id}?integration=async', body=body,
                           headers={'Authorization': 'Bearer ' + token,
                                    'Content-Type': 'application/json'})
        res = connection.getresponse()
        status = res.status
        res.read(2048)
        return status == 202
    finally:
        connection.close()
