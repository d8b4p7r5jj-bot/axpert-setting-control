"""Offline unit tests for the MQTT setting-control path; no inverter required."""
import importlib.util
import json
import pathlib
import queue
import sys
import types
import unittest
from unittest.mock import patch

# The production container provides these dependencies. Stub them here so the
# command-validation and queue logic can be tested without hardware/packages.
crcmod = types.ModuleType("crcmod")
crcmod.predefined = types.SimpleNamespace(mkCrcFun=lambda _name: (lambda _data: 0))
sys.modules.setdefault("crcmod", crcmod)
sys.modules.setdefault("crcmod.predefined", crcmod.predefined)
paho = types.ModuleType("paho")
mqtt = types.ModuleType("paho.mqtt")
client_module = types.ModuleType("paho.mqtt.client")
client_module.Client = object
mqtt.client = client_module
paho.mqtt = mqtt
sys.modules.setdefault("paho", paho)
sys.modules.setdefault("paho.mqtt", mqtt)
sys.modules.setdefault("paho.mqtt.client", client_module)

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("monitor_under_test", HERE / "monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

class FakeClient:
    def __init__(self):
        self.published = []
    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))

class FakeMessage:
    def __init__(self, payload, retain=False):
        self.payload = payload.encode()
        self.retain = retain

class SettingControlTests(unittest.TestCase):
    def test_allowed_commands(self):
        cases = [
            ({"id":"a", "setting":"output_source_priority", "value":"01"}, "POP01"),
            ({"id":"b", "setting":"charger_source_priority", "value":"03"}, "PCP03"),
            ({"id":"c", "setting":"ac_input_range", "value":"01"}, "PGR01"),
        ]
        for request, expected in cases:
            with self.subTest(request=request):
                self.assertEqual(monitor.validate_setting_request(json.dumps(request), True), (request["id"], expected, None))

    def test_disabled_rejects(self):
        _id, command, error = monitor.validate_setting_request('{"setting":"output_source_priority","value":"01"}', False)
        self.assertIsNone(command)
        self.assertIn("disabled", error)

    def test_unknown_setting_and_invalid_value_rejected(self):
        for payload in (
            '{"setting":"raw_command","value":"PF"}',
            '{"setting":"output_source_priority","value":"99"}',
            'not-json',
        ):
            with self.subTest(payload=payload):
                _id, command, error = monitor.validate_setting_request(payload, True)
                self.assertIsNone(command)
                self.assertTrue(error)

    def test_retained_command_never_queued(self):
        client, q = FakeClient(), queue.Queue(maxsize=10)
        monitor.handle_command_message(client, FakeMessage('{"id":"x","setting":"output_source_priority","value":"01"}', retain=True), q, True, "result")
        self.assertTrue(q.empty())
        result = json.loads(client.published[-1][0][1])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("Retained", result["error"])

    def test_queue_full_rejected(self):
        client, q = FakeClient(), queue.Queue(maxsize=1)
        q.put_nowait(("existing", "POP00"))
        monitor.handle_command_message(client, FakeMessage('{"id":"x","setting":"output_source_priority","value":"01"}'), q, True, "result")
        result = json.loads(client.published[-1][0][1])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("queue is full", result["error"])

    def test_ack_then_readback_verifies(self):
        client, q = FakeClient(), queue.Queue()
        q.put_nowait(("x", "POP01"))
        with patch.object(monitor, "serial_command", return_value="ACK"), patch.object(monitor, "get_settings", return_value={"OutputSourcePriority":"solar"}):
            monitor.process_one_setting_command("/dev/null", client, q, "result")
        result = json.loads(client.published[-1][0][1])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["expected"], result["actual"])

    def test_ack_but_mismatch_is_not_reported_as_success(self):
        client, q = FakeClient(), queue.Queue()
        q.put_nowait(("x", "POP01"))
        with patch.object(monitor, "serial_command", return_value="ACK"), patch.object(monitor, "get_settings", return_value={"OutputSourcePriority":"utility"}):
            monitor.process_one_setting_command("/dev/null", client, q, "result")
        result = json.loads(client.published[-1][0][1])
        self.assertEqual(result["status"], "acknowledged_but_readback_mismatch")

    def test_nak_is_rejected_by_inverter(self):
        client, q = FakeClient(), queue.Queue()
        q.put_nowait(("x", "POP01"))
        with patch.object(monitor, "serial_command", return_value="NAK"):
            monitor.process_one_setting_command("/dev/null", client, q, "result")
        result = json.loads(client.published[-1][0][1])
        self.assertEqual(result["status"], "rejected_by_inverter")

if __name__ == "__main__":
    unittest.main(verbosity=2)
