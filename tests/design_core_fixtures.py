from hashlib import sha256
import json
from pathlib import Path


def write_core_release(root: Path, runtime: Path):
    release_id = "nextbss-1.9-" + "a" * 64
    directory = root / release_id
    css_path = "nextbss/design-system/lwc/novaTokens/novaTokens.css"
    css = b":host { --fixture-color: #123456; }\n"
    artifact_tree = sha256(("novaTokens/novaTokens.css\0" + sha256(css).hexdigest() + "\n").encode()).hexdigest()
    input_files = [{"path": "packages/design-system-core/salesforce/main/default/lwc/novaTokens/novaTokens.css",
                    "bytes": len(css), "sha256": sha256(css).hexdigest()}]
    input_tree = sha256("".join(f"{item['path']}\0{item['sha256']}\n" for item in input_files).encode()).hexdigest()
    core = {"schemaVersion": 1, "contract": "sfdc-design-system-core-v1", "coreVersion": "1.9.0",
        "sourceRepository": "https://github.com/choijoonbin/sfdc_ui.git", "sourcePath": "packages/design-system-core",
        "commit": "c" * 40, "inputTreeSha256": input_tree, "sourceTreeSha256": artifact_tree,
        "artifactTreeSha256": artifact_tree, "compatibility": {"engineContract": "nextbss-lwc-v1"}}
    contents = {css_path: css, "nextbss/design-system/core-source.json": (json.dumps(core) + "\n").encode(),
        "nextbss/design-system/core-inputs.json": (json.dumps({"schemaVersion": 1, "files": input_files}) + "\n").encode()}
    entries = []
    for name, content in sorted(contents.items()):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entries.append({"path": name, "bytes": len(content), "sha256": sha256(content).hexdigest()})
    manifest = {"schemaVersion": 1, "releaseId": release_id,
        "source": {"commit": core["commit"], "treeSha256": artifact_tree}, "files": entries}
    raw = (json.dumps(manifest) + "\n").encode()
    (directory / "release.json").write_bytes(raw)
    capabilities = runtime / "nextbss/engine/managed-runtime-capabilities.json"
    capabilities.parent.mkdir(parents=True, exist_ok=True)
    capabilities.write_text(json.dumps({"designSystemCoreContracts": ["nextbss-lwc-v1"]}))
    return {"release_id": release_id, "manifest_sha": sha256(raw).hexdigest(), "release_root": root,
        "runtime_root": runtime, "core": core, "directory": directory}
