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



# Push-only mode uses per-field deduplication. A verified command ACK can
# update the setpoint independently of room-temperature readings. The periodic
# snapshot publisher fills gaps (local button presses, ambient readings, offline).
PUSH_LEDGER = '/probes/alice-push-ledger.json'


def push_changes(settings, services, updates, callback=send_callback, deadline_seconds=12):
    """Notify only real changed fields. Store the ledger only after HTTP 202."""
    skill_id, token, owner, names = callback_settings(settings)
    deadline = time.monotonic() + deadline_seconds
    previous = services.read_json(PUSH_LEDGER, deadline) or {}
    if not isinstance(previous, dict) or previous.get('schema') != 2:
        previous = {}
    ledger = previous.get('devices', {})
    if not isinstance(ledger, dict):
        ledger = {}
    latest = {k: dict(v) for k, v in ledger.items() if isinstance(v, dict)}
    notifications = []
    timestamps = []
    now = time.time()
    for heater in sorted(updates):
        if heater not in names or heater not in ('h1', 'h2', 'h3', 'h4'):
            continue
        fields = updates[heater]
        if not isinstance(fields, dict):
            continue
        saved = dict(latest.get(heater, {}))
        device = {'id': heater}
        capabilities = []
        properties = []
        for field in ('status', 'power', 'target', 'temperature'):
            value_stamp = fields.get(field)
            if not isinstance(value_stamp, (list, tuple)) or len(value_stamp) != 2:
                continue
            value, stamp = value_stamp
            if (type(stamp) not in (float, int) or
                    not now - 90 <= stamp <= now + 5):
                continue
            if field == 'status':
                if value not in ('online', 'offline'):
                    continue
            elif field == 'power':
                if type(value) is not bool:
                    continue
            elif field == 'target':
                if type(value) is not int or not 10 <= value <= 35:
                    continue
            elif field == 'temperature':
                if (type(value) not in (int, float) or
                        not -60 <= value <= 100):
                    continue
                # Do not flood Dialogs for 0.01 C sensor noise. 0.5 C steps.
                value = round(value * 2) / 2
            prior = saved.get(field)
            if isinstance(prior, dict):
                if stamp <= prior.get('ts', -1):
                    continue
                if prior.get('value') == value:
                    continue
            saved[field] = {'value': value, 'ts': stamp}
            timestamps.append(stamp)
            if field == 'status':
                device['status'] = value
            elif field == 'power':
                capabilities.append({'type': 'devices.capabilities.on_off',
                    'state': {'instance': 'on', 'value': value}})
            elif field == 'target':
                capabilities.append({'type': 'devices.capabilities.range',
                    'state': {'instance': 'temperature', 'value': value}})
            else:
                properties.append({'type': 'devices.properties.float',
                    'state': {'instance': 'temperature', 'value': value}})
        if capabilities:
            device['capabilities'] = capabilities
        if properties:
            device['properties'] = properties
        if len(device) > 1:
            notifications.append(device)
            latest[heater] = saved
    if not notifications:
        return {'status': 'UNCHANGED', 'heater_control': False}
    ts = max(timestamps)
    http_status = callback(skill_id, token, owner, notifications, ts)
    if http_status != 202:
        return {'status': 'CALLBACK_REJECTED', 'http_status': http_status,
                'heater_control': False}
    services.request('PUT', PUSH_LEDGER, deadline, json.dumps(
        {'schema': 2, 'devices': latest}, separators=(',', ':')).encode())
    return {'status': 'OK', 'reported': [d['id'] for d in notifications],
            'heater_control': False}


def push_from_snapshot(settings, services, callback=send_callback):
    """Periodic fallback for physical changes and sensors; Disk is the source."""
    deadline = time.monotonic() + 12
    snapshot = services.state_snapshot(deadline)
    if not supported(snapshot):
        return {'status': 'NO_ROUTER_STATE', 'heater_control': False}
    updates = {}
    for heater in ('h1', 'h2', 'h3', 'h4'):
        state = snapshot['devices'].get(heater)
        if not isinstance(state, dict) or state.get('heater') != heater:
            continue
        stamp = state.get('observed_at')
        if type(stamp) not in (int, float) or not time.time() - 90 <= stamp <= time.time() + 5:
            continue
        if state.get('online') is False:
            updates[heater] = {'status': ('offline', stamp)}
            continue
        observed = observed_state(snapshot, heater)
        if observed is None:
            continue
        target = observed['capabilities'][1]['state']['value']
        power = observed['capabilities'][0]['state']['value']
        measured = observed['properties'][0]['state']['value']
        field_stamps = state.get('field_observed_at') or {}
        updates[heater] = {
            'status': ('online', stamp),
            'power': (power, field_stamps.get('device_mode', stamp)),
            'target': (target, field_stamps.get('reported_target', stamp)),
            'temperature': (measured, field_stamps.get('current_temperature', stamp)),
        }
    return push_changes(settings, services, updates, callback)


