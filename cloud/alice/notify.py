"""Confirmed asynchronous Alice state notifications for the Disk mailbox.

Run as a separate, non-public Cloud Function on a Yandex timer.  The normal
skill only acknowledges durable WebDAV acceptance in explicit async mode.
Actual states come exclusively from verified router observations.
"""
import hashlib
import http.client
import json
import os
import re
import time

from device_state import observed_state, supported
from skill import owner_identifier
from yandex_services import ServiceError, from_environment, error_details

LEDGER = '/probes/alice-notify-ledger.json'
HOST = 'dialogs.yandex.net'


def callback_settings(settings):
    skill_id = settings.get('HEATBRIDGE_DIALOGS_SKILL_ID', '')
    token = settings.get('HEATBRIDGE_DIALOGS_OAUTH_TOKEN', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{6,128}', skill_id):
        raise ValueError('Invalid Dialogs skill ID')
    if not isinstance(token, str) or not 16 <= len(token) <= 2048 or any(c.isspace() for c in token):
        raise ValueError('Invalid Dialogs OAuth token')
    owner = owner_identifier(settings)
    names = json.loads(settings['HEATBRIDGE_DEVICES_JSON'])
    if not isinstance(names, dict) or not names or any(
            key not in ('h1', 'h2', 'h3', 'h4') for key in names):
        raise ValueError('Invalid devices config')
    return skill_id, token, owner, names


def send_callback(skill_id, token, owner, changes, timestamp, timeout=4):
    """No redirect; only a fixed Yandex host may receive the Dialogs token."""
    body = json.dumps({
        'ts': timestamp,
        'payload': {'user_id': owner, 'devices': changes},
    }, separators=(',', ':')).encode()
    connection = http.client.HTTPSConnection(HOST, timeout=timeout)
    try:
        connection.request(
            'POST', '/api/v1/skills/' + skill_id + '/callback/state',
            body=body, headers={
                'Authorization': 'OAuth ' + token,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            })
        response = connection.getresponse()
        status = response.status
        response.read(8192)
        response.close()
        return status
    finally:
        connection.close()


def run(settings=None, callback=send_callback, services=None, deadline_seconds=12):
    settings = os.environ if settings is None else settings
    skill_id, token, owner, names = callback_settings(settings)
    close_after = services is None
    if services is None:
        services = from_environment()
    if getattr(services, 'transport', None) != 'disk':
        raise ValueError('Notifications require Disk transport')
    deadline = time.monotonic() + deadline_seconds
    try:
        snapshot = services.state_snapshot(deadline)
        if not supported(snapshot):
            return {'status': 'NO_ROUTER_STATE', 'heater_control': False}
        prev = services.read_json(LEDGER, deadline) or {}
        if not isinstance(prev, dict) or prev.get('schema') != 1:
            prev = {}
        ledger = prev.get('devices', {}) if isinstance(prev.get('devices'), dict) else {}
        changes = []
        latest = dict(ledger)
        for heater in sorted(names):
            current = observed_state(snapshot, heater)
            if current is None:
                continue  # Never invent an offline state from a stale read.
            fingerprint = hashlib.sha256(json.dumps(
                current, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            previous = ledger.get(heater, {})
            if isinstance(previous, dict) and previous.get('fingerprint') == fingerprint:
                continue
            device = snapshot['devices'][heater]
            observed = device['observed_at']
            if isinstance(previous, dict) and observed <= previous.get('ts', 0):
                continue
            changes.append({'id': heater, 'status': 'online', **current})
            latest[heater] = {'fingerprint': fingerprint, 'ts': observed}
        if not changes:
            return {'status': 'UNCHANGED', 'heater_control': False}
        # Timestamp is the time of the latest observation in this batch.
        ts = max(latest[item['id']]['ts'] for item in changes)
        status = callback(skill_id, token, owner, changes, ts)
        if status != 202:
            # Preserve ledger, so the next scheduled run may retry.
            return {'status': 'CALLBACK_REJECTED', 'http_status': status,
                    'heater_control': False}
        body = json.dumps({'schema': 1, 'devices': latest},
                          separators=(',', ':')).encode()
        services.request('PUT', LEDGER, deadline, body)
        return {'status': 'OK', 'reported': [c['id'] for c in changes],
                'heater_control': False}
    finally:
        if close_after:
            services.close()


def handler(event, context):
    # Configure the function as private, invokable only by a timer service account.
    try:
        return run()
    except (KeyError, ValueError, TypeError):
        return {'status': 'INVALID_CONFIGURATION', 'heater_control': False}
    except ServiceError as error:
        return {'status': 'DISK_ERROR', **error_details(error),
                'heater_control': False}
    except (OSError, http.client.HTTPException):
        return {'status': 'CALLBACK_NETWORK_ERROR', 'heater_control': False}
