import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from challenger.groq_client import GroqBackoff, GroqConfigurationError, SharedGroqDecider
from challenger.models import Message
from challenger.storage import Store


IGNORE = {"action": "IGNORE", "target_id": None, "text": None,
          "emoji": None, "reason": "Nothing useful to add"}


def completion(content=None):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(IGNORE) if content is None else content}}]})


def candidate(message_id=42, sent_at=1000, chat_id=-123):
    return Message(message_id, chat_id, 7, "Which amount was listed for the fare?", sent_at)


class Coordinator:
    def __init__(self):
        self.blocks = {}

    def blocked_until(self, scope):
        return self.blocks.get(scope, 0)

    def set_block(self, scope, until):
        self.blocks[scope] = max(until, self.blocked_until(scope))


class SharedGroqTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.shared = []
        self.now = 1000.0

    async def asyncTearDown(self):
        for shared in self.shared:
            await shared.aclose()
        for client in self.clients:
            await client.aclose()

    def build(self, handler, *, coordinator=None, concurrency=1):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(http)
        shared = SharedGroqDecider("test-key", "openai/gpt-oss-20b",
                                   coordinator or Coordinator(), concurrency=concurrency,
                                   http=http, clock=lambda: self.now)
        self.shared.append(shared)
        return shared

    async def test_one_account_429_stops_queued_accounts_before_their_requests(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(429, headers={"retry-after": "7200"})

        coordinator = Coordinator()
        shared = self.build(handler, coordinator=coordinator)
        first = asyncio.create_task(shared.decide([candidate()], [], []))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(shared.decide([candidate(99, chat_id=-999)], [], []))
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(calls, 1)
        self.assertTrue(all(isinstance(outcome, GroqBackoff) for outcome in outcomes))
        self.assertEqual(coordinator.blocked_until("groq"), 8200)
        with self.assertRaises(GroqBackoff) as caught:
            await shared.decide([candidate()], [], [])
        self.assertEqual(caught.exception.seconds, 7200)
        self.assertEqual(calls, 1)

    async def test_shared_backoff_survives_coordinator_restart(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"retry-after": "1800"})

        with TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.sqlite3"
            coordinator = Store(path, "shared-provider", 0)
            try:
                shared = self.build(handler, coordinator=coordinator)
                with self.assertRaises(GroqBackoff):
                    await shared.decide([candidate()], [], [])
            finally:
                coordinator.close()
            reopened = Store(path, "shared-provider", 0)
            try:
                restarted = self.build(handler, coordinator=reopened)
                with self.assertRaises(GroqBackoff) as caught:
                    await restarted.decide([candidate()], [], [])
                self.assertEqual(caught.exception.seconds, 1800)
                self.assertEqual(calls, 1)
            finally:
                reopened.close()

    async def test_concurrency_is_bounded_and_clamped_between_one_and_three(self):
        for configured, expected in ((0, 1), (2, 2), (99, 3)):
            with self.subTest(configured=configured):
                started, release = asyncio.Event(), asyncio.Event()
                active = maximum = calls = 0

                async def handler(request):
                    nonlocal active, maximum, calls
                    active += 1
                    calls += 1
                    maximum = max(maximum, active)
                    if active == expected:
                        started.set()
                    try:
                        await release.wait()
                        return completion()
                    finally:
                        active -= 1

                shared = self.build(handler, concurrency=configured)
                pending = [asyncio.create_task(shared.decide([candidate(index)], [], []))
                           for index in range(1, 6)]
                await asyncio.wait_for(started.wait(), 2)
                self.assertEqual(calls, expected)
                release.set()
                results = await asyncio.gather(*pending)
                self.assertEqual(maximum, expected)
                self.assertEqual(calls, 5)
                self.assertTrue(all(result["action"] == "IGNORE" for result in results))

    async def test_candidate_freshness_is_checked_after_semaphore_wait(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return completion()

        shared = self.build(handler)
        first = asyncio.create_task(shared.decide([candidate()], [], []))
        await asyncio.wait_for(started.wait(), 2)
        queued = asyncio.create_task(shared.decide([candidate(99)], [], []))
        await asyncio.sleep(0)
        self.now = 1720
        release.set()
        _, result = await asyncio.gather(first, queued)
        self.assertEqual(result["action"], "IGNORE")
        self.assertIn("expired", result["reason"])
        self.assertEqual(calls, 1)

    async def test_only_fresh_candidates_sent_and_account_contexts_stay_separate(self):
        payloads = []

        def handler(request):
            payloads.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
            return completion()

        shared = self.build(handler)
        await shared.decide([candidate(42), candidate(43, sent_at=280)],
                            [candidate(44)], ["First account reply"])
        await shared.decide([candidate(99, chat_id=-999)],
                            [candidate(100, chat_id=-999)], ["Second account reply"])
        self.assertEqual([m["id"] for m in payloads[0]["CANDIDATES"]], [42])
        self.assertEqual([m["id"] for m in payloads[1]["CANDIDATES"]], [99])
        self.assertEqual(payloads[0]["RECENT_REPLIES"], ["First account reply"])
        self.assertEqual(payloads[1]["RECENT_REPLIES"], ["Second account reply"])

    async def test_malformed_decision_is_ignored_and_semaphore_is_released(self):
        responses = iter([completion("bad json"), completion("{}"), completion()])
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return next(responses)

        shared = self.build(handler)
        shared._delegate._sleep = AsyncMock()
        result = await shared.decide([candidate()], [], [])
        self.assertEqual(result["action"], "IGNORE")
        self.assertIn("Malformed", result["reason"])
        self.assertEqual(await shared.decide([candidate()], [], []), IGNORE)
        self.assertEqual(calls, 3)

    async def test_configuration_error_is_shared_without_repeating_provider_requests(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(401)

        coordinator = Coordinator()
        shared = self.build(handler, coordinator=coordinator)
        for _ in range(2):
            with self.assertRaises(GroqConfigurationError):
                await shared.decide([candidate()], [], [])
        self.assertEqual(calls, 1)
        self.assertEqual(coordinator.blocked_until("groq"), 1060)

    async def test_cancellation_releases_semaphore(self):
        started = asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Event().wait()
            return completion()

        shared = self.build(handler)
        task = asyncio.create_task(shared.decide([candidate()], [], []))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await shared.decide([candidate()], [], []), IGNORE)

    async def test_owned_client_created_once_and_closed_once(self):
        http = type("OwnedHTTP", (), {"aclose": AsyncMock()})()
        with patch("challenger.groq_client.httpx.AsyncClient", return_value=http) as factory:
            shared = SharedGroqDecider("test-key", "model", Coordinator())
            await shared.aclose()
            await shared.aclose()
        factory.assert_called_once()
        http.aclose.assert_awaited_once()

    async def test_injected_http_client_remains_caller_owned(self):
        shared = self.build(lambda request: completion())
        await shared.aclose()
        await shared.aclose()
        self.assertFalse(self.clients[-1].is_closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await shared.decide([candidate()], [], [])

    async def test_concurrent_schema_failures_do_not_double_downgrade(self):
        both_started = asyncio.Event()
        strict_calls = 0
        modes = []

        async def handler(request):
            nonlocal strict_calls
            payload = json.loads(request.content)
            mode = payload.get("response_format", {}).get("type", "plain")
            modes.append(mode)
            if mode == "json_schema":
                strict_calls += 1
                if strict_calls == 2:
                    both_started.set()
                await both_started.wait()
                return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
            return completion()

        shared = self.build(handler, concurrency=2)
        await asyncio.wait_for(asyncio.gather(
            shared.decide([candidate()], [], []),
            shared.decide([candidate(99)], [], []),
        ), timeout=2)
        self.assertEqual(modes.count("json_schema"), 2)
        self.assertEqual(modes.count("json_object"), 2)
        self.assertNotIn("plain", modes)


if __name__ == "__main__":
    unittest.main()