def watch_batch(event, settings=None, services=None, callback=send_callback, seconds=18):
    """From a private async Cloud Function, follow existing signed Disk ACKs.

    The router never calls this function, and its heater is not queried again.
    Only the agent's authenticated session + echo can produce a callback.
    """
    from skill import confirmed
    settings = os.environ if settings is None else settings
    if not isinstance(event, dict) or set(event) != {'heatbridge_watch_batch', 'jobs'}:
        raise ValueError('Invalid watch envelope')
    batch = event['heatbridge_watch_batch']
    jobs = event['jobs']
    if not isinstance(batch, str) or not re.fullmatch(r'[0-9]{10}-[a-f0-9]{64}', batch):
        raise ValueError('Invalid batch ID')
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 8:
        raise ValueError('Invalid jobs')
    identifiers = set()
    for job in jobs:
        if not isinstance(job, dict) or set(job) != {'id', 'heater', 'action', 'value'}:
            raise ValueError('Invalid job')
        if (not isinstance(job['id'], str) or
            not re.fullmatch(r'[a-f0-9]{64}', job['id']) or
            job['id'] in identifiers or
            job['heater'] not in ('h1', 'h2', 'h3', 'h4') or
            job['action'] not in ('set', 'mode') or
            type(job['value']) is not int or
            not ((job['action'] == 'set' and 10 <= job['value'] <= 35) or
                 (job['action'] == 'mode' and job['value'] in (0, 1)))):
            raise ValueError('Invalid job fields')
        identifiers.add(job['id'])
    callback_settings(settings)  # never start a watcher without valid settings
    if settings.get('HEATBRIDGE_TRANSPORT') != 'disk' or not (settings.get('HEATBRIDGE_PUSH_STATE') == '1' or settings.get('HEATBRIDGE_HYBRID_STATE') == '1'):
        raise ValueError('Fast push is disabled')
    close_after = services is None
    if services is None:
        services = from_environment()
    deadline = time.monotonic() + min(max(seconds, 1), 22)
    pending = {job['id']: job for job in jobs}
    completed = []
    try:
        while pending and time.monotonic() < deadline:
            data = services.read_json(f'/acks/{batch}.json', deadline, timeout_cap=1.8)
            if data is not None:
                if not isinstance(data, dict) or data.get('batch') != batch or not isinstance(data.get('acks'), dict):
                    return {'status': 'INVALID_ACK', 'heater_control': False}
                updates = {}
                for job_id, job in list(pending.items()):
                    ack = data['acks'].get(job_id)
                    if ack is None:
                        continue
                    del pending[job_id]
                    if not confirmed(ack, job):
                        continue
                    stamp = ack.get('observed_at')
                    if type(stamp) not in (int, float) or not time.time()-90 <= stamp <= time.time()+5:
                        continue
                    field = 'target' if job['action'] == 'set' else 'power'
                    value = job['value'] if field == 'target' else bool(job['value'])
                    updates.setdefault(job['heater'], {})[field] = (value, stamp)
                    completed.append(job_id)
                if updates:
                    result = push_changes(settings, services, updates, callback,
                                          deadline_seconds=max(.1, deadline-time.monotonic()))
                    if result['status'] == 'CALLBACK_REJECTED':
                        return result
            if pending:
                time.sleep(min(.4, max(0, deadline-time.monotonic())))
        return {'status': 'CONFIRMED' if completed else 'NO_CONFIRMED_ACTION',
                'confirmed': len(completed), 'heater_control': False}
    finally:
        if close_after:
            services.close()


def handler(event, context):
    # Configure private invocation for timer and authenticated async watch only.
    try:
        if isinstance(event, (str, bytes, bytearray)):
            if len(event) > 4096:
                raise ValueError('Invalid watch size')
            event = json.loads(event)
        if isinstance(event, dict) and 'heatbridge_watch_batch' in event:
            return watch_batch(event)
        if os.environ.get('HEATBRIDGE_PUSH_STATE') == '1' or os.environ.get('HEATBRIDGE_HYBRID_STATE') == '1':
            services = from_environment()
            try:
                if getattr(services, 'transport', None) != 'disk':
                    raise ValueError('Push requires Disk')
                return push_from_snapshot(os.environ, services)
            finally:
                services.close()
        return run()
    except (KeyError, ValueError, TypeError):
        return {'status': 'INVALID_CONFIGURATION', 'heater_control': False}
    except ServiceError as error:
        return {'status': 'DISK_ERROR', **error_details(error),
                'heater_control': False}
    except (OSError, http.client.HTTPException):
        return {'status': 'CALLBACK_NETWORK_ERROR', 'heater_control': False}
