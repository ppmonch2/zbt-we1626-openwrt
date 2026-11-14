"""Private Yandex Smart Home JSON-RPC provider; no HOMMYN requests or Ballu secrets."""
import hashlib
import json
import os
import re
import time

from yandex_services import ServiceError, from_environment, owner_authorized

RANGE = 'devices.capabilities.range'
POWER = 'devices.capabilities.on_off'
IDS = {'h1', 'h2', 'h3', 'h4'}


def failure(code, message=None):
    return {'status': 'ERROR', 'error_code': code,
            **({'error_message': message} if message else {})}


def command(capability):
    if not isinstance(capability, dict) or not isinstance(capability.get('state'), dict):
        raise ValueError('INVALID_ACTION')
    state = capability['state']
    if capability.get('type') == POWER and state.get('instance') == 'on':
        if type(state.get('value')) is not bool:
            raise ValueError('INVALID_VALUE')
        return 'mode', int(state['value'])
    if capability.get('type') == RANGE and state.get('instance') == 'temperature':
        value = state.get('value')
        if state.get('relative', False) is not False:
            raise ValueError('INVALID_ACTION')
        if type(value) not in (int, float) or not 5 <= value <= 30 or int(value) != value:
            raise ValueError('INVALID_VALUE')
        return 'set', int(value)
    raise ValueError('INVALID_ACTION')


def confirmed(ack, job):
    if not isinstance(ack, dict) or ack.get('id') != job['id'] or ack.get('heater') != job['heater']:
        return False
    if ack.get('action') != job['action'] or type(ack.get('value')) is not int or ack['value'] != job['value']:
        return False
    result = ack.get('result')
    if ack.get('ok') is not True or not isinstance(result, dict):
        return False
    if not all(result.get(k) is True for k in ('encrypted_session', 'command_acknowledged', 'state_echo_matches_request')):
        return False
    field = 'reported_target' if job['action'] == 'set' else 'device_mode'
    return type(result.get(field)) in (int, float) and result[field] == job['value']


def catalog(names):
    return [{'id': key, 'name': name, 'type': 'devices.types.thermostat',
             'description': 'Локальный Ballu через роутер, без HOMMYN',
             'capabilities': [
                 {'type': POWER, 'retrievable': False, 'reportable': False},
                 {'type': RANGE, 'retrievable': False, 'reportable': False,
                  'parameters': {'instance': 'temperature', 'unit': 'unit.temperature.celsius',
                                 'random_access': True, 'range': {'min': 5, 'max': 30, 'precision': 1}}}]}
            for key, name in names.items()]


def process(event, names, owner_uid, services, authorize=owner_authorized):
    deadline = time.monotonic() + 2.35  # Leave room for the platform's network overhead.
    if not isinstance(event, dict) or len(json.dumps(event)) > 16000:
        return {'payload': {'error_code': 'INVALID_ACTION'}}
    headers = event.get('headers')
    headers = headers if isinstance(headers, dict) else {}
    request_id = headers.get('request_id', '')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
        return {'payload': {'error_code': 'INVALID_ACTION'}}
    response = {'request_id': request_id}
    if not authorize(headers.get('authorization'), owner_uid, deadline):
        return {**response, 'payload': {'error_code': 'ACCOUNT_LINKING_ERROR'}}
    kind = event.get('request_type')
    if kind == 'discovery':
        return {**response, 'payload': {'user_id': owner_uid, 'devices': catalog(names)}}
    if kind == 'unlink':
        return response
    payload = event.get('payload')
    devices = payload.get('devices') if isinstance(payload, dict) else None
    if kind not in ('action', 'query') or not isinstance(devices, list) or not 1 <= len(devices) <= 4:
        return {**response, 'payload': {'error_code': 'INVALID_ACTION'}}
    if any(not isinstance(device, dict) or not isinstance(device.get('id'), str) for device in devices):
        return {**response, 'payload': {'error_code': 'INVALID_ACTION'}}
    output, pending = [], []
    for device in devices:
        heater = device['id']
        entry = {'id': heater}
        output.append(entry)
        if heater not in names:
            entry['error_code' if kind == 'query' else 'action_result'] = (
                'DEVICE_NOT_FOUND' if kind == 'query' else failure('DEVICE_NOT_FOUND'))
            continue
        if kind == 'query':
            # Control-only first release: never present desired settings as measured state.
            entry['error_code'] = 'NOT_SUPPORTED_IN_CURRENT_MODE'
            continue
        capabilities = device.get('capabilities')
        if (not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 2 or
                any(not isinstance(capability, dict) for capability in capabilities)):
            entry['action_result'] = failure('INVALID_ACTION')
            continue
        entry['capabilities'] = []
        for capability in capabilities:
            state = capability.get('state')
            instance = state.get('instance', '') if isinstance(state, dict) else ''
            result = {'type': capability.get('type', ''), 'state': {'instance': instance,
                      'action_result': failure('DEVICE_UNREACHABLE', 'No timely heater confirmation; the command may have been applied.')}}
            entry['capabilities'].append(result)
            try:
                action, value = command(capability)
            except ValueError as exc:
                result['state']['action_result'] = failure(str(exc))
                continue
            identity = json.dumps([owner_uid, request_id, heater, action, value], separators=(',', ':'))
            job = {'id': hashlib.sha256(identity.encode()).hexdigest(), 'heater': heater,
                   'action': action, 'value': value,
                   'expires_at': time.time() + max(0, deadline - time.monotonic() - .1)}
            try:
                services.submit(job, deadline)
                pending.append((job, result))
            except ServiceError:
                pass
    while pending and time.monotonic() < deadline:
        for job, result in pending[:]:
            try:
                ack = services.acknowledgement(job['id'], deadline)
            except ServiceError:
                ack = None
            if ack is not None:
                if confirmed(ack, job):
                    result['state']['action_result'] = {'status': 'DONE'}
                pending.remove((job, result))
        if pending:
            time.sleep(min(.08, max(0, deadline - time.monotonic())))
    return {**response, 'payload': {'devices': output}}


def owner_identifier(settings):
    uid = settings.get('HEATBRIDGE_OWNER_UID', '')
    if uid:
        if not re.fullmatch(r'[1-9][0-9]{0,31}', uid):
            raise ValueError('configuration')
        return uid
    login = settings.get('HEATBRIDGE_OWNER_LOGIN', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', login):
        raise ValueError('configuration')
    return 'login:' + login.lower()


def handler(event, context):
    try:
        owner = owner_identifier(os.environ)
        names = json.loads(os.environ['HEATBRIDGE_DEVICES_JSON'])
        if not isinstance(names, dict) or not names:
            raise ValueError('configuration')
        if any(key not in IDS or not isinstance(name, str) or not 1 <= len(name) <= 40 for key, name in names.items()):
            raise ValueError('configuration')
        if isinstance(event, dict) and isinstance(event.get('headers'), dict):
            identity = event['headers'].get('request_id', '')
            if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
                print(json.dumps({'request_id': identity}))
        return process(event, names, owner, from_environment())
    except (KeyError, ValueError, TypeError, ServiceError):
        # Do not echo tokens, credentials, cloud response bodies or environment variables.
        return {'payload': {'error_code': 'INTERNAL_ERROR'}}
