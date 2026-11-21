"""Offline contract tests. No real VK token, router, or heater is accessed."""
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud" / "vk"))
import vk_pilot

CONFIG = {
    "token": "fixture-not-a-real-vk-token-2026",
    "group_id": 14567,
    "peer_id": 84562,
    "key": bytes.fromhex("01" * 32),
}


class FakeVK:
    def __init__(self, history=None):
        self.items = list(history or [])
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return 100

    def history(self):
        return self.items


def outbound(text):
    return {"out": 1, "peer_id": CONFIG["peer_id"], "from_id": -CONFIG["group_id"],
            "text": text, "date": int(time.time())}


class VkPilotTests(unittest.TestCase):
    def test_config_requires_community_token_peer_and_secret(self):
        source = {"HB_VK_TOKEN": CONFIG["token"],
                  "HB_VK_GROUP_ID": str(CONFIG["group_id"]),
                  "HB_VK_PEER_ID": str(CONFIG["peer_id"]),
                  "HB_VK_HMAC_KEY": "01" * 32}
        self.assertEqual(vk_pilot.settings_from_env(source), CONFIG)
        for field in source:
            malformed = dict(source)
            malformed[field] = "invalid"
            with self.assertRaises((KeyError, ValueError)):
                vk_pilot.settings_from_env(malformed)

    def test_ping_is_signed_and_sent_without_heater_actions(self):
        fake = FakeVK()
        result = vk_pilot.run({"vk_pilot": "send_ping"}, CONFIG, fake)
        self.assertEqual(result["status"], "PING_SENT")
        self.assertTrue(result["heater_control"] is False)
        self.assertEqual(len(fake.sent), 1)
        nonce, stamp = vk_pilot.unpack(fake.sent[0], "PING", CONFIG["key"])
        self.assertEqual(nonce, result["nonce"])
        self.assertAlmostEqual(stamp, time.time(), delta=2)

    def test_only_outbound_same_group_same_peer_signed_pong_is_accepted(self):
        msg = vk_pilot.pack("PONG", "ab"*12, time.time(), CONFIG["key"])
        fake = FakeVK([outbound(msg)])
        result = vk_pilot.run({"vk_pilot":"check_pong", "nonce":"ab"*12}, CONFIG, fake)
        self.assertEqual(result["status"], "PONG_RECEIVED")
        self.assertEqual(result["nonce"], "ab"*12)
        for mutation in (
            lambda r: r.update(out=0),
            lambda r: r.update(peer_id=424242),
            lambda r: r.update(from_id=-424242),
            lambda r: r.update(text=r["text"][:-1]+"0" if r["text"][-1]!="0" else r["text"][:-1]+"1"),
        ):
            other = outbound(msg)
            mutation(other)
            result = vk_pilot.run({"vk_pilot":"check_pong"}, CONFIG, FakeVK([other]))
            self.assertEqual(result["status"], "NO_PONG_YET")

    def test_replay_expired_and_future_messages_are_rejected(self):
        nonce = "f1"*12
        self.assertIsNone(vk_pilot.unpack(vk_pilot.pack("PONG",nonce,time.time()-121,CONFIG["key"]),
                                         "PONG",CONFIG["key"]))
        self.assertIsNone(vk_pilot.unpack(vk_pilot.pack("PONG",nonce,time.time()+10,CONFIG["key"]),
                                         "PONG",CONFIG["key"]))
        self.assertIsNone(vk_pilot.unpack(vk_pilot.pack("PING",nonce,time.time(),CONFIG["key"]),
                                         "PONG",CONFIG["key"]))

    def test_error_no_secrets_and_read_only_actions(self):
        self.assertEqual(vk_pilot.run({"vk_pilot":"set_heater","heater":"h1"},CONFIG,FakeVK()),
                         {"status":"INVALID_REQUEST","heater_control":False})
        self.assertEqual(vk_pilot.run({"vk_pilot":"check_api"},CONFIG,FakeVK())["status"],"OK")

    def test_cloud_client_rejects_non_whitelisted_vk_api_calls(self):
        client = vk_pilot.VkClient(CONFIG)
        with self.assertRaises(ValueError):
            client.call("messages.delete",{})

    def test_handler_never_echoes_token_or_key_in_error(self):
        with patch.dict("os.environ", {"HB_VK_TOKEN":"something-invalid"}, clear=True):
            result=vk_pilot.handler({"vk_pilot":"check_api"},None)
        self.assertEqual(result["status"],"INVALID_CONFIGURATION_OR_INPUT")
        self.assertNotIn("something",repr(result))


