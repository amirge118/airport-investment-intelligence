import json
import unittest
import urllib.error
from unittest import mock

from airport_intel.planning import gemini, groq, llm
from airport_intel.planning.llm_contract import PLAN_SCHEMA

FAKE_KEY = "gsk_fake-key-not-real"


def _fake_response(content):
    payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
    response = mock.MagicMock()
    response.read.return_value = payload
    response.__enter__ = mock.Mock(return_value=response)
    response.__exit__ = mock.Mock(return_value=False)
    return response


class GroqRequestTests(unittest.TestCase):
    """Mocked transport only: no live Groq call."""

    def _propose(self, content='{"intent": "rank"}', **env):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return _fake_response(content)

        with mock.patch.dict("os.environ", {"GROQ_API_KEY": FAKE_KEY, **env}, clear=True), \
                mock.patch.object(groq.urllib.request, "urlopen", side_effect=fake_urlopen):
            proposal = groq.propose_plan("Which airports in Texas are growing?", "airports=[]")
        return proposal, captured["request"]

    def test_key_only_in_authorization_header(self):
        _, request = self._propose()
        self.assertEqual(request.headers.get("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertNotIn(FAKE_KEY, request.full_url)
        self.assertNotIn(FAKE_KEY, request.data.decode("utf-8"))

    def test_sends_an_explicit_user_agent(self):
        _, request = self._propose()
        self.assertEqual(request.headers.get("User-agent"), groq.USER_AGENT)

    def test_strict_schema_and_low_reasoning(self):
        _, request = self._propose()
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], groq.DEFAULT_MODEL)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], PLAN_SCHEMA)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertFalse(body["include_reasoning"])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_returns_parsed_content(self):
        proposal, _ = self._propose('{"intent": "compare", "entities": ["PDX", "SEA"]}')
        self.assertEqual(proposal["entities"], ["PDX", "SEA"])

    def test_malformed_content_is_unavailable(self):
        with self.assertRaises(groq.GroqUnavailable):
            self._propose("not json")
        with self.assertRaises(groq.GroqUnavailable):
            self._propose("[1, 2]")

    def test_http_error_never_carries_the_key(self):
        error = urllib.error.HTTPError(groq.API_URL, 429, "Too Many Requests", {}, None)
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": FAKE_KEY}, clear=True), \
                mock.patch.object(groq.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(groq.GroqUnavailable) as caught:
                groq.propose_plan("x")
        self.assertIn("429", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_model_without_strict_schema_is_refused_before_any_request(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": FAKE_KEY, "GROQ_MODEL": "llama-3.1-8b-instant"},
                             clear=True), \
                mock.patch.object(groq.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(groq.GroqUnavailable):
                groq.propose_plan("x")
        urlopen.assert_not_called()

    def test_schema_is_strict_compatible(self):
        """Strict mode needs every property required and every object closed."""
        def check(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            if node.get("type") == "array":
                check(node["items"])
        check(PLAN_SCHEMA)


class ProviderSelectionTests(unittest.TestCase):
    def test_groq_preferred_when_both_configured(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "a", "GEMINI_API_KEY": "b"}, clear=True):
            self.assertIs(llm.active(), groq)
            self.assertEqual(llm.label(), f"groq:{groq.DEFAULT_MODEL}")

    def test_pinned_provider_wins(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "a", "GEMINI_API_KEY": "b",
                                            "PLANNER_PROVIDER": "gemini"}, clear=True):
            self.assertIs(llm.active(), gemini)

    def test_pinned_provider_without_key_means_keyless(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "a", "PLANNER_PROVIDER": "gemini"}, clear=True):
            self.assertIsNone(llm.active())
            self.assertIsNone(llm.label())

    def test_no_key_is_keyless_and_propose_raises(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(llm.is_configured())
            with self.assertRaises(llm.LLMUnavailable):
                llm.propose_plan("x")

    def test_provider_failures_share_one_type(self):
        self.assertTrue(issubclass(groq.GroqUnavailable, llm.LLMUnavailable))
        self.assertTrue(issubclass(gemini.GeminiUnavailable, llm.LLMUnavailable))


if __name__ == "__main__":
    unittest.main()
