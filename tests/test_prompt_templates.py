import hashlib
import json
import unittest
from unittest.mock import patch

from sfdc_agent.prompt_registry import BASELINE_RELEASE, runtime_release
from sfdc_agent.prompt_run_values import PromptRunValues, capture_prompt_run
from sfdc_agent.prompt_templates import load_baseline_templates, template_placeholders, validate_template
from sfdc_agent.screen_tasks import build_screen_task
from sfdc_agent.story_tasks import build_story_task


def template_cases():
    reference = {"fileKey": "FixtureFile", "nodeId": "29876:15045"}
    context = {"fileKey": reference["fileKey"], "nodeIds": [reference["nodeId"], "8:12"]}
    remote = {
        **reference, "name": "nextBssScreen15045", "version": "1.9",
        "tokenDeclarations": "--nova-color-t-blue-600: #3617ce;",
        "typographyGroups": ["--nova-typography-body", "--nova-typography-title"],
        "componentVariants": "조회 · Filled",
        "layoutDimensions": {"width": 1380.25, "height": 1716},
    }
    node = {"nodeId": reference["nodeId"], "text": '텍스트 <div>{{literal_evidence}}</div>'}
    action = {"kind": "search", "nodeId": "8:13"}
    history = {"nodeId": "8:20", "empty": {"label": "결과 없음"}}
    detailed = {
        **remote, "actions": [action], "coverage": {"split": True, "unresolved": ["8:12"], "nodesRead": ["8:11"]},
        "splitLayout": [{"x": 15, "y": 45}], "sourceSections": [node], "historyTables": [history],
    }
    reviewed = {"id": "analysis", "mappings": [{"nodeId": "8:13", "component": "Button"}]}
    saved = {**remote, "capturedContext": [node], "reviewedAnalysis": reviewed}
    local = {
        **remote, "local": True, "actions": [action], "reviewedAnalysis": reviewed,
        "sourceContext": {"nodes": [{"id": reference["nodeId"]}], "actions": [action], "historyTables": [history]},
        "historyTables": [history],
    }
    rest = {**local, "sourceKind": "figma-rest", "historyTables": [{"nodeId": "8:22"}]}
    plan = {"source": {"id": reference["nodeId"]}, "sourceDigest": "a" * 64}
    plan_context = {**plan, "referenceContext": "검토한 Figma 원본"}
    diagnostic = "테이블 열 수정"
    old_draft = '{"html":"<template></template>","note":"보정"}'
    story_input = json.dumps({"bundleName": "nextBssStory", "specDigest": "b" * 64, "spec": {"plan": {}}})
    return {
        "analyze": ("screen.analyze", context),
        "analyze_screenshot": ("screen.analyze", {**context, "includeScreenshot": True}),
        "metadata": ("screen.metadata", context),
        "capture": ("screen.capture", {**reference, "maxDimension": 4096}),
        "capture_layout": ("screen.capture", {**reference, "maxDimension": 8192, "readLayout": True}),
        "discovery_document": ("screen.discovery", {"fileKey": reference["fileKey"]}),
        "discovery_page": ("screen.discovery", reference),
        "review_remote": ("screen.review-plan", remote),
        "review_detailed": ("screen.review-plan", detailed),
        "review_saved": ("screen.review-plan", saved),
        "review_local": ("screen.review-plan", local),
        "review_rest": ("screen.review-plan", rest),
        "generate_remote": ("screen.generate", remote),
        "generate_without_size": ("screen.generate", {key: value for key, value in remote.items() if key != "layoutDimensions"}),
        "generate_detailed": ("screen.generate", detailed),
        "generate_saved": ("screen.generate", saved),
        "generate_local": ("screen.generate", local),
        "generate_rest": ("screen.generate", rest),
        "legacy_local_analysis": ("screen.analyze.local", local),
        "repair_remote": ("screen.repair", {"local": False, "diagnostic": diagnostic}),
        "repair_local": ("screen.repair", {"local": True, "diagnostic": diagnostic}),
        "repair_with_evidence": ("screen.repair", {
            "local": True, "evidence": rest, "diagnostic": diagnostic, "previousDraft": old_draft,
        }),
        "plan": ("screen.plan", plan),
        "plan_context": ("screen.plan", plan_context),
        "plan_repair": ("screen.plan.repair", {"diagnostic": diagnostic}),
        "plan_repair_with_evidence": ("screen.plan.repair", {
            "evidence": plan_context, "diagnostic": diagnostic, "previousDraft": old_draft,
        }),
        "story_classification": ("story.classification", {"evidence": '{"headings":[],"screens":[],"links":[]}'}),
        "story_analysis": ("story.analysis", {
            "evidence": '{"screens":[{"title":"번호이동"}]}', "categories": '{"workflow":"흐름"}', "maxFacts": 40,
        }),
        "story_generation": ("story.generate", {"input": story_input}),
        "story_retry": ("story.generate", {
            "input": story_input, "diagnostic": '{"code":"TEMPLATE_INVALID","message":"컴파일 오류"}',
        }),
    }


