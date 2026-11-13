"""Check that reports actually share a source and measure paired outcomes."""


def ratio(sample: dict) -> float | None:
    pixel = sample.get("pixel") or sample
    if not pixel.get("totalPixels") or "changedPixels" not in pixel:
        return None
    return pixel["changedPixels"] / pixel["totalPixels"]


def visual_pair(original: dict, candidate: dict, reference_key: str) -> dict:
    left, right = original.get(reference_key), candidate.get(reference_key)
    if not left or left != right:
        raise ValueError("Paired render reports do not share the same reference image digest.")
    if original.get("comparisonMode") != candidate.get("comparisonMode"):
        raise ValueError("Paired reports disagree on reference provenance.")
    a, b = ratio(original), ratio(candidate)
    return {
        "referenceSha256": left,
        "mode": original.get("comparisonMode"),
        "baselineRatio": a,
        "agentRatio": b,
        "deltaPercentagePoints": None if a is None or b is None else round(100 * (b - a), 6),
    }


def story_comparison(baseline: dict, agent: dict) -> dict:
    if baseline["fixtureDigest"] != agent["fixtureDigest"]:
        raise ValueError("Paired Story reports used different fixtures.")
    candidates = {item["id"]: item for item in agent["workflows"]}
    if set(candidates) != {item["id"] for item in baseline["workflows"]}:
        raise ValueError("Paired Story reports covered different workflows.")
    result = {}
    for previous in baseline["workflows"]:
        current = candidates[previous["id"]]
        cases = {case["id"]: case for case in current["cases"]}
        if set(cases) != {case["id"] for case in previous["cases"]}:
            raise ValueError("Paired Story reports covered different test cases.")

        def compare(left: dict, right: dict) -> dict:
            if left["reference"]["mode"] != right["reference"]["mode"]:
                raise ValueError("Paired Story reports disagree on reference provenance.")
            return visual_pair(
                {**left, "sha": left["reference"]["sha256"], "comparisonMode": left["reference"]["mode"]},
                {**right, "sha": right["reference"]["sha256"], "comparisonMode": right["reference"]["mode"]},
                "sha",
            )

        result[previous["id"]] = {
            "initial": compare(previous["initial"], current["initial"]),
            "cases": {case["id"]: {
                **compare(case, cases[case["id"]]),
                "baselineStatus": case["status"],
                "agentStatus": cases[case["id"]]["status"],
            } for case in previous["cases"]},
        }
    return result
