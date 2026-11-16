import hashlib
import json
import unittest

from sfdc_agent.prompt_templates import load_baseline_templates, preview_template, validate_template
from sfdc_agent.screen_tasks import build_screen_task
from sfdc_agent.story_tasks import build_story_task


def template_cases():
    reference = {"fileKey": "FixtureFile", "nodeId": "29876:15045"}
    context = {"fileKey": reference["fileKey"], "nodeIds": [reference["nodeId"], "8:12"]}
    remote = {
        **reference, "name": "nextBssScreen15045", "version": "1.9",
        "tokenDeclarations": "--nb-color-t-blue-600: #3617ce;",
        "typographyGroups": ["--nb-typography-body", "--nb-typography-title"],
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


# SHA-256 of the original builders' exact UTF-8 output at ab0166c.
EXPECTED_BASELINE = {
    "analyze": "9585f5b4581adf2f076dffc6031303da81549c1c07fc9dd8e1d6bfd2b66f49b7",
    "analyze_screenshot": "46bd1750b8ccac0dbb1562bf078cd5d254ba8245900e50d0e5bbcabca5987969",
    "metadata": "27b959e85fd760536b03df59a7560f2e13f21e1c503bccdabc7afa7500d76355",
    "capture": "316a1338c2e9eb2d9f3595b10f4c089138989b58aed3c79a59b692458c9dc77d",
    "capture_layout": "9ab6213d07799dc91fb7f3ce571d9460fc40fc1366b61463db9b4c0bdf37f2d4",
    "discovery_document": "7e8b2ae8c8f77bca33fe691a892c5643292d7a52787a34a04caa3dfc816d952b",
    "discovery_page": "434c9229985b3521d3ca50c95c81302c62e5502d9e1ac94fbe1936f7a8d14e17",
    "review_remote": "53f3c9993f1b420c48d104ebeb605ead7df6620d89963cc184151161393300f7",
    "review_detailed": "4a14fdd98684564db46a4a0cd375519caa2ba719f0f0929507425b275f2865d8",
    "review_saved": "d22cdeb0efa91721c643913fd82cefb52587336fa439d0154f3d15ed508b5201",
    "review_local": "f424f70d711d27c8bb8f636c4689a7e7800446936d90c54b823991d626190963",
    "review_rest": "bc1128a040b8a5ae5e08949a81fcc830c96c4a4c852c9791d5d6712d8b5df736",
    "generate_remote": "764d6ee82701d45c29249728e875f3d105d8e562e88f1498284add0e0650510e",
    "generate_without_size": "d9503916ed3d751ae1a6e980960c556e185995ff60b533828975ed8125bc7133",
    "generate_detailed": "8904f8decc9a622fcf6a0ab16265dc289986054b9085b597c48b9d2d1fc58840",
    "generate_saved": "b0f30fff150752750c892fbfcf46b1b4fdcacf20e5e53c63d121f3efb7e7b79e",
    "generate_local": "c41e74149c48f5c84611557dd2befa0ac173f0046c835c4d8ab0559c85fd65c9",
    "generate_rest": "a8a1b43995e064cc6f172de47809648ed5e89edf8c6ad72561c19251f538f51f",
    "legacy_local_analysis": "9666f8f5fb6f3ed2f4913120f9ecb4f8e554c6f719f21d2890fb77b8c350a891",
    "repair_remote": "a2f7ee00f6ee0ff02bdbeedf74744f0a611bb55f7f0c419015c466ce83ea4799",
    "repair_local": "d2dba7b49140059651a1240dac352b1edcb5e86152a4b6deba1d735d7eea864b",
    "repair_with_evidence": "21ff6b9672391270d44e6293999b871701fd6c530b70eef6851afdc17602037b",
    "plan": "7852ea0f514c8e6ae9af37a9b2b39d536493cf1ec37f42cba4732b61a9f03080",
    "plan_context": "09a5a59763a965dd8e4ae782366c7f4b009be7b5a4f82adbbb46bf3493c06600",
    "plan_repair": "9543a8a7c620efc85224c67082a36f27a3c430af2e4c640b13cc8c44fc7dc4aa",
    "plan_repair_with_evidence": "3ad3d02c2d5c2f77519f08e12bd379338c83b3128d65d4a89cc069576adac828",
    "story_classification": "f11fb038cc987616669fa8fad9dcd9fcddf65bd1f4a3382e8eaf0bf25d72ac8f",
    "story_analysis": "c3ddcc9f19d9e838da2c8a2bb7bf37e4131263249d862dec416fdfbbfb2dc23d",
    "story_generation": "708bdc8db4d0d4132a731b1b992f09de20a7e4ad111199a5f9ddab63167ff49b",
    "story_retry": "c4c46db8fcbca1a5341cbd5bb5a917029b4f32f4256714e7c34933837c64da6f",
}


class PromptTemplateTests(unittest.TestCase):
    def test_packaged_templates_match_original_builder_bytes(self):
        cases = template_cases()
        self.assertEqual(EXPECTED_BASELINE.keys(), cases.keys())
        self.assertEqual(set(load_baseline_templates()), {operation for operation, _ in cases.values()})
        for case, (operation, fields) in cases.items():
            with self.subTest(case=case):
                builder = build_story_task if operation.startswith("story.") else build_screen_task
                actual = hashlib.sha256(builder(operation, fields).encode("utf-8")).hexdigest()
                self.assertEqual(actual, EXPECTED_BASELINE[case])

    def test_preview_and_edits_keep_evidence_and_validate_template_contract(self):
        operation, fields = template_cases()["story_analysis"]
        baseline = load_baseline_templates()
        changed = baseline.copy()
        changed[operation] = "Edited guidance.\n" + changed[operation]
        prompt = build_story_task(operation, fields, templates=changed)
        self.assertEqual(prompt, "Edited guidance.\n" + build_story_task(operation, fields))
        preview = preview_template(operation, changed[operation])
        self.assertIn("〈evidence〉", preview)
        self.assertNotIn(fields["evidence"], preview)
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
