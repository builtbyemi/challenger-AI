"""Synthetic worker -> SQLite -> telemetry fixture for browser integration tests.

No dotenv loading, real sessions, Telegram client, or external HTTP transport.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from challenger.config import Settings, ApplicationSettings, PERSONAS
from challenger.coordinator import Coordinator
from challenger.engine import Worker, Refresh
from challenger.models import Message, Identity
from challenger.reporting import snapshot
from challenger.storage import Store
from challenger.telemetry import Publisher
from test_hourly_only import TOPICS


async def main():
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory)
        coordinator=Coordinator(base/'coordinator.sqlite3',25)
        settings=[]
        now=time.time()
        for n in range(1,6):coordinator.register(f'account_{n}',n)
        try:
            for n in range(1,6):
                config=Settings(1,'synthetic-secret',-100,'synthetic-groq','model',account_id=f'account_{n}',
                                database_path=base/f'{n}.sqlite3',auto_send=True,persona=PERSONAS[n-1])
                settings.append(config)
                store=Store(config.database_path,config.account_id,-100)
                try:
                    q,evidence,answer=TOPICS[n-1]
                    target=Message(n*100+50,-100,99,q,now)
                    context=Message(n*100+49,-100,98,evidence,now-10)
                    class Service:
                        async def sync(self):return [context,target]
                        async def refresh(self, _target):return Refresh(target,[context,target])
                        async def decide(self,*_):return dict(action='REPLY',target_id=target.message_id,text=answer,emoji=None,reason='Synthetic integration test')
                        async def send(self,text,target_id):return Message(n*100+80,-100,n,text,now,outgoing=True,reply_to_id=target_id)
                    service=Service()
                    worker=Worker(config,store,Identity(n),service,service,clock=lambda:now,coordinator=coordinator)
                    for i in range(n-1):worker.ingest(Message(n*100+i,-100,n,'Synthetic manual message',now-5,outgoing=True))
                    await worker.process_once()
                    assert store.quota_remaining(now)==5-n
                    store.set_state('health','connected');store.set_state('heartbeat',now);store.set_state('last_sync',now)
                finally:store.close()
            app=ApplicationSettings(tuple(settings),base/'coordinator.sqlite3')
            exported=[]
            def transport(request):
                if '/auth/' in str(request.url):return httpx.Response(200,json={'access_token':'synthetic-token','expires_in':3600})
                exported.append(json.loads(request.content))
                return httpx.Response(200)
            with patch.dict('os.environ',dict(TELEMETRY_SUPABASE_URL='https://test.supabase.co',TELEMETRY_SUPABASE_PUBLISHABLE_KEY='public',
                                            TELEMETRY_WORKER_EMAIL='worker@example.test',TELEMETRY_WORKER_PASSWORD='synthetic-password',TELEMETRY_WORKER_ID='test-worker')):
                async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
                    publisher=Publisher(app,http)
                    publisher.outbox.put(snapshot(app,now))
                    await publisher.publish_once()
            encoded=json.dumps(exported[0])
            assert 'synthetic-secret' not in encoded and 'Synthetic manual message' not in encoded
            print(encoded)
        finally:coordinator.close()


if __name__=='__main__':asyncio.run(main())
