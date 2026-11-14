"""Small, dependency-free HTTPS clients. Runs in Cloud Functions, not on the router."""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class ServiceError(Exception):
    def __init__(self, reason, *, service=None, http_status=None, cloud_code=None):
        super().__init__(reason)
        self.service = service
        self.http_status = http_status
        self.cloud_code = cloud_code


def error_details(error):
    # Never log exception text, response bodies, URLs or signed request headers.
    details = {}
    if error.service in ('sqs', 's3'):
        details['service'] = error.service
    if type(error.http_status) is int:
        details['http_status'] = error.http_status
    allowed = {'AccessDenied', 'AccessDeniedException', 'SignatureDoesNotMatch',
               'InvalidClientTokenId', 'InvalidAccessKeyId', 'RequestTimeTooSkewed',
               'ExpiredToken', 'NonExistentQueue', 'AWS.SimpleQueueService.NonExistentQueue'}
    if error.cloud_code in allowed:
        details['cloud_code'] = error.cloud_code
    reason = error.args[0] if error.args else None
    if reason in ('deadline', 'cloud_http_error', 'cloud_connection_error',
                  'oversized_response', 'queue_rejected', 'invalid_queue_response',
                  'invalid_acknowledgement'):
        details['reason'] = reason
    return details


def signed_headers(method, url, body, service, access, secret, instant=None):
    instant = instant or datetime.datetime.now(datetime.timezone.utc)
    stamp = instant.strftime('%Y%m%dT%H%M%SZ')
    day = stamp[:8]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query:
        raise ValueError('Only HTTPS requests without a query are supported')
    digest = hashlib.sha256(body).hexdigest()
    fields = {'host': parsed.netloc, 'x-amz-content-sha256': digest, 'x-amz-date': stamp}
    names = ';'.join(sorted(fields))
    canonical_headers = ''.join(f'{key}:{fields[key]}\n' for key in sorted(fields))
    # Callers supply a percent-encoded canonical path (S3 paths retain slashes).
    canonical = '\n'.join([method, parsed.path or '/', '', canonical_headers, names, digest])
    scope = f'{day}/ru-central1/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ('AWS4' + secret).encode()
    for part in (day, 'ru-central1', service, 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    fields['Authorization'] = f'AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={names}, Signature={signature}'
    return fields


class Services:
    def __init__(self, queue, bucket, access, secret, opener=urllib.request.urlopen):
        if not re.fullmatch(r'https://message-queue\.api\.cloud\.yandex\.net/[A-Za-z0-9_./-]+', queue):
            raise ValueError('Invalid queue URL')
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise ValueError('Invalid bucket')
        self.queue, self.bucket = queue, bucket
        self.access, self.secret, self.opener = access, secret, opener

    def request(self, method, url, body, service, deadline, content_type=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError('deadline')
        headers = signed_headers(method, url, body, service, self.access, self.secret)
        if content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=body if method != 'GET' else None, headers=headers, method=method)
        try:
            with self.opener(req, timeout=min(.65, remaining)) as response:
                data = response.read(65537)
                if len(data) > 65536:
                    raise ServiceError('oversized_response')
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and service == 's3' and method == 'GET':
                return None
            code = None
            try:
                code = ET.fromstring(exc.read(4096)).findtext('.//{*}Code')
            except (ET.ParseError, OSError, TypeError, AttributeError):
                pass
            raise ServiceError('cloud_http_error', service=service,
                               http_status=exc.code, cloud_code=code) from None
        except (OSError, ValueError) as exc:
            raise ServiceError('cloud_connection_error', service=service) from None

    def submit(self, job, deadline):
        # Query protocol is supported by YMQ; do not assume AWS's newer JSON API.
        encoded = base64.b64encode(json.dumps(job, separators=(',', ':')).encode()).decode()
        body = urllib.parse.urlencode({'Action': 'SendMessage', 'Version': '2012-11-05',
                                      'QueueUrl': self.queue, 'MessageBody': encoded}).encode()
        data = self.request('POST', 'https://message-queue.api.cloud.yandex.net/', body, 'sqs', deadline,
                            'application/x-www-form-urlencoded')
        try:
            if ET.fromstring(data).find('.//{*}MessageId') is None:
                raise ServiceError('queue_rejected')
        except (ET.ParseError, TypeError):
            raise ServiceError('invalid_queue_response') from None

    def acknowledgement(self, job_id, deadline):
        if not re.fullmatch(r'[a-f0-9]{64}', job_id):
            raise ValueError('Invalid job ID')
        url = f'https://storage.yandexcloud.net/{self.bucket}/acks/{job_id}.json'
        data = self.request('GET', url, b'', 's3', deadline)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ServiceError('invalid_acknowledgement') from None


def owner_authorized(authorization, owner_uid, deadline, opener=urllib.request.urlopen):
    if not isinstance(authorization, str) or not re.fullmatch(r'Bearer [^\s]{1,2048}', authorization):
        return False
    token = authorization[7:]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    req = urllib.request.Request('https://login.yandex.ru/info?format=json',
                                 headers={'Authorization': 'OAuth ' + token})
    try:
        with opener(req, timeout=min(.65, remaining)) as response:
            raw = response.read(8193)
            if len(raw) > 8192:
                return False
            info = json.loads(raw)
            if not isinstance(info, dict) or not re.fullmatch(r'[1-9][0-9]{0,31}', str(info.get('id', ''))):
                return False
            if owner_uid.startswith('login:'):
                login = info.get('login')
                return isinstance(login, str) and login.lower() == owner_uid[6:]
            return str(info['id']) == owner_uid
    except (OSError, ValueError, UnicodeError):
        return False


def from_environment():
    return Services(os.environ['HEATBRIDGE_QUEUE_URL'], os.environ['HEATBRIDGE_ACK_BUCKET'],
                    os.environ['AWS_ACCESS_KEY_ID'], os.environ['AWS_SECRET_ACCESS_KEY'])
