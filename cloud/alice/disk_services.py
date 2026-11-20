"""Private WebDAV mailbox. No public links, redirects, SDKs or router tokens."""
import base64
import hashlib
import http.client
import json
import re
import time
import uuid

from yandex_services import ServiceError


class DiskServices:
    transport = 'disk'
    poll_interval = .12

    def __init__(self, login, password, folder='heatbridge-v1', connection=None):
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9@._+-]{1,120}', login):
            raise ValueError('Invalid Disk login')
        if not isinstance(password, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', password):
            raise ValueError('Invalid application password')
        if not isinstance(folder, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', folder):
            raise ValueError('Invalid Disk folder')
        self.root = '/' + folder
        self.authorization = 'Basic ' + base64.b64encode((login + ':' + password).encode()).decode()
        self.connection = connection
        self.batch = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def request(self, method, path, deadline, body=b'', missing=False, timeout_cap=1.35):
        if not re.fullmatch(r'/(commands|acks|probes)/[A-Za-z0-9_.-]+', path):
            raise ValueError('Invalid Disk path')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError('deadline', service='webdav')
        timeout = min(timeout_cap, remaining)
        if self.connection is None:
            self.connection = http.client.HTTPSConnection('webdav.yandex.ru', timeout=timeout)
        connection = self.connection
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
        started = time.monotonic()
        stage = 'connect'
        try:
            if isinstance(connection, http.client.HTTPSConnection) and connection.sock is None:
                connection.connect()
            stage = 'send'
            connection.request(method, self.root + path, body=body, headers={
                'Authorization': self.authorization, 'Content-Type': 'application/json',
                'Cache-Control': 'no-cache', 'Accept': 'application/json'})
            stage = 'response'
            response = connection.getresponse()
            stage = 'body'
            status = response.status
            data = response.read(65537)
            response.close()
            if len(data) > 65536:
                self.close()
                raise ServiceError('oversized_response', service='webdav')
            if status == 404 and missing:
                return None
            if not 200 <= status < 300:
                # Do not follow redirects or expose response bodies/credentials.
                raise ServiceError('cloud_http_error', service='webdav', http_status=status)
            return data
        except (OSError, http.client.HTTPException) as problem:
            self.close()
            # A timed-out PUT might already have been committed. Never retry it.
            error = ServiceError('cloud_connection_error', service='webdav')
            error.network_stage = stage
            kind = type(problem).__name__
            error.network_kind = kind if kind in (
                'TimeoutError', 'ConnectionResetError', 'ConnectionRefusedError',
                'BrokenPipeError', 'gaierror', 'SSLError', 'SSLEOFError',
                'RemoteDisconnected', 'CannotSendRequest', 'ResponseNotReady',
                'OSError') else 'other_network_error'
            error.elapsed_ms = round((time.monotonic() - started) * 1000)
            raise error from None

    def read_json(self, path, deadline, timeout_cap=1.35):
        data = self.request('GET', path, deadline, missing=True, timeout_cap=timeout_cap)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ServiceError('invalid_acknowledgement', service='webdav') from None

    def submit_many(self, jobs, deadline):
        if not 1 <= len(jobs) <= 8:
            raise ValueError('Invalid batch')
        identity = hashlib.sha256(json.dumps([job['id'] for job in jobs]).encode()).hexdigest()
        self.batch = f'{int(time.time()):010d}-{identity}'
        body = json.dumps({'schema': 1, 'batch': self.batch, 'jobs': jobs},
                          separators=(',', ':'), allow_nan=False).encode()
        # A command has one durable PUT; its response may take longer than
        # the fast ACK polls. Bound it by Alice's overall request deadline.
        self.request('PUT', f'/commands/{self.batch}.json', deadline, body,
                     timeout_cap=2.3)

    def submit(self, job, deadline):
        self.submit_many([job], deadline)

    def acknowledgements(self, deadline):
        if self.batch is None:
            return {}
        data = self.read_json(f'/acks/{self.batch}.json', deadline)
        if data is None:
            return {}
        if (not isinstance(data, dict) or data.get('batch') != self.batch or
                not isinstance(data.get('acks'), dict)):
            raise ServiceError('invalid_acknowledgement', service='webdav')
        return data['acks']

    def acknowledgement(self, job_id, deadline):
        if not re.fullmatch(r'[a-f0-9]{64}', job_id):
            raise ValueError('Invalid job ID')
        return self.acknowledgements(deadline).get(job_id)

    def state_snapshot(self, deadline):
        # Queries have only one WebDAV GET. Allow a slower but legitimate
        # response without changing the short polls used for action ACKs.
        remaining = deadline - time.monotonic() - .15
        if remaining <= 0:
            raise ServiceError('deadline', service='webdav')
        return self.read_json('/acks/state-v1.json', deadline,
                              timeout_cap=min(2.3, remaining))

    def probe(self, deadline):
        """Exercise exactly the PUT+GET path used by live commands and status.

        A DELETE is not required for command delivery. WebDAV deletion may block
        well beyond Alice's three-second window, causing a false failed probe.
        Reuse one inert probe document, overwriting a fresh nonce each time so
        diagnostics never accumulate objects or touch any executable commands.
        """
        path = '/probes/cloud-check.json'
        body = json.dumps({'probe': uuid.uuid4().hex}).encode()
        phase = 'write'
        try:
            self.request('PUT', path, deadline, body, timeout_cap=2.4)
            phase = 'read'
            if self.request('GET', path, deadline) != body:
                raise ServiceError('invalid_probe_response', service='webdav')
        except ServiceError as exc:
            # Report only the safe operation name; never expose credentials or paths.
            exc.probe_phase = phase
            raise
        return {'status': 'OK', 'write_read': True, 'heater_control': False}
