import copy
import struct
from types import SimpleNamespace
import unittest

from sfdc_agent.image_input import inspect_png, validate_image_input
from image_input_fixtures import chunk, image_payload, png


class ImageInputTests(unittest.TestCase):
    def validate(self, value, **options):
        return validate_image_input(value["png"], value["imageBinding"], operation=value["operation"],
                                    fields=value["fields"], **options)

    def test_valid_source_is_bound_to_saved_analysis_and_live_attempt(self):
        value = image_payload()
        context = SimpleNamespace(purpose="screen", file_key="FixtureFile", node_id="12:34")
        result = self.validate(value, context=context)
        self.assertEqual(result.content, png())
        self.assertEqual(result.receipt(), value["imageBinding"])

    def test_mcp_snapshot_is_not_promoted_to_a_figma_revision_or_verified_scale(self):
        value = image_payload(source_version="mcp-snapshot:fixture-snapshot", revision=False, scale_verified=False)
        source = value["fields"]["reviewedAnalysis"]["source"]
        source.pop("version")
        source["snapshotId"] = "fixture-snapshot"
        result = self.validate(value)
        self.assertFalse(result.binding.revisionVerified)
        self.assertFalse(result.binding.scaleVerified)
        changed = copy.deepcopy(value)
        changed["imageBinding"]["revisionVerified"] = True
        changed["fields"]["reviewedAnalysis"]["source"]["revisionVerified"] = True
        with self.assertRaises(ValueError):
            self.validate(changed)

    def test_rejects_cross_file_node_revision_digest_and_managed_scope(self):
        for key, wrong in (("fileKey", "DifferentFile"), ("nodeId", "56:78"),
                           ("sourceVersion", "987654321"), ("analysisSourceDigest", "b" * 64)):
            with self.subTest(key=key):
                value = image_payload()
                value["imageBinding"][key] = wrong
                with self.assertRaises(ValueError):
                    self.validate(value)
        for context in (SimpleNamespace(purpose="story", file_key=None, node_id=None),
                        SimpleNamespace(purpose="screen", file_key="DifferentFile", node_id="12:34"),
                        SimpleNamespace(purpose="screen", file_key=None, node_id="56:78")):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.validate(image_payload(), context=context)

    def test_rejects_dimensions_not_matching_both_original_evidence_and_analysis(self):
        for path in (("layoutDimensions",), ("reviewedAnalysis", "dimensions")):
            value = image_payload()
            at = value["fields"]
            for part in path:
                at = at[part]
            at["width"] = 3
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(value)

    def test_local_archive_allows_reference_input_but_never_remote_revision_claim(self):
        value = image_payload(source_version="local-" + "c" * 64, revision=False)
        self.assertFalse(self.validate(value).binding.revisionVerified)
        value["imageBinding"]["revisionVerified"] = True
        value["fields"]["reviewedAnalysis"]["source"]["revisionVerified"] = True
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_image_and_binding_are_atomic_and_generation_only(self):
        self.assertIsNone(validate_image_input(None, None, operation="story.generate", fields={}))
        value = image_payload()
        for image, binding, operation in ((None, value["imageBinding"], "screen.generate"),
                                           (value["png"], None, "screen.generate"),
                                           (value["png"], value["imageBinding"], "screen.review-plan"),
                                           (value["png"], value["imageBinding"], "story.generate")):
            with self.subTest(operation=operation, image=image is None), self.assertRaises(ValueError):
                validate_image_input(image, binding, operation=operation, fields=value["fields"])

    def test_repair_validates_the_original_nested_generation_evidence(self):
        value = image_payload()
        value["operation"] = "screen.repair"
        value["fields"] = {"local": False, "diagnostic": "fixture", "previousDraft": "{}", "evidence": value["fields"]}
        self.assertEqual(self.validate(value).content, png())
        value["fields"]["evidence"]["fileKey"] = "OtherFile"
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_rejects_noncanonical_base64_altered_bytes_and_nonunit_scale(self):
        for key, wrong in (("sha256", "f" * 64), ("bytes", 1), ("scale", 2),
                           ("scale", True), ("schemaVersion", True), ("scaleVerified", "true")):
            value = image_payload()
            value["imageBinding"][key] = wrong
            with self.subTest(key=key, wrong=wrong), self.assertRaises(ValueError):
                self.validate(value)
        value = image_payload()
        value["png"] += "\n"
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_png_checks_full_crc_decoded_rows_and_unsupported_multiimage_chunks(self):
        valid = png()
        self.assertEqual(inspect_png(valid), (2, 2))
        bad_crc = bytearray(valid)
        bad_crc[-1] ^= 1
        animated = valid[:33] + chunk(b"acTL", struct.pack(">II", 2, 0)) + valid[33:]
        for content in (bytes(bad_crc), valid[:-12], valid + b"extra", animated,
                        png(pixels=b"\0" + b"\xff" * 30), png(pixels=b"\x07" + b"\xff" * 6 + b"\0" + b"\xff" * 6)):
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                inspect_png(content)

    def test_decoded_bomb_and_dimension_overflow_are_rejected_before_model_use(self):
        with self.assertRaises(ValueError):
            inspect_png(png(pixels=b"\0" * 1000000))
        with self.assertRaises(ValueError):
            inspect_png(png(width=8193, height=1, pixels=b"\0"))


if __name__ == "__main__":
    unittest.main()
