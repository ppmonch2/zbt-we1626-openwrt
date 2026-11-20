"""Private Yandex Smart Home JSON-RPC provider; no HOMMYN requests or Ballu secrets."""
import hashlib
import json
import os
import re
import time

from device_state import FLOAT, STATE_MAX_AGE, observed_setpoint, observed_state, supported

from yandex_services import ServiceError, error_details, from_environment, cached_owner_authorized

RANGE = 'devices.capabilities.range'
POWER = 'devices.capabilities.on_off'
IDS = {'h1', 'h2', 'h3', 'h4'}
# Keep 200 ms of Alice's three-second window for dispatch/serialization/network.
RESPONSE_BUDGET = 2.8
VERSION = '0.2.0'


def failure(code, message=None):
    return {'status': 'ERROR', 'error_code': code,
            **({'error_message': message} if message else {})}


def command(capability, relative_target=None):
    if not isinstance(capability, dict) or not isinstance(capability.get('state'), dict):
        raise ValueError('INVALID_ACTION')
    state = capability['state']
    if capability.get('type') == POWER and state.get('instance') == 'on':
        if type(state.get('value')) is not bool:
            raise ValueError('INVALID_VALUE')
        return 'mode', int(state['value'])
    if capability.get('type') == RANGE and state.get('instance') == 'temperature':
        value = state.get('value')
        relative = state.get('relative', False)
        if relative is True:
            if relative_target is None:
                raise ValueError('INVALID_ACTION')
            if type(value) not in (int, float) or not -25 <= value <= 25 or int(value) != value:
                raise ValueError('INVALID_VALUE')
            value += relative_target
        elif relative is not False:
            raise ValueError('INVALID_ACTION')
        if type(value) not in (int, float) or not 10 <= value <= 35 or int(value) != value:
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


def catalog(names, state_available=False, push=False, hybrid=False):
    return [{'id': key, 'name': name, 'type': 'devices.types.thermostat',
             'description': 'Локальный Ballu через роутер, без HOMMYN',
             'capabilities': [
                 {'type': POWER, 'retrievable': state_available and not push, 'reportable': push or hybrid},
                 {'type': RANGE, 'retrievable': state_available and not push, 'reportable': push or hybrid,
                  'parameters': {'instance': 'temperature', 'unit': 'unit.temperature.celsius',
                                 'random_access': True, 'range': {'min': 10, 'max': 35, 'precision': 1}}}],
             **({'properties': [{'type': FLOAT, 'retrievable': not push, 'reportable': push or hybrid,
                  'parameters': {'instance': 'temperature', 'unit': 'unit.temperature.celsius'}}]}
                if state_available else {})}
            for key, name in names.items()]



def recent_confirmed_overlay(snapshot, heater, state, services, deadline):
    """Keep query retrievable, but never replace a newer confirmed callback
    with the router publisher's older value. The ledger only contains state
    accepted by Alice callback/202, not queued requests or optimistic intents.
    A missing ledger leaves the original router observation unchanged.
    """
    if not isinstance(state, dict) or not supported(snapshot):
        return state
    try:
        remaining = deadline - time.monotonic() - .10
        if remaining <= .15:
            return state
        ledger = services.read_json('/probes/alice-push-ledger.json',
                                    deadline, timeout_cap=min(.9, remaining))
    except (ServiceError, OSError, AttributeError, ValueError):
        return state
    if not isinstance(ledger, dict) or ledger.get('schema') != 2:
        return state
    fields = (ledger.get('devices') or {}).get(heater)
    local = snapshot['devices'].get(heater)
    if not isinstance(fields, dict) or not isinstance(local, dict):
        return state
    local_stamps = local.get('field_observed_at') or {}
    if not isinstance(local_stamps, dict):
        local_stamps = {}
    now = time.time()
    for field, key, cap_type in (
        ('target', 'reported_target', RANGE),
        ('power', 'device_mode', POWER),
    ):
        record = fields.get(field)
        if not isinstance(record, dict):
            continue
        ts = record.get('ts')
        if (type(ts) not in (float, int) or
                not now - 90 <= ts <= now + 5):
            continue
        oldstamp = local_stamps.get(key, local.get('observed_at', 0))
        if type(oldstamp) not in (float, int) or ts <= oldstamp:
            continue
        value = record.get('value')
        if field == 'target':
            if type(value) is not int or not 10 <= value <= 35:
                continue
        elif type(value) is not bool:
            continue
        for cap in state.get('capabilities', []):
            if cap.get('type') == cap_type:
                cap['state']['value'] = value
                break
    return state


