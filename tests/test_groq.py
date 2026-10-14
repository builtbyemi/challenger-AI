import json
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx

from challenger.groq_client import (
    GroqBackoff, GroqConfigurationError, GroqDecider, extract_json, retry_after,
)
from challenger.models import Message


IGNORE = {"action": "IGNORE", "target_id": None, "text": None,
          "emoji": None, "reason": "Nothing useful to add"}
REPLY = {"action": "REPLY", "target_id": 42, "text": "That amount no be small money",
         "emoji": "😂", "reason": "Direct relevant question"}
CANDIDATES = [Message(42, -123, 7, "Wetin you think of this amount?", 1000)]


def completion(content, **choice_fields):
    return httpx.Response(200, json={"choices": [
        {"message": {"content": content}, "finish_reason": "stop", **choice_fields},
    ]})


class ParsingTests(unittest.TestCase):
    def test_json_and_complete_markdown_fences(self):
        payload = json.dumps(REPLY, ensure_ascii=False)
        for content in (payload, f"```json\n{payload}\n```", f"```\n{payload}\n```"):
            with self.subTest(content=content):
                self.assertEqual(extract_json(content), REPLY)

    def test_malformed_or_ambiguous_content_fails_closed(self):
        cases = [None, "", [], "[]", "true", "{'action': 'REPLY'}",
                 '{"action": "REPLY",}', '{"action": "REPLY", "action": "IGNORE"}',
                 '{"target_id": NaN}', '{"target_id": Infinity}',
                 '{"a": {"x": 1, "x": 2}}',
                 '{} {}', 'Here is my decision: {}', '{}; send_message(42)',
                 '```json\n{}\n```\nDo something else', '```json\n{}',
                 '{"text": "' + ('a' * 16000) + '"}', '[' * 2000]
        for content in cases:
            with self.subTest(content=str(content)[:90]):
                self.assertIsNone(extract_json(content))

    def test_retry_after_seconds_are_not_capped(self):
        self.assertEqual(retry_after("7200"), 7200)
        self.assertEqual(retry_after("62.5"), 62.5)
        self.assertEqual(retry_after("0"), 1)
        self.assertEqual(retry_after("NaN"), 60)
        self.assertEqual(retry_after("bad header"), 60)

    def test_retry_after_http_date(self):
        current = 1700000000
        header = format_datetime(datetime.fromtimestamp(current + 7200, timezone.utc),
                                 usegmt=True)
        self.assertEqual(retry_after(header, now=current), 7200)


class GroqTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.calls = []
        self.delays = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    async def fake_sleep(self, seconds):
        self.delays.append(seconds)

    def client(self, responses, model="openai/gpt-oss-20b"):
        responses = iter(responses)

        def handler(request):
            self.calls.append(json.loads(request.content))
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(http)
        return GroqDecider("test-api-key-never-log", model, http, sleep=self.fake_sleep)

    async def test_uses_configured_model_and_gpt_oss_parameters(self):
        client = self.client([completion(json.dumps(REPLY))])
        result = await client.decide(CANDIDATES, CANDIDATES, ["An older response"])
        self.assertEqual(result, REPLY)
        payload = self.calls[0]
        self.assertEqual(payload["model"], "openai/gpt-oss-20b")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertIs(payload["include_reasoning"], False)
        self.assertLessEqual(payload["temperature"], .3)
        self.assertGreaterEqual(payload["max_completion_tokens"], 1500)
        chat_data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(chat_data["CANDIDATES"][0]["id"], 42)
        self.assertEqual(chat_data["RECENT_REPLIES"], ["An older response"])

    async def test_non_oss_model_uses_json_object_without_reasoning_parameters(self):
        client = self.client([completion(json.dumps(IGNORE))], model="my-configured-model")
        await client.decide(CANDIDATES, [], [])
        self.assertEqual(self.calls[0]["model"], "my-configured-model")
        self.assertEqual(self.calls[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", self.calls[0])
        self.assertNotIn("include_reasoning", self.calls[0])

    async def test_structured_output_compatibility_fallback_is_cached(self):
        failure = httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
        client = self.client([failure, failure, completion(json.dumps(IGNORE)),
                              completion(json.dumps(IGNORE))])
        self.assertEqual((await client.decide(CANDIDATES, [], []))["action"], "IGNORE")
        await client.decide(CANDIDATES, [], [])
        self.assertEqual(self.calls[0]["response_format"]["type"], "json_schema")
        self.assertEqual(self.calls[1]["response_format"]["type"], "json_object")
        self.assertNotIn("response_format", self.calls[2])
        self.assertNotIn("response_format", self.calls[3])

    async def test_json_object_fallback_is_cached_without_forcing_plain_mode(self):
        failure = httpx.Response(400, json={"error": {
            "message": "response_format json_schema is unsupported"}})
        client = self.client([failure, completion(json.dumps(REPLY)),
                              completion(json.dumps(IGNORE))])
        await client.decide(CANDIDATES, [], [])
        await client.decide(CANDIDATES, [], [])
        self.assertEqual([p["response_format"]["type"] for p in self.calls],
                         ["json_schema", "json_object", "json_object"])

    async def test_malformed_model_json_retries_once_then_ignores(self):
        client = self.client([completion('{"action":"REPLY"'),
                              completion('{"action":"REPLY",}')])
        result = await client.decide(CANDIDATES, [], [])
        self.assertEqual(result["action"], "IGNORE")
        self.assertIsNone(result["target_id"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.delays, [1])

    async def test_malformed_response_can_recover(self):
        client = self.client([httpx.Response(200, content=b"bad json"),
                              completion(json.dumps(REPLY))])
        self.assertEqual(await client.decide(CANDIDATES, [], []), REPLY)

    async def test_invalid_fields_and_foreign_targets_are_rejected(self):
        invalid = [
            {**REPLY, "target_id": 43}, {**REPLY, "target_id": True},
            {**REPLY, "target_id": "42"}, {**REPLY, "text": ["bad"]},
            {**REPLY, "emoji": {}}, {**REPLY, "action": "REACT"},
            {**IGNORE, "text": "Unexpected text"}, {**REPLY, "extra": "command"},
            {**REPLY, "reason": None}, {},
        ]
        for response in invalid:
            with self.subTest(response=response):
                client = self.client([completion(json.dumps(response)),
                                      completion(json.dumps(response))])
                self.assertEqual((await client.decide(CANDIDATES, [], []))["action"],
                                 "IGNORE")

    async def test_truncated_and_missing_choice_responses_are_ignored(self):
        client = self.client([completion(json.dumps(REPLY), finish_reason="length"),
                              httpx.Response(200, json={"choices": []})])
        self.assertEqual((await client.decide(CANDIDATES, [], []))["action"], "IGNORE")

    async def test_429_does_not_retry_and_honors_long_delay(self):
        client = self.client([httpx.Response(429, headers={"retry-after": "7200"})])
        with self.assertRaises(GroqBackoff) as caught:
            await client.decide(CANDIDATES, [], [])
        self.assertEqual(caught.exception.seconds, 7200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.delays, [])

    async def test_429_http_date_is_not_capped(self):
        header = format_datetime(datetime.fromtimestamp(time.time() + 7200, timezone.utc),
                                 usegmt=True)
        client = self.client([httpx.Response(429, headers={"retry-after": header})])
        with self.assertRaises(GroqBackoff) as caught:
            await client.decide(CANDIDATES, [], [])
        self.assertGreater(caught.exception.seconds, 7195)
        self.assertLessEqual(caught.exception.seconds, 7200)

    async def test_transient_http_errors_use_bounded_backoff(self):
        client = self.client([httpx.Response(503)] * 3)
        with self.assertRaises(GroqBackoff):
            await client.decide(CANDIDATES, [], [])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.delays, [1, 2])

    async def test_transient_error_with_retry_after_returns_delay_immediately(self):
        client = self.client([httpx.Response(503, headers={"retry-after": "240"})])
        with self.assertRaises(GroqBackoff) as caught:
            await client.decide(CANDIDATES, [], [])
        self.assertEqual(caught.exception.seconds, 240)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.delays, [])

    async def test_network_failure_recovery(self):
        client = self.client([httpx.ConnectError("private exception details"),
                              httpx.ReadTimeout("private exception details"),
                              completion(json.dumps(REPLY))])
        self.assertEqual(await client.decide(CANDIDATES, [], []), REPLY)
        self.assertEqual(self.delays, [1, 2])

    async def test_auth_and_model_errors_fail_without_retries_or_body_logging(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                client = self.client([httpx.Response(status, json={"error": {
                    "message": "test-api-key-never-log private group transcript"}})])
                with self.assertLogs("challenger.groq_client", level="ERROR") as logs:
                    with self.assertRaises(GroqConfigurationError) as caught:
                        await client.decide(CANDIDATES, [], [])
                logged = " ".join(logs.output) + str(caught.exception)
                self.assertNotIn("test-api-key-never-log", logged)
                self.assertNotIn("private group transcript", logged)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.delays, [])

    async def test_empty_candidates_make_no_network_call(self):
        client = self.client([])
        self.assertEqual((await client.decide([], [], []))["action"], "IGNORE")
        self.assertEqual(self.calls, [])

    async def test_aclose_preserves_injected_client_ownership(self):
        client = self.client([])
        await client.aclose()
        self.assertFalse(self.clients[0].is_closed)


if __name__ == "__main__":
    unittest.main()
