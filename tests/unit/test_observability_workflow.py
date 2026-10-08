import unittest
from pathlib import Path


class ObservabilityWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(
            ".github/workflows/agent-observability.yml"
        ).read_text(encoding="utf-8")

    def test_manual_and_scheduled_runs_use_cloud_judge(self):
        self.assertIn("runs-on: ubuntu-latest", self.workflow)
        self.assertIn("DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}", self.workflow)
        self.assertIn("SCENARIO_JUDGE_PROVIDER: deepseek", self.workflow)
        self.assertIn("SCENARIO_JUDGE_MODEL: deepseek-v4-pro", self.workflow)
        self.assertNotIn("LOCAL_LLM_BASE_URL", self.workflow)

    def test_job_has_bounded_timeout(self):
        self.assertIn("timeout-minutes: 90", self.workflow)

    def test_cloud_preflight_replaces_local_model_lease(self):
        self.assertIn("python3 .github/scripts/check_cloud_judge.py", self.workflow)
        self.assertNotIn("/release/background", self.workflow)

    def test_active_workflows_have_no_retired_model_routes(self):
        for path in Path(".github/workflows").glob("*.yml"):
            source = path.read_text(encoding="utf-8").lower()
            for retired in ("bosgame", "gpt-oss", "159.195.207.48"):
                self.assertNotIn(retired, source, path.name)


if __name__ == "__main__":
    unittest.main()
