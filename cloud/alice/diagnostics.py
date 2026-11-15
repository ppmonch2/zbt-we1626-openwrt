"""Console probe: only an already expired queue message; never a live heater job."""
import hashlib
import re
import time

from yandex_services import ServiceError, error_details, from_environment


def read_ack_probe(job_id):
    """Read one existing acknowledgement; never enqueue or execute a command."""
    result = {'diagnostic': 'read_ack', 'version': '0.1.6', 'heater_control': False}
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{64}', job_id):
        return {**result, 'error': 'invalid_job_id'}
    try:
        services = from_environment()
    except (KeyError, ValueError, TypeError):
        return {**result, 'configuration': 'invalid'}
    started = time.monotonic()
    result['bucket'] = services.bucket
    try:
        ack = services.acknowledgement(job_id, started + 2.35)
        result.update(status='OK', object_found=ack is not None)
        if isinstance(ack, dict):
            from skill import confirmed
            job = {key: ack.get(key) for key in ('heater', 'action', 'value')}
            job['id'] = job_id
            result['valid_confirmation'] = confirmed(ack, job)
    except ServiceError as exc:
        result.update(status='ERROR', **error_details(exc))
    finally:
        result['read_ms'] = round((time.monotonic() - started) * 1000)
        close = getattr(services, 'close', None)
        if close:
            close()
    return result


def probe():
    result = {'diagnostic': 'expired_queue_probe', 'heater_control': False}
    deadline = time.monotonic() + 2.35
    try:
        services = from_environment()
    except (KeyError, ValueError, TypeError):
        return {**result, 'configuration': 'invalid'}
    job = {'id': hashlib.sha256(str(time.time_ns()).encode()).hexdigest(),
           'heater': 'h2', 'action': 'set', 'value': 22, 'expires_at': 0}
    try:
        services.submit(job, deadline)
        result['queue_send'] = {'status': 'OK'}
    except ServiceError as exc:
        result['queue_send'] = {'status': 'ERROR', **error_details(exc)}
    try:
        ack = services.acknowledgement(job['id'], deadline)
        result['ack_read'] = {'status': 'OK', 'object_found': ack is not None}
    except ServiceError as exc:
        result['ack_read'] = {'status': 'ERROR', **error_details(exc)}
    finally:
        close = getattr(services, 'close', None)
        if close:
            close()
    return result
