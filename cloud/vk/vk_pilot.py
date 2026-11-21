"""Read-only VK mailbox proof-of-concept. No heating commands or credentials in logs.

Cloud -> VK community chat -> router -> VK community chat -> cloud.
Requires a community messages token, an owner chat and a 32-byte shared HMAC key.
"""
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

API_HOST = "api.vk.com"
API_VERSION = "5.199"
VALID_TOKEN = re.compile(r"^[A-Za-z0-9._-]{20,512}$")
VALID_HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")
VALID_NONCE = re.compile(r"^[a-f0-9]{24}$")
VALID_MSG = re.compile(r"^HB1\.(PING|PONG)\.([a-f0-9]{24})\.([0-9]{10})\.([a-f0-9]{64})$")
MAX_AGE = 120


class VkError(Exception):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def settings_from_env(source=None):
    env = os.environ if source is None else source
    token = env["HB_VK_TOKEN"]
    group_id = int(env["HB_VK_GROUP_ID"])
    peer_id = int(env["HB_VK_PEER_ID"])
    shared = env["HB_VK_HMAC_KEY"]
    if (not VALID_TOKEN.fullmatch(token) or not VALID_HEX_KEY.fullmatch(shared)
            or group_id <= 0 or peer_id <= 0 or group_id == peer_id):
        raise ValueError("Invalid VK pilot configuration")
    return {"token": token, "group_id": group_id, "peer_id": peer_id,
            "key": bytes.fromhex(shared)}


def pack(kind, nonce, stamp, key):
    if kind not in ("PING", "PONG") or not VALID_NONCE.fullmatch(nonce):
        raise ValueError("Invalid packet")
    stamp = int(stamp)
    body = f"HB1.{kind}.{nonce}.{stamp}"
    mac = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
    return body + "." + mac


def unpack(message, kind, key, now=None):
    if not isinstance(message, str):
        return None
    match = VALID_MSG.fullmatch(message)
    if match is None or match.group(1) != kind:
        return None
    nonce, stamp, mac = match.group(2), int(match.group(3)), match.group(4)
    now = time.time() if now is None else now
    if not 0 <= now - stamp <= MAX_AGE:
        return None
    body = message[:message.rfind(".")]
    expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
    return (nonce, stamp) if hmac.compare_digest(mac, expected) else None


class VkClient:
    def __init__(self, config, connection_factory=None):
        self.config = config
        self.connection_factory = connection_factory or (
            lambda timeout: http.client.HTTPSConnection(API_HOST, timeout=timeout))

    def call(self, method, params, timeout=6):
        if method not in ("messages.send", "messages.getHistory"):
            raise ValueError("Method not permitted in pilot")
        body = urlencode({**params, "access_token": self.config["token"],
                          "v": API_VERSION}).encode("ascii")
        conn = self.connection_factory(timeout)
        try:
            conn.request("POST", "/method/" + method, body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"})
            resp = conn.getresponse()
            raw = resp.read(65537)
            if resp.status != 200 or len(raw) > 65536:
                raise VkError("HTTP_" + str(resp.status))
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise VkError("INVALID_RESPONSE")
            if "error" in data:
                error = data["error"]
                # Never report VK error_msg; it may contain sensitive inputs.
                code = error.get("error_code", "UNKNOWN") if isinstance(error, dict) else "UNKNOWN"
                raise VkError("API_" + str(code))
            if "response" not in data:
                raise VkError("INVALID_RESPONSE")
            return data["response"]
        except (OSError, http.client.HTTPException):
            raise VkError("CONNECTION") from None
        except (ValueError, UnicodeError) as error:
            if isinstance(error, VkError):
                raise
            raise VkError("INVALID_JSON") from None
        finally:
            conn.close()

    def send(self, text):
        answer = self.call("messages.send", {
            "peer_id": self.config["peer_id"],
            "random_id": secrets.randbelow(2147483646) + 1,
            "message": text})
        if type(answer) is not int or answer <= 0:
            raise VkError("SEND_NOT_CONFIRMED")
        return answer

    def history(self):
        answer = self.call("messages.getHistory", {
            "peer_id": self.config["peer_id"], "count": 100})
        if not isinstance(answer, dict) or not isinstance(answer.get("items"), list):
            raise VkError("INVALID_HISTORY")
        return answer["items"]


def latest_pong(config, client, nonce=None, now=None):
    now = time.time() if now is None else now
    matching = []
    for item in client.history():
        if not isinstance(item, dict):
            continue
        if (item.get("out") != 1 or item.get("peer_id") != config["peer_id"]
                or item.get("from_id") != -config["group_id"]):
            continue
        found = unpack(item.get("text"), "PONG", config["key"], now)
        if found and (nonce is None or nonce == found[0]):
            matching.append((found[1], found[0]))
    if not matching:
        return None
    stamp, answer_nonce = max(matching)
    return {"nonce": answer_nonce, "age_seconds": round(now - stamp, 1)}


def run(event, config=None, client=None):
    if isinstance(event, (str, bytes)):
        event = json.loads(event)
    if not isinstance(event, dict) or set(event) not in (
            {"vk_pilot"}, {"vk_pilot", "nonce"}):
        return {"status": "INVALID_REQUEST", "heater_control": False}
    action = event.get("vk_pilot")
    if action not in ("check_api", "send_ping", "check_pong"):
        return {"status": "INVALID_REQUEST", "heater_control": False}
    config = config or settings_from_env()
    client = client or VkClient(config)
    if action == "check_api":
        items = client.history()
        return {"status": "OK", "history_items": len(items), "heater_control": False}
    if action == "send_ping":
        nonce = secrets.token_hex(12)
        text = pack("PING", nonce, time.time(), config["key"])
        client.send(text)
        return {"status": "PING_SENT", "nonce": nonce, "heater_control": False}
    nonce = event.get("nonce")
    if nonce is not None and not VALID_NONCE.fullmatch(nonce):
        return {"status": "INVALID_REQUEST", "heater_control": False}
    pong = latest_pong(config, client, nonce)
    return {"status": "PONG_RECEIVED", **pong, "heater_control": False} if pong else (
        {"status": "NO_PONG_YET", "heater_control": False})


def handler(event, context):
    try:
        return run(event)
    except (ValueError, KeyError, TypeError, UnicodeError):
        return {"status": "INVALID_CONFIGURATION_OR_INPUT", "heater_control": False}
    except VkError as exc:
        return {"status": "VK_ERROR", "error_code": exc.code, "heater_control": False}
