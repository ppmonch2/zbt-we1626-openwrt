"""Console probe: only an already expired queue message; never a live heater job."""
import hashlib
import time

from yandex_services import ServiceError, error_details, from_environment


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
    return result
