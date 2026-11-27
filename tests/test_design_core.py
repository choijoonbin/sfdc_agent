import json
from hashlib import sha256
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from design_core_fixtures import write_core_release
from sfdc_agent.design_core import DesignCore, preserve_core_summary, verify_selected_core
from sfdc_agent.managed_contracts import ExecutionResult


class DesignCoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fixture = write_core_release(self.root / "releases", self.root / "runtime")
        self.enterContext(patch.dict(os.environ, {"SFDC_MANAGED_ENGINE_ROOT": str(self.root / "runtime")}))

    def verify(self, **changes):
        fields = {name: self.fixture[name] for name in ("release_id", "manifest_sha", "release_root", "runtime_root")}
        return verify_selected_core(**{**fields, "expected": DesignCore.model_validate(self.fixture["core"]), **changes})

    def test_exact_selected_core_is_checked_even_without_an_explicit_wire_pin(self):
        self.assertEqual(self.verify(), self.fixture["core"])
        self.assertEqual(self.verify(expected=None), self.fixture["core"])

    def test_tampered_pin_manifest_and_actual_lwc_are_rejected(self):
        with self.assertRaises(HTTPException) as wrong_manifest:
            self.verify(manifest_sha="f" * 64)
        self.assertEqual(wrong_manifest.exception.detail, "DESIGN_CORE_MANIFEST_MISMATCH")
        pin = DesignCore.model_validate({**self.fixture["core"], "commit": "f" * 40})
        with self.assertRaises(HTTPException) as wrong_pin:
            self.verify(expected=pin)
        self.assertEqual(wrong_pin.exception.detail, "DESIGN_CORE_PIN_MISMATCH")
        path = self.fixture["directory"] / "nextbss/design-system/lwc/novaTokens/novaTokens.css"
        path.write_text("tampered core")
        with self.assertRaises(HTTPException) as wrong_file:
            self.verify()
        self.assertEqual(wrong_file.exception.detail, "DESIGN_CORE_ARTIFACT_INVALID")

    def test_new_core_requires_manifest_pin_and_an_installed_compatible_engine(self):
        with self.assertRaises(HTTPException) as missing_pin:
            self.verify(manifest_sha=None)
        self.assertEqual(missing_pin.exception.detail, "DESIGN_CORE_MANIFEST_PIN_REQUIRED")
        with patch.dict(os.environ, {"SFDC_MANAGED_ENGINE_ROOT": ""}):
            with self.assertRaises(HTTPException) as missing_runtime:
                self.verify()
        self.assertEqual(missing_runtime.exception.detail, "DESIGN_CORE_RUNTIME_UNAVAILABLE")
        path = self.fixture["runtime_root"] / "nextbss/engine/managed-runtime-capabilities.json"
        path.write_text(json.dumps({"designSystemCoreContracts": ["future-engine-v2"]}))
        with self.assertRaises(HTTPException) as incompatible:
            self.verify()
        self.assertEqual(incompatible.exception.detail, "RELEASE_RUNTIME_INCOMPATIBLE")

    def test_raw_authored_tree_is_bound_to_the_committed_input_inventory(self):
        core = {**self.fixture["core"], "sourceTreeSha256": "f" * 64}
        marker_path = "nextbss/design-system/core-source.json"
        raw = json.dumps(core).encode()
        (self.fixture["directory"] / marker_path).write_bytes(raw)
        manifest_path = self.fixture["directory"] / "release.json"
        manifest = json.loads(manifest_path.read_bytes())
        for entry in manifest["files"]:
            if entry["path"] == marker_path:
                entry.update(bytes=len(raw), sha256=sha256(raw).hexdigest())
        manifest_raw = json.dumps(manifest).encode()
        manifest_path.write_bytes(manifest_raw)
        with self.assertRaises(HTTPException) as mismatch:
            self.verify(expected=DesignCore.model_validate(core), manifest_sha=sha256(manifest_raw).hexdigest())
        self.assertEqual(mismatch.exception.detail, "DESIGN_CORE_ARTIFACT_INVALID")

    def test_nested_runtime_cannot_be_added_outside_the_committed_core_inputs(self):
        path = "nextbss/server/conversion-quality/unreviewed-runtime.mjs"
        raw = b'export default "unreviewed";\n'
        target = self.fixture["directory"] / path
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        manifest_path = self.fixture["directory"] / "release.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"].append({"path": path, "bytes": len(raw), "sha256": sha256(raw).hexdigest()})
        manifest_raw = json.dumps(manifest).encode()
        manifest_path.write_bytes(manifest_raw)
        with self.assertRaises(HTTPException) as rejected:
            self.verify(manifest_sha=sha256(manifest_raw).hexdigest())
        self.assertEqual(rejected.exception.detail, "DESIGN_CORE_ARTIFACT_INVALID")

    def test_marker_and_core_file_symlinks_do_not_cross_the_artifact_boundary(self):
        path = self.fixture["directory"] / "nextbss/design-system/lwc/novaTokens/novaTokens.css"
        value = path.read_bytes()
        path.unlink()
        outside = self.root / "outside.css"
        outside.write_bytes(value)
        path.symlink_to(outside)
        with self.assertRaises(HTTPException):
            self.verify()

    def test_core_contract_rejects_paths_credentials_unknown_fields_and_future_contracts(self):
        for fields in ({"sourcePath": "../outside"}, {"sourcePath": "packages/../outside"},
                       {"schemaVersion": True}, {"schemaVersion": 1.0},
                       {"sourceRepository": "https://user:secret@example.test/repo.git"},
                       {"compatibility": {"engineContract": "future-engine-v2"}}, {"unknown": "field"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                DesignCore.model_validate({**self.fixture["core"], **fields})

    def test_result_keeps_the_trusted_core_and_rejects_worker_substitution(self):
        result = ExecutionResult(status="COMPLETED", title="Saved result", artifacts=[], summary={"bundleName": "screen"})
        self.assertEqual(preserve_core_summary(result, self.fixture["core"]).summary["designCore"], self.fixture["core"])
        with self.assertRaises(HTTPException) as mismatch:
            preserve_core_summary(result.model_copy(update={"summary": {"designCore": {"commit": "other"}}}), self.fixture["core"])
        self.assertEqual(mismatch.exception.detail, "DESIGN_CORE_RESULT_MISMATCH")
        with self.assertRaises(HTTPException):
            preserve_core_summary(result.model_copy(update={"summary": {"designCore": self.fixture["core"]}}), None)

    def test_legacy_selected_release_cannot_claim_core_provenance(self):
        path = self.fixture["directory"] / "release.json"
        legacy = {"releaseId": self.fixture["release_id"], "files": []}
        path.write_text(json.dumps(legacy))
        (self.fixture["directory"] / "nextbss/design-system/core-source.json").unlink()
        self.assertIsNone(self.verify(expected=None, manifest_sha=None))
        with self.assertRaises(HTTPException):
            self.verify(manifest_sha=None)