class VkTelemetryTests(unittest.TestCase):
    def make_state(self, nonce="aa"*12, observed=None):
        stamp = int(time.time())
        if observed is None:
            observed = stamp-4
        bodydata = {"schema": 1, "devices": {
            "h1": {"observed_at": observed, "reported_target": 25,
                   "current_temperature": 19.5, "device_mode": 1},
            "h2": {"observed_at": observed, "reported_target": 21,
                   "current_temperature": 20.0, "device_mode": 0}}}
        import base64
        encoded = base64.urlsafe_b64encode(json.dumps(bodydata, separators=(",", ":")).encode()).decode().rstrip("=")
        body = f"HB2.STATE.{nonce}.{stamp}.{encoded}"
        mac = hmac.new(CONFIG["key"], body.encode(), hashlib.sha256).hexdigest()
        return body + "." + mac

    def test_request_is_read_only_and_hmac_authenticated(self):
        fake = FakeVK()
        response = vk_pilot.run({"vk_pilot": "request_states"}, CONFIG, fake)
        self.assertEqual(response["status"], "STATE_REQUEST_SENT")
        self.assertFalse(response["heater_control"])
        self.assertEqual(len(fake.sent), 1)
        self.assertTrue(fake.sent[0].startswith("HB2.READ." + response["nonce"]))
        wrong_key = b"2"*32
        body, signature = fake.sent[0].rsplit(".", 1)
        self.assertNotEqual(signature, hmac.new(wrong_key, body.encode(), hashlib.sha256).hexdigest())

    def test_response_validates_payload_and_origin_and_nonce(self):
        nonce = "aa"*12
        payload = self.make_state(nonce)
        client = FakeVK([outbound(payload)])
        result = vk_pilot.run({"vk_pilot":"check_states", "nonce":nonce}, CONFIG, client)
        self.assertEqual(result["status"], "STATE_RECEIVED")
        self.assertEqual(result["devices"]["h1"]["reported_target"], 25)
        self.assertTrue(result["devices"]["h1"]["fresh"])
        for alter in (
            lambda item: item.update(peer_id=123),
            lambda item: item.update(from_id=-123),
            lambda item: item.update(out=0),
            lambda item: item.update(text=item["text"][:-1]+"f" if item["text"][-1] != "f" else item["text"][:-1]+"e"),
        ):
            item = outbound(payload)
            alter(item)
            result = vk_pilot.run({"vk_pilot":"check_states", "nonce":nonce},CONFIG,FakeVK([item]))
            self.assertEqual(result["status"], "NO_STATE_YET")

    def test_stale_states_labeled_not_fresh(self):
        result = vk_pilot.decode_state(self.make_state(observed=time.time()-300), CONFIG["key"])
        self.assertIsNotNone(result)
        self.assertFalse(result["devices"]["h2"]["fresh"])

    def test_message_age_rejects_expired_and_future(self):
        state = self.make_state()
        self.assertIsNotNone(vk_pilot.decode_state(state,CONFIG["key"]))
        self.assertIsNone(vk_pilot.decode_state(state,CONFIG["key"],now=time.time()+121))
        self.assertIsNone(vk_pilot.decode_state(state,CONFIG["key"],now=time.time()-5))

    def test_invalid_nonce_does_not_trigger_history(self):
        result = vk_pilot.run({"vk_pilot":"check_states","nonce":"bad"},CONFIG,FakeVK())
        self.assertEqual(result["status"], "INVALID_REQUEST")

if __name__ == "__main__":
    unittest.main()