# SHA-256 of reviewed packaged prompt output. Screen cases use the NOVA 1.0 contract.
EXPECTED_BASELINE = {
    "analyze": "9585f5b4581adf2f076dffc6031303da81549c1c07fc9dd8e1d6bfd2b66f49b7",
    "analyze_screenshot": "46bd1750b8ccac0dbb1562bf078cd5d254ba8245900e50d0e5bbcabca5987969",
    "metadata": "27b959e85fd760536b03df59a7560f2e13f21e1c503bccdabc7afa7500d76355",
    "capture": "316a1338c2e9eb2d9f3595b10f4c089138989b58aed3c79a59b692458c9dc77d",
    "capture_layout": "9ab6213d07799dc91fb7f3ce571d9460fc40fc1366b61463db9b4c0bdf37f2d4",
    "discovery_document": "7e8b2ae8c8f77bca33fe691a892c5643292d7a52787a34a04caa3dfc816d952b",
    "discovery_page": "434c9229985b3521d3ca50c95c81302c62e5502d9e1ac94fbe1936f7a8d14e17",
    "review_remote": "a5a448ef7b91037fc202f4045ef4667fe3e5319d3b5c77ef601949c6d435cc48",
    "review_detailed": "e09e7b2cdf71839b2215dd394cc2e283a6388e9b194cd3a3b12bfc904c2f4943",
    "review_saved": "0141ecda01cbd5efded939fdd8ea21044b88dc31ccd95b370a7fa203774b9f5c",
    "review_local": "2075dc71aad6d57d104bf7095a1657e920d0bebdeba90038d40386ada8f5b0cc",
    "review_rest": "11578b71d4d25bfdc4ccf183e94b18f6fce1f40c419220ba647fd7b195997eef",
    "generate_remote": "a515fd3cdc665cf19df3a5f4f053e7f1a733e72a06870a328aa36ea4ed9f3cb9",
    "generate_without_size": "f5edacf2f35ba4ce3f8edd23cad20f052f0be4ca175e53ccba66490e163056ed",
    "generate_detailed": "dae429c2ea77dd35c56f97e0fae9e6fcd8fa501c376bc00bd6fb3338c9b07eed",
    "generate_saved": "e504bbb01c0fc83aa4b7b0224a025c35e0aca44063e2f764aeec0d4cfc739f54",
    "generate_local": "1b669d5eab3586d5627a2009f67408de05b39b1a5263dc731a201614055304bc",
    "generate_rest": "f7f525d84970c704c03d431e76bd7e44b36040532c186169ca44ca9bae030f77",
    "legacy_local_analysis": "9666f8f5fb6f3ed2f4913120f9ecb4f8e554c6f719f21d2890fb77b8c350a891",
    "repair_remote": "d36ece139d1baac041eb5761b666399ee55b98579cf5b1c567b1d61e4d89e6a7",
    "repair_local": "4f4f976eb655b208da8bf6b07f8f6818c04abbbe627f4fb2f79b32d53e02eead",
    "repair_with_evidence": "6f8206d9267e8da62891780b376b0f9ef6d318de5e8f765ca0fbb580566359b9",
    "plan": "7852ea0f514c8e6ae9af37a9b2b39d536493cf1ec37f42cba4732b61a9f03080",
    "plan_context": "09a5a59763a965dd8e4ae782366c7f4b009be7b5a4f82adbbb46bf3493c06600",
    "plan_repair": "9543a8a7c620efc85224c67082a36f27a3c430af2e4c640b13cc8c44fc7dc4aa",
    "plan_repair_with_evidence": "3ad3d02c2d5c2f77519f08e12bd379338c83b3128d65d4a89cc069576adac828",
    "story_classification": "f11fb038cc987616669fa8fad9dcd9fcddf65bd1f4a3382e8eaf0bf25d72ac8f",
    "story_analysis": "c3ddcc9f19d9e838da2c8a2bb7bf37e4131263249d862dec416fdfbbfb2dc23d",
    "story_generation": "332703cf791cbf954ffe14664aa486bcd7e261c19787062eded3ded9d17c549c",
    "story_retry": "bff10e9d4d90d58e5763df58f57fd0934a306ac38f64477f20e691d2c94e4c6e",
}


