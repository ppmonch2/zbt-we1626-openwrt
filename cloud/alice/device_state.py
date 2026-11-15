"""Map authenticated, recent router observations to two distinct Alice values."""
import math
import time

STATE_MAX_AGE = 90
FUTURE_TOLERANCE = 5
FLOAT = 'devices.properties.float'


def supported(bundle):
    return (isinstance(bundle, dict) and type(bundle.get('schema')) is int and
            bundle['schema'] == 1 and isinstance(bundle.get('devices'), dict))


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def observed_state(bundle, heater, now=None):
    """Never substitute requested values, old ACKs, or another heater's data."""
    if not supported(bundle):
        return None
    snapshot = bundle['devices'].get(heater)
    if not isinstance(snapshot, dict) or snapshot.get('heater') != heater:
        return None
    stamp = snapshot.get('observed_at')
    now = time.time() if now is None else now
    if not number(stamp, now - STATE_MAX_AGE, now + FUTURE_TOLERANCE):
        return None
    if snapshot.get('online') is not True:
        return None
    result = snapshot.get('result')
    if not isinstance(result, dict) or result.get('encrypted_session') is not True:
        return None
    stamps = snapshot.get('field_observed_at')
    if stamps is not None:
        if not isinstance(stamps, dict) or any(
                not number(stamps.get(field), now - STATE_MAX_AGE, now + FUTURE_TOLERANCE)
                for field in ('reported_target', 'current_temperature', 'device_mode')):
            return None
    target, measured, mode = (result.get(name) for name in
                              ('reported_target', 'current_temperature', 'device_mode'))
    if (not number(target, 5, 35) or not number(measured, -60, 100) or
            type(mode) is not int or mode not in (0, 1, 2, 3)):
        return None
    return {'capabilities': [
        {'type': 'devices.capabilities.on_off', 'state': {'instance': 'on', 'value': mode != 0}},
        {'type': 'devices.capabilities.range', 'state': {'instance': 'temperature', 'value': target}}],
        'properties': [{'type': FLOAT, 'state': {'instance': 'temperature', 'value': measured}}]}
