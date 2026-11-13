import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sfdc_agent.comparison import visual_pair
from sfdc_agent.contracts import PilotRequest, ScreenPlan
from sfdc_agent.pilot import model_checkpoint, propose
from sfdc_agent.providers import OpenAIProvider
from sfdc_agent.storage import RunStore


class PilotTests(unittest.TestCase):
    def test_only_one_worker_claims_a_run(self):
        with tempfile.TemporaryDirectory() as root:
            store = RunStore(Path(root))
            run_id = store.enqueue(PilotRequest())
            with ThreadPoolExecutor(max_workers=4) as executor:
                claims = list(executor.map(lambda _: store.claim(run_id), range(4)))
            self.assertEqual(sum(claim is not None for claim in claims), 1)
            self.assertEqual(store.get(run_id)["status"], "running")

    def test_transient_model_failure_can_be_retried_but_valid_plan_is_reused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "proposal.json"
            calls = iter([{"status": "failed"}, {"status": "received", "plan": {"findings": []}}])
            self.assertEqual(model_checkpoint(path, lambda: next(calls))["status"], "failed")
            accepted = model_checkpoint(path, lambda: next(calls))
            self.assertEqual(model_checkpoint(path, lambda: next(calls)), accepted)

    def test_proposal_with_extra_fields_is_rejected(self):
        class Stub:
            def complete(self, *_args):
                return {"findings": [], "javascript": "unrecognized"}

        self.assertEqual(propose(Stub(), "review", {}, ScreenPlan)["code"], "MODEL_PLAN_SCHEMA")

    def test_pixel_comparison_refuses_different_references(self):
        with self.assertRaisesRegex(ValueError, "reference image digest"):
            visual_pair({"sha": "one"}, {"sha": "two"}, "sha")

    def test_openai_adapter_uses_the_same_plan_contract(self):
        response = {
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"findings":[{"evidenceId":"field","explanation":"check"}]}'},
            ]}],
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"}), \
             patch("sfdc_agent.providers.build_opener") as opener:
            transport = opener.return_value.open
            transport.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            result = propose(OpenAIProvider("test-model", 10), "review", {"entries": []}, ScreenPlan)
            body = json.loads(transport.call_args.args[0].data)
        self.assertEqual(result["plan"], {"findings": [{"evidenceId": "field", "explanation": "check"}]})
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertFalse(body["store"])


if __name__ == "__main__":
    unittest.main()
