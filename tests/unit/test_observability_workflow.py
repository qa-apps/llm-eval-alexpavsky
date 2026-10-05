import unittest
from pathlib import Path


class ObservabilityWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(
            ".github/workflows/agent-observability.yml"
        ).read_text(encoding="utf-8")

    def test_manual_and_scheduled_runs_use_cloud_gateway(self):
        self.assertIn("runs-on: ubuntu-latest", self.workflow)
        self.assertIn("LOCAL_LLM_BASE_URL: http://127.0.0.1:18765/v1", self.workflow)
        self.assertIn("LOCAL_LLM_MODEL: cloud-eval", self.workflow)
        self.assertNotIn("runs-on: [self-hosted, Linux, X64, bosgame]", self.workflow)

    def test_secrets_are_detected_even_when_gateway_fails(self):
        keys = self.workflow.split("- name: Detect backend keys", 1)[1]
        self.assertIn("if: always()", keys.split("- name: Voice agent", 1)[0])

    def test_gateway_requires_real_completion(self):
        self.assertIn("Start cloud judge and verify a real completion", self.workflow)
        self.assertIn("/v1/chat/completions", self.workflow)


if __name__ == "__main__":
    unittest.main()
