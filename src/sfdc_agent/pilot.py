"""Run the same source and release through NOVA and the model-assisted path."""

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from time import monotonic

from pydantic import ValidationError

from .comparison import story_comparison, visual_pair
from .contracts import PilotRequest, ScreenPlan, StoryPlan
from .engine import ConversionEngine
from .providers import ModelProvider, ProviderError
from .storage import RunStore, read_json, write_json
from .story_tasks import build_story_task


def checkpoint(path: Path, execute: Callable[[], dict]) -> dict:
    if path.is_file():
        return read_json(path)
    result = execute()
    write_json(path, result)
    return result


def propose(provider: ModelProvider, task: str, evidence: dict | str, contract) -> dict:
    started = monotonic()
    try:
        response = provider.complete(task, evidence, contract.model_json_schema())
        return {"status": "received", "plan": contract.model_validate(response).model_dump(exclude_none=True),
                "elapsedSeconds": round(monotonic() - started, 3)}
    except ProviderError as error:
        return {"status": "failed", "code": error.code}
    except ValidationError:
        return {"status": "failed", "code": "MODEL_PLAN_SCHEMA"}


def model_checkpoint(path: Path, execute: Callable[[], dict]) -> dict:
    if path.is_file() and (result := read_json(path))["status"] == "received":
        return result
    result = execute()
    write_json(path, result)
    return result


def comparison_outcome(result: dict) -> dict:
    findings = []
    if screen := result.get("screen"):
        delta = screen["paired"]["deltaPercentagePoints"]
        if delta is None:
            findings.append("screen-visual-unmeasured")
        elif delta > 0:
            findings.append("screen-visual-regression")
        findings.extend(screen["paired"]["behaviorRegressions"])
        findings.extend(item["code"] for item in screen["agent"].get("applyBlockers") or [])
    if story := result.get("story"):
        for workflow, report in story["paired"].items():
            initial = report["initial"]["deltaPercentagePoints"]
            if initial is None or initial > 0:
                findings.append(f"{workflow}:initial-visual")
            for case, outcome in report["cases"].items():
                if outcome["baselineStatus"] == "pass" and outcome["agentStatus"] != "pass":
                    findings.append(f"{workflow}:{case}:behavior-regression")
                delta = outcome["deltaPercentagePoints"]
                if delta is None:
                    findings.append(f"{workflow}:{case}:visual-unmeasured")
                elif delta > 0:
                    findings.append(f"{workflow}:{case}:visual-regression")
    return {"status": "review-required" if findings else "no-measured-regression", "findings": findings}


def run_pilot(store: RunStore, run_id: str, request: PilotRequest,
              engine: ConversionEngine, provider: ModelProvider) -> dict:
    directory = store.directory(run_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    pin = checkpoint(directory / "input.json", lambda: engine.invoke("pin"))
    context = {key: pin[key] for key in ("sourceSha256", "releaseId", "fixtureDigest")}
    if request.reference_manifest:
        context["referenceManifest"] = request.reference_manifest
    report = {
        "runId": run_id, "provider": provider.name, "model": provider.model,
        "input": pin, "results": {}, "modelPlansApplied": False,
    }
    targets = ("screen", "story") if request.target == "all" else (request.target,)
    for target in targets:
        target_dir = directory / target
        target_dir.mkdir(exist_ok=True, mode=0o700)
        store.update(run_id, f"{target}.evidence")
        evidence = checkpoint(target_dir / "evidence.json", lambda: engine.invoke(target, phase="evidence", **context))

        def render(phase: str, **extra) -> dict:
            output = target_dir / phase
            if extra:
                fingerprint = sha256(json.dumps(extra, sort_keys=True).encode()).hexdigest()
                output /= fingerprint[:16]
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            return checkpoint(output / "result.json", lambda: {**engine.invoke(
                target, phase=phase, outputDir=str(output), **context, **extra,
            ), "artifactDirectory": str(output.relative_to(directory))})

        store.update(run_id, f"{target}.baseline")
        baseline = render("baseline")
        store.update(run_id, f"{target}.model")
        if target == "screen":
            proposals = {"screen": model_checkpoint(target_dir / "model-screen.json", lambda: propose(
                provider, "Review the Screen evidence in Korean. Cite exact evidenceId values. "
                "Prioritize verifying read-only fields, current select labels, local reset/search, "
                "and unresolved business behavior. Return observations without inventing business rules.",
                evidence, ScreenPlan,
            ))}
            plan = proposals["screen"].get("plan")
        else:
            proposals = {}
            for workflow in evidence["workflows"]:
                workflow_id = workflow["id"]
                if not workflow_id or not all(char.isalnum() or char in "-_" for char in workflow_id):
                    raise ValueError("The Story engine returned an invalid workflow ID.")
                proposals[workflow_id] = model_checkpoint(target_dir / f"model-{workflow_id}.json", lambda: propose(
                    provider, "Produce a NOVA Story specification using the instructions in this "
                    "NOVA-generated task. Keep existing validated lookup responses and required state behavior.",
                    workflow["prompt"] if "prompt" in workflow else build_story_task("story.analysis", workflow["task"]),
                    StoryPlan,
                ))
            plan = {key: item["plan"] for key, item in proposals.items() if item["status"] == "received"}

        store.update(run_id, f"{target}.agent")
        agent = render(
            "agent", modelPlan=plan, referenceDirectory=str(target_dir / "baseline"),
        )
        for result in (baseline, agent):
            release = result.get("releaseId") or result.get("standard", {}).get("releaseId")
            if release != pin["releaseId"]:
                raise ValueError(f"The {target} engine report does not match the pinned release.")
        if target == "story":
            paired = story_comparison(baseline, agent)
        else:
            paired = visual_pair(baseline["visual"], agent["visual"], "referenceSha256")
            common = {item["id"]: item["status"] for item in agent["behavior"]["checks"]}
            paired["behaviorRegressions"] = [
                item["id"] for item in baseline["behavior"]["checks"]
                if item["status"] == "pass" and common.get(item["id"]) != "pass"
            ]
        report["results"][target] = {
            "baseline": baseline, "agent": agent,
            "paired": paired,
            "model": {key: {field: value for field, value in item.items() if field != "plan"}
                      for key, item in proposals.items()},
        }
        report["modelPlansApplied"] = all(
            item["status"] == "received"
            for result_set in report["results"].values() for item in result_set["model"].values()
        ) and all(
            workflow["proposal"]["status"] == "accepted"
            for workflow in report["results"].get("story", {}).get("agent", {}).get("workflows", [])
        )
        if screen := report["results"].get("screen"):
            report["modelPlansApplied"] &= bool(screen["agent"]["review"]["accepted"])
        report["assessment"] = comparison_outcome(report["results"])
        write_json(directory / "comparison.json", report)

    return report