class PromptTemplateTests(unittest.TestCase):
    def test_packaged_templates_match_original_builder_bytes(self):
        cases = template_cases()
        self.assertEqual(EXPECTED_BASELINE.keys(), cases.keys())
        self.assertEqual(set(load_baseline_templates()), {operation for operation, _ in cases.values()})
        for case, (operation, fields) in cases.items():
            with self.subTest(case=case):
                builder = build_story_task if operation.startswith("story.") else build_screen_task
                with capture_prompt_run(operation) as capture:
                    actual = builder(operation, fields)
                self.assertEqual(hashlib.sha256(actual.encode("utf-8")).hexdigest(), EXPECTED_BASELINE[case])
                self.assertLessEqual(capture.values.keys(), set(template_placeholders(operation)))
                for items in capture.values.values():
                    for entry in items:
                        if "value" in entry:
                            self.assertIn(entry["value"], actual)
                if case == "plan_repair_with_evidence":
                    self.assertEqual(capture.values["diagnostic"], [
                        {"section": "with_evidence", "value": json.dumps(fields["diagnostic"], ensure_ascii=False)},
                    ])
                if case == "repair_with_evidence":
                    self.assertEqual(capture.values["prompt"], [{"section": "with_evidence", "redacted": True}])

    def test_runtime_values_expire_and_older_completions_do_not_overwrite_latest(self):
        release = runtime_release(BASELINE_RELEASE)
        owner = 'a' * 64
        operation = "story.classification"
        store = PromptRunValues()
        now = [2.0]
        with patch("sfdc_agent.prompt_run_values.monotonic", side_effect=lambda: now[0]), patch(
            "sfdc_agent.prompt_run_values._MAX_VALUE_BYTES", 50,
        ):
            new_evidence = json.dumps({"title": "a" * 70})
            with capture_prompt_run(operation) as latest:
                build_story_task(operation, {"evidence": new_evidence})
            store.save(owner, latest, started=2, release=release, mode="headless")
            run = store.get(owner, operation, "evidence")
            self.assertEqual(run["values"], [{"section": "main", "omitted": True}])
            with capture_prompt_run(operation) as older:
                build_story_task(operation, {"evidence": "{}"})
            store.save(owner, older, started=1, release=release, mode="headless")
            self.assertEqual(store.get(owner, operation, "evidence"), run)
            now[0] += 60 * 60
            self.assertIsNone(store.get(owner, operation, "evidence"))

    def test_edits_keep_evidence_and_validate_template_contract(self):
        operation, fields = template_cases()["story_analysis"]
        baseline = load_baseline_templates()
        changed = baseline.copy()
        changed[operation] = "Edited guidance.\n" + changed[operation]
        prompt = build_story_task(operation, fields, templates=changed)
        self.assertEqual(prompt, "Edited guidance.\n" + build_story_task(operation, fields))
        for invalid in (
            changed[operation].replace("{{evidence}}", ""),
            changed[operation].replace("{{evidence}}", "{{dto.evidence}}"),
            changed[operation] + "{{evidence}}",
            changed[operation] + "\n@@section extra\nUnsafe extra section.",
        ):
            with self.subTest(invalid=invalid[-50:]), self.assertRaises(ValueError):
                validate_template(operation, invalid)
        operation, fields = template_cases()["generate_saved"]
        self.assertIn("{{literal_evidence}}", build_screen_task(operation, fields))

    def test_repair_uses_related_templates_from_supplied_release(self):
        operation, fields = template_cases()["repair_with_evidence"]
        changed = load_baseline_templates()
        changed["screen.generate"] = changed["screen.generate"].replace(
            "Generate the first", "Generate the revised", 1,
        )
        self.assertIn("Generate the revised", build_screen_task(operation, fields, templates=changed))
        self.assertNotIn("Generate the revised", build_screen_task(operation, fields))


if __name__ == "__main__":
    unittest.main()
