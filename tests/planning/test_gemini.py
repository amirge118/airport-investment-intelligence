import json
import unittest
from unittest import mock

from airport_intel.planning import gemini


class DefaultModelTests(unittest.TestCase):
    """Regression guard: the default must never be a model Google's own docs list as shut down."""

    def test_default_model_is_not_shut_down(self):
        self.assertNotIn(gemini.DEFAULT_MODEL, gemini.SHUT_DOWN_MODELS)

    def test_default_model_is_in_the_documented_active_list(self):
        self.assertIn(gemini.DEFAULT_MODEL, gemini.ACTIVE_MODELS)

    def test_known_shut_down_models_are_not_silently_reintroduced(self):
        # Previously-shipped/suggested defaults that are confirmed shut down.
        for dead_model in ("gemini-2.0-flash", "gemini-3.1-flash-lite-preview"):
            self.assertIn(dead_model, gemini.SHUT_DOWN_MODELS)
            self.assertNotEqual(gemini.DEFAULT_MODEL, dead_model)


class ConfigurationTests(unittest.TestCase):
    def test_not_configured_without_env_var(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(gemini.is_configured())

    def test_model_name_honors_env_override(self):
        with mock.patch.dict("os.environ", {"GEMINI_MODEL": "gemini-3.5-flash-lite"}):
            self.assertEqual(gemini.model_name(), "gemini-3.5-flash-lite")

    def test_model_name_falls_back_to_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(gemini.model_name(), gemini.DEFAULT_MODEL)

    def test_propose_plan_raises_when_not_configured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(gemini.GeminiUnavailable):
                gemini.propose_plan("Which airports in Texas are growing?")


class RequestShapeTests(unittest.TestCase):
    """A mocked request-shape test, no live Gemini call. Asserts the key goes in the
    x-goog-api-key header (never the URL), and no deprecated sampling
    parameter is sent for the Gemini 3.6+ request shape."""

    def test_request_shape_has_header_key_and_no_deprecated_params(self):
        fake_payload = json.dumps({
            "candidates": [{"content": {"parts": [{"text": json.dumps({"intent": "rank"})}]}}]
        }).encode("utf-8")
        fake_response = mock.MagicMock()
        fake_response.read.return_value = fake_payload
        fake_response.__enter__ = mock.Mock(return_value=fake_response)
        fake_response.__exit__ = mock.Mock(return_value=False)

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return fake_response

        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key-not-real"}), \
             mock.patch.object(gemini.urllib.request, "urlopen", side_effect=fake_urlopen):
            gemini.propose_plan("Which airports in Texas are growing?")

        request = captured["request"]
        self.assertNotIn("fake-key-not-real", request.full_url)
        self.assertNotIn("key=", request.full_url)
        self.assertEqual(request.headers.get("X-goog-api-key"), "fake-key-not-real")
        body = json.loads(request.data.decode("utf-8"))
        generation_config = body.get("generationConfig", {})
        for deprecated in ("temperature", "top_p", "top_k", "candidate_count", "candidateCount", "topP", "topK"):
            self.assertNotIn(deprecated, generation_config)
        self.assertNotEqual(body["contents"][-1].get("role"), "model")


if __name__ == "__main__":
    unittest.main()
