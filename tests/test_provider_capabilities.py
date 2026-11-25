import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sfdc_agent.provider_capabilities import cli_identity, provider_capabilities, provider_receipt, reported_stream_model


class ProviderCapabilitiesTests(unittest.TestCase):
    def test_current_transports_never_implicitly_enable_image_input(self):
        for provider in ("kiro", "openai", "azure"):
            capabilities = provider_capabilities(provider)
            self.assertTrue(capabilities["structuredText"])
            self.assertFalse(capabilities["images"])
        self.assertEqual(provider_capabilities("kiro")["imageInputStatus"], "unsupported")
        with self.assertRaises(ValueError):
            provider_capabilities("automatic-fallback")

    def test_receipt_distinguishes_configured_and_provider_reported_model(self):
        receipt = provider_receipt("openai", "configured-model", "reported-model")
        self.assertEqual(receipt["configuredModel"], "configured-model")
        self.assertEqual(receipt["reportedModel"], "reported-model")
        self.assertEqual(provider_receipt("kiro")["modelSelection"], "provider-managed")
        self.assertIsNone(provider_receipt("kiro", reported_model="unsafe\nvalue")["reportedModel"])

    def test_cli_identity_hashes_launched_bytes_without_a_process_or_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "synthetic-cli"
            binary.write_bytes(b"fixture executable bytes")
            receipt = cli_identity(str(binary))
            self.assertEqual(receipt["executableSha256"], hashlib.sha256(binary.read_bytes()).hexdigest())
            self.assertNotIn(directory, json.dumps(receipt))
            self.assertIsNone(receipt["version"])
            self.assertEqual(receipt["versionStatus"], "unreported")

    def test_only_explicit_terminal_model_metadata_is_retained(self):
        content = [{"type": "sessionUpdate", "data": {"model": "ignored-partial"}},
                   {"type": "runFinished", "data": {"modelId": "model-snapshot", "finalText": "private source text"}}]
        body = b"\n".join(json.dumps(event).encode() for event in content)
        self.assertEqual(reported_stream_model(body), "model-snapshot")
        self.assertIsNone(reported_stream_model(b'{"type":"runFinished","data":{"finalText":"model-snapshot"}}'))


if __name__ == "__main__":
    unittest.main()