def process(event, names, owner_uid, services, authorize=cached_owner_authorized):
    deadline = time.monotonic() + RESPONSE_BUDGET
    if not isinstance(event, dict) or len(json.dumps(event)) > 16000:
        return {'payload': {'error_code': 'INVALID_ACTION'}}
    headers = event.get('headers')
    headers = headers if isinstance(headers, dict) else {}
    request_id = headers.get('request_id', '')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
        return {'payload': {'error_code': 'INVALID_ACTION'}}
    response = {'request_id': request_id}
    def trace(stage, **details):
        print(json.dumps({'request_id': request_id, 'stage': stage, 'version': VERSION,
                          'elapsed_ms': round((time.monotonic() - deadline + RESPONSE_BUDGET) * 1000),
                          **details}))
    if not authorize(headers.get('authorization'), owner_uid, deadline):
        trace('authorization_failed')
        return {**response, 'payload': {'error_code': 'ACCOUNT_LINKING_ERROR'}}
    trace('authorized')
    kind = event.get('request_type')
    disk_transport = getattr(services, 'transport', None) == 'disk'
    async_disk = (disk_transport and os.environ.get('HEATBRIDGE_ASYNC_ACTIONS') == '1')
    push_state = (disk_transport and os.environ.get('HEATBRIDGE_PUSH_STATE') == '1')
    hybrid_state = (disk_transport and os.environ.get('HEATBRIDGE_HYBRID_STATE') == '1')
    snapshot, state_error = None, False
    snapshot_loaded = kind in ('discovery', 'query')
    # Device discovery describes capabilities, not current reachability.
    # A momentary WebDAV failure or no first snapshot must not permanently
    # register Disk-powered devices as retrievable:false in Alice.
    if kind == 'discovery' and disk_transport:
        trace('disk_discovery_without_state_dependency')
        return {**response, 'payload': {'user_id': owner_uid,
                'devices': catalog(names, True, push_state, hybrid_state)}}
    if kind in ('discovery', 'query'):
        try:
            reader = getattr(services, 'state_snapshot', None)
            snapshot = reader(deadline) if reader else None
            state_error = snapshot is not None and not supported(snapshot)
        except ServiceError as exc:
            state_error = True
            trace('state_read_failed', **error_details(exc))
    if kind == 'discovery':
        if state_error:
            return {**response, 'payload': {'error_code': 'INTERNAL_ERROR'}}
        return {**response, 'payload': {'user_id': owner_uid,
                'devices': catalog(names, supported(snapshot), push_state, hybrid_state)}}
    if kind == 'unlink':
        return response
    payload = event.get('payload')
    devices = payload.get('devices') if isinstance(payload, dict) else None
    if kind not in ('action', 'query') or not isinstance(devices, list) or not 1 <= len(devices) <= 4:
        return {**response, 'payload': {'error_code': 'INVALID_ACTION'}}
    if any(not isinstance(device, dict) or not isinstance(device.get('id'), str) for device in devices):
        return {**response, 'payload': {'error_code': 'INVALID_ACTION'}}
    output, pending = [], []
    batch_submit = getattr(services, 'submit_many', None)
    for device in devices:
        heater = device['id']
        entry = {'id': heater}
        output.append(entry)
        if heater not in names:
            entry['error_code' if kind == 'query' else 'action_result'] = (
                'DEVICE_NOT_FOUND' if kind == 'query' else failure('DEVICE_NOT_FOUND'))
            continue
        if kind == 'query':
            if snapshot is None and not state_error:
                # Cloud can be installed first, while the old router has no
                # publisher. Discovery still advertises retrievable:false.
                entry['capabilities'] = []
                trace('router_state_publisher_not_installed')
            else:
                state = observed_state(snapshot, heater)
                if state is None:
                    entry['error_code'] = 'DEVICE_UNREACHABLE'
                    trace('state_missing_stale_or_invalid', heater=heater)
                else:
                    if hybrid_state:
                        state = recent_confirmed_overlay(snapshot, heater, state,
                                                         services, deadline)
                    entry.update(state)
                    trace('state_returned', heater=heater)
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
                relative_target = None
                if (capability.get('type') == RANGE and isinstance(state, dict) and
                        state.get('instance') == 'temperature' and state.get('relative') is True):
                    reader = getattr(services, 'state_snapshot', None)
                    if reader:
                        if not snapshot_loaded:
                            snapshot_loaded = True
                            try:
                                snapshot = reader(deadline)
                            except ServiceError:
                                snapshot = None
                        relative_target = observed_setpoint(snapshot, heater)
                        if relative_target is None:
                            raise ValueError('DEVICE_UNREACHABLE')
                action, value = command(capability, relative_target)
            except ValueError as exc:
                result['state']['action_result'] = failure(str(exc))
                continue
            identity = json.dumps([owner_uid, request_id, heater, action, value], separators=(',', ':'))
            job = {'id': hashlib.sha256(identity.encode()).hexdigest(), 'heater': heater,
                   'action': action, 'value': value,
                   'expires_at': time.time() + max(0, deadline - time.monotonic() - .1)}
            if batch_submit:
                pending.append((job, result))
                continue
            try:
                services.submit(job, deadline)
                trace('queue_submitted', job_id=job['id'])
                pending.append((job, result))
            except ServiceError as exc:
                trace('queue_submit_failed', **error_details(exc))
    if batch_submit and pending:
        try:
            batch_submit([job for job, result in pending], deadline)
            trace('disk_batch_submitted', commands=len(pending), batch_id=getattr(services, 'batch', None))
            if async_disk and time.monotonic() < deadline:
                # A separate, private Cloud Function watches only the verified
                # ACK on Disk. This is best effort; periodic notifications remain
                # a recovery fallback. No router-to-cloud HTTPS is required.
                if push_state or hybrid_state:
                    try:
                        from fast_push import invoke
                        dispatched = invoke(getattr(services, 'batch', ''),
                                            [job for job, _ in pending],
                                            os.environ.get('HEATBRIDGE_FAST_NOTIFY_FUNCTION_ID', ''),
                                            deadline)
                        trace('fast_notify_accepted' if dispatched else 'fast_notify_not_accepted')
                    except (ValueError, KeyError, OSError, TypeError):
                        trace('fast_notify_unavailable')
                # DONE acknowledges a durable mailbox write, never heater execution.
                # Real state must be delivered via confirmed callback.
                for job, result in pending:
                    result['state']['action_result'] = {'status': 'DONE'}
                trace('disk_async_queued_for_callback', commands=len(pending))
                pending.clear()
        except ServiceError as exc:
            trace('disk_batch_submit_failed', **error_details(exc))
            pending.clear()
    ack_errors = set()
    while pending and time.monotonic() < deadline:
        batch_acks = None
        if hasattr(services, 'acknowledgements'):
            try:
                batch_acks = services.acknowledgements(deadline)
            except ServiceError as exc:
                if 'batch' not in ack_errors:
                    trace('ack_read_failed', **error_details(exc))
                    ack_errors.add('batch')
                batch_acks = {}
        for job, result in pending[:]:
            try:
                ack = (batch_acks.get(job['id']) if batch_acks is not None else
                       services.acknowledgement(job['id'], deadline))
            except ServiceError as exc:
                if job['id'] not in ack_errors:
                    trace('ack_read_failed', **error_details(exc))
                    ack_errors.add(job['id'])
                ack = None
            if ack is not None:
                if time.monotonic() < deadline and confirmed(ack, job):
                    result['state']['action_result'] = {'status': 'DONE'}
                    trace('heater_confirmed', job_id=job['id'])
                else:
                    trace('heater_confirmation_rejected', job_id=job['id'])
                pending.remove((job, result))
        if pending:
            time.sleep(min(getattr(services, 'poll_interval', .03), max(0, deadline - time.monotonic())))
    if pending:
        trace('confirmation_deadline')
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
    # Console tests use integration=raw: Python may receive the JSON as text/bytes.
    if isinstance(event, (str, bytes, bytearray)):
        if len(event) > 16000:
            return {'payload': {'error_code': 'INVALID_ACTION'}}
        try:
            event = json.loads(event)
        except (ValueError, UnicodeError):
            return {'payload': {'error_code': 'INVALID_ACTION'}}
    if event == {'heatbridge_diagnostic': 'version'}:
        disk_transport = os.environ.get('HEATBRIDGE_TRANSPORT', 'cloud') == 'disk'
        return {'version': VERSION, 'heater_control': False,
                'transport': os.environ.get('HEATBRIDGE_TRANSPORT', 'cloud'),
                'parallel_ack_connection': not disk_transport, 'owner_check_cache_seconds': 30,
                'response_budget_seconds': RESPONSE_BUDGET,
                'state_reporting': 'auto', 'state_max_age_seconds': STATE_MAX_AGE,
                'push_state': (disk_transport and os.environ.get('HEATBRIDGE_PUSH_STATE') == '1'),
                'hybrid_state': (disk_transport and os.environ.get('HEATBRIDGE_HYBRID_STATE') == '1'),
                'fast_push_configured': bool(os.environ.get('HEATBRIDGE_FAST_NOTIFY_FUNCTION_ID')),
                'state_router_min_version': '0.2.0' if disk_transport else '0.1.3'}
    if event == {'heatbridge_diagnostic': 'disk_probe'}:
        services = None
        try:
            services = from_environment()
            if getattr(services, 'transport', None) != 'disk':
                return {'diagnostic': 'disk_probe', 'heater_control': False, 'status': 'SELECT_DISK_TRANSPORT'}
            return {'diagnostic': 'disk_probe', **services.probe(time.monotonic() + 2.8)}
        except (KeyError, ValueError, TypeError):
            return {'diagnostic': 'disk_probe', 'heater_control': False, 'status': 'INVALID_CONFIGURATION'}
        except ServiceError as exc:
            return {'diagnostic': 'disk_probe', 'heater_control': False, 'status': 'ERROR',
                    **error_details(exc),
                    **({'phase': exc.probe_phase} if getattr(exc, 'probe_phase', None) in
                       ('write', 'read', 'delete') else {}),
                    **({'network_stage': exc.network_stage}
                       if getattr(exc, 'network_stage', None) in
                       ('connect', 'send', 'response', 'body') else {}),
                    **({'network_kind': exc.network_kind, 'elapsed_ms': exc.elapsed_ms}
                       if getattr(exc, 'network_kind', None) is not None else {})}
        finally:
            if services:
                services.close()
    if event == {'heatbridge_diagnostic': 'disk_status'}:
        services = None
        result = {'diagnostic': 'disk_status', 'heater_control': False, 'version': VERSION}
        try:
            services = from_environment()
            if getattr(services, 'transport', None) != 'disk':
                return {**result, 'status': 'SELECT_DISK_TRANSPORT'}
            snapshot = services.state_snapshot(time.monotonic() + 2.5)
            if snapshot is None:
                return {**result, 'status': 'NO_ROUTER_STATE'}
            if not supported(snapshot):
                return {**result, 'status': 'INVALID_ROUTER_STATE'}
            return {**result, 'status': 'OK', 'published_at': snapshot.get('published_at'),
                    'devices': {heater: (observed_state(snapshot, heater) or
                                        {'error_code': 'DEVICE_UNREACHABLE'}) for heater in sorted(IDS)}}
        except (KeyError, ValueError, TypeError):
            return {**result, 'status': 'INVALID_CONFIGURATION'}
        except ServiceError as exc:
            return {**result, 'status': 'ERROR', **error_details(exc)}
        finally:
            if services:
                services.close()
    # This exact console-only envelope cannot supply a live job or credentials.
    # expires_at=0 ensures the router rejects the probe before hardware access.
    if event == {'heatbridge_diagnostic': 'expired_queue_probe'}:
        from diagnostics import probe
        return probe()
    if (isinstance(event, dict) and set(event) == {'heatbridge_diagnostic', 'job_id'}
            and event.get('heatbridge_diagnostic') == 'read_ack'):
        from diagnostics import read_ack_probe
        return read_ack_probe(event['job_id'])
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
        services = from_environment()
        try:
            if os.environ.get('HEATBRIDGE_HYBRID_STATE') == '1' and os.environ.get('HEATBRIDGE_PUSH_STATE') == '1':
                raise ValueError('Cannot select both push-only and hybrid')
            if os.environ.get('HEATBRIDGE_PUSH_STATE') == '1' or os.environ.get('HEATBRIDGE_HYBRID_STATE') == '1':
                # Never disable Yandex query before private callback delivery is configured.
                if getattr(services, 'transport', None) != 'disk' or os.environ.get('HEATBRIDGE_ASYNC_ACTIONS') != '1':
                    raise ValueError('Push requires Disk and async callbacks')
                if not re.fullmatch(r'[A-Za-z0-9_-]{6,128}',
                                    os.environ.get('HEATBRIDGE_FAST_NOTIFY_FUNCTION_ID', '')):
                    raise ValueError('Missing private notifier ID')
            if os.environ.get('HEATBRIDGE_ASYNC_ACTIONS') == '1':
                # Never claim async success unless the notification publisher
                # has been explicitly configured (and its timer activated).
                if getattr(services, 'transport', None) != 'disk':
                    raise ValueError('Async actions require Disk')
                if not re.fullmatch(r'[A-Za-z0-9_-]{6,128}',
                                    os.environ.get('HEATBRIDGE_DIALOGS_SKILL_ID', '')):
                    raise ValueError('Missing Dialogs skill ID')
                dialogs_token = os.environ.get('HEATBRIDGE_DIALOGS_OAUTH_TOKEN', '')
                if not 16 <= len(dialogs_token) <= 2048 or any(c.isspace() for c in dialogs_token):
                    raise ValueError('Missing Dialogs OAuth token')
            return process(event, names, owner, services)
        finally:
            close = getattr(services, 'close', None)
            if close:
                close()
    except (KeyError, ValueError, TypeError, ServiceError):
        # Do not echo tokens, credentials, cloud response bodies or environment variables.
        return {'payload': {'error_code': 'INTERNAL_ERROR'}}
