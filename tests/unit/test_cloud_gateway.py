import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cloud_gateway", ROOT / "eval/cloud_gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class CloudGatewayTests(unittest.TestCase):
    def setUp(self):
        gateway.ROUTE_DISABLED_UNTIL.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger = mock.patch.object(gateway, "LEDGER", Path(self.tempdir.name) / "ledger.jsonl")
        self.ledger.start()

    def tearDown(self):
        self.ledger.stop()
        self.tempdir.cleanup()

    def test_text_routes_are_ordered_free_then_paid(self):
        keys = {route[1]: "test" for route in gateway.ROUTES}
        with mock.patch.dict(gateway.os.environ, keys, clear=True):
            names = [route[0] for route in gateway.configured_routes()]
        self.assertEqual(names, ["groq", "cerebras", "sambanova", "nvidia", "opencode-go", "deepseek"])

    def test_image_uses_vision_capable_routes(self):
        payload = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]}]}
        self.assertTrue(gateway.has_image(payload))
        with mock.patch.dict(gateway.os.environ, {"NVIDIA_API_KEY": "test", "DEEPSEEK_API_KEY": "test"}, clear=True):
            self.assertEqual([route[0] for route in gateway.configured_routes(True)], ["nvidia-vision", "deepseek"])

    def test_rate_limit_rotates_and_records_actual_provider(self):
        routes = [
            ("free", "test", "https://free.invalid/v1", "free-model"),
            ("deepseek", "test", "https://paid.invalid", "deepseek-flash"),
        ]
        failure = HTTPError("https://free.invalid", 429, "rate limited", {}, None)
        answer = io.BytesIO(json.dumps({"choices": [{"message": {"content": "OK"}}], "usage": {
            "prompt_tokens": 3, "completion_tokens": 1,
        }}).encode())
        with mock.patch.object(gateway.urllib.request, "urlopen", side_effect=[failure, answer]):
            result = gateway.chat({"messages": [{"role": "user", "content": "OK"}]}, routes)
        self.assertEqual(result["model"], "deepseek/deepseek-flash")
        self.assertGreater(gateway.ROUTE_DISABLED_UNTIL["free"], gateway.time.time())
        entry = json.loads(gateway.LEDGER.read_text())
        self.assertEqual(entry["provider"], "deepseek")
        self.assertEqual(entry["status"], "ok")


if __name__ == "__main__":
    unittest.main()
