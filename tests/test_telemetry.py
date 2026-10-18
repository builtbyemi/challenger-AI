import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import main
from challenger.config import ApplicationSettings, Settings
from challenger.telemetry import Publisher, failure_details, retry_delay


ENV = dict(TELEMETRY_SUPABASE_URL='https://test.supabase.co', TELEMETRY_SUPABASE_PUBLISHABLE_KEY='public',
           TELEMETRY_WORKER_EMAIL='worker@example.test', TELEMETRY_WORKER_PASSWORD='private-password',
           TELEMETRY_WORKER_ID='local-worker')


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = ApplicationSettings((Settings(1,'hash',-1,'key','model'),),Path(self.temp.name)/'coordinator.sqlite3')
        self.payload = dict(version=1,generated_at=time.time(),accounts=[])

    async def publisher(self, handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        publisher = Publisher(self.app, http)
        publisher.outbox.put(self.payload)
        return publisher

    async def test_success_uses_worker_token_and_exact_rpc_parameters(self):
        seen=[]
        def handler(request):
            seen.append(request)
            if '/auth/' in request.url.path:
                return httpx.Response(200,json=dict(access_token='private-token',expires_in=3600))
            self.assertEqual(request.headers['Authorization'], 'Bearer private-token')
            self.assertEqual(json.loads(request.content),dict(p_worker='local-worker',p_payload=self.payload))
            return httpx.Response(204)
        publisher=await self.publisher(handler)
        await publisher.publish_once()
        self.assertIsNone(publisher.outbox.get())
        publisher.outbox.put(self.payload)
        await publisher.publish_once()
        self.assertEqual(len(seen),3)  # Cached token; no unnecessary password login.

    async def test_auth_failure_never_calls_rpc_and_keeps_snapshot(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(400,json=dict(error_code='invalid_credentials',message='private-password'))
        publisher=await self.publisher(handler)
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await publisher.publish_once()
        self.assertEqual(len(seen),1)
        self.assertEqual(publisher.outbox.get(),self.payload)
        details=failure_details(caught.exception)
        self.assertIn('stage=auth http=400 code=invalid_credentials',details)
        self.assertNotIn('private-password',details)

    async def test_expired_token_reauthenticates_on_next_attempt(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            if '/auth/' in request.url.path:return httpx.Response(200,json=dict(access_token='private-token'))
            return httpx.Response(401 if len(calls)==2 else 204)
        publisher=await self.publisher(handler)
        with self.assertRaises(httpx.HTTPStatusError):await publisher.publish_once()
        self.assertIsNone(publisher.token)
        await publisher.publish_once()
        self.assertEqual(len(calls),4)
        self.assertIsNone(publisher.outbox.get())

    async def test_binding_failure_is_actionable_and_retained_across_restart(self):
        def handler(request):
            if '/auth/' in request.url.path:return httpx.Response(200,json=dict(access_token='private-token'))
            return httpx.Response(403,json=dict(code='42501',message='private-password private-token'))
        publisher=await self.publisher(handler)
        with self.assertRaises(httpx.HTTPStatusError) as caught:await publisher.publish_once()
        details=failure_details(caught.exception)
        self.assertIn('category=publisher_binding',details)
        self.assertIn('http=403 code=42501',details)
        self.assertNotIn('private-',details)
        self.assertEqual(Publisher(self.app,publisher.http).outbox.get(),self.payload)

    async def test_bad_auth_response_and_network_timeout_preserve_outbox(self):
        for response in [httpx.Response(200,json=dict(access_token=None)),httpx.ReadTimeout('private-password')]:
            def handler(request):
                if isinstance(response,Exception):raise response
                return response
            publisher=await self.publisher(handler)
            with self.assertRaises((ValueError,httpx.ReadTimeout)) as caught:await publisher.publish_once()
            self.assertNotIn('private-password',failure_details(caught.exception))
            self.assertIsNotNone(publisher.outbox.get())

    async def test_capture_during_upload_keeps_newer_snapshot(self):
        publisher=None
        newer={**self.payload,'generated_at':self.payload['generated_at']+1}
        def handler(request):
            if '/auth/' in request.url.path:return httpx.Response(200,json=dict(access_token='private-token'))
            publisher.outbox.put(newer)
            return httpx.Response(204)
        publisher=await self.publisher(handler)
        await publisher.publish_once()
        self.assertEqual(publisher.outbox.get(),newer)

    async def test_run_retries_with_backoff_and_safe_logs(self):
        publisher=await self.publisher(lambda _:httpx.Response(204))
        request=httpx.Request('POST','https://test.supabase.co/rest/v1/rpc/publish_dashboard_snapshot')
        error=httpx.HTTPStatusError('private-password',request=request,
              response=httpx.Response(503,request=request,json={'code':'private-token'}))
        publisher.capture=AsyncMock()
        publisher.publish_once=AsyncMock(side_effect=[error,error,asyncio.CancelledError()])
        publisher.wake.set()
        with patch('challenger.telemetry.asyncio.sleep',new=AsyncMock()) as sleep, self.assertLogs('challenger.telemetry',logging.WARNING) as logs:
            with self.assertRaises(asyncio.CancelledError):await publisher.run()
        self.assertEqual([c.args[0] for c in sleep.await_args_list],[5,10])
        self.assertIn('http=503 code=unknown',' '.join(logs.output))
        self.assertNotIn('private-', ' '.join(logs.output))

    async def test_binding_diagnostic_only_prints_booleans_not_raw_body(self):
        publisher=await self.publisher(lambda _:httpx.Response(200,json=dict(binding_exists=True,worker_id_matches=False,secret='private-token')))
        publisher.token='private-token'
        self.assertIn('worker_id differs',await publisher.binding_status())

    def test_retry_after_and_unknown_error_codes_are_bounded(self):
        request=httpx.Request('POST','https://test.supabase.co/rest/v1/rpc/test')
        error=httpx.HTTPStatusError('private',request=request,response=httpx.Response(429,request=request,
                                   headers={'Retry-After':'999999'},json={'code':'password-secret'}))
        self.assertEqual(retry_delay(error,5),300)
        self.assertIn('code=unknown',failure_details(error))


class TelemetryCliTests(unittest.TestCase):
    def test_one_shot_check_forces_preview_and_never_starts_workers(self):
        app=ApplicationSettings((Settings(1,'hash',-1,'key','model',auto_send=True),),Path('unused.sqlite3'))
        with patch.object(main,'load_application_settings',return_value=app), \
             patch('sys.argv',['main.py','--telemetry-check']), \
             patch('challenger.telemetry.check_telemetry',new=AsyncMock(return_value=0)) as check, \
             patch('challenger.application.run_application',new=AsyncMock()) as worker:
            self.assertEqual(main.main(),0)
        self.assertFalse(check.await_args.args[0].accounts[0].auto_send)
        worker.assert_not_called()


if __name__ == '__main__':unittest.main()
