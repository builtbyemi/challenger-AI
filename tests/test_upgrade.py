import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from challenger.config import Settings, ApplicationSettings, load_application_settings, PERSONAS
from challenger.coordinator import Coordinator
from challenger.discussions import choose_discussion
from challenger.engine import Worker, Refresh
from challenger.groq_client import GroqBackoff, SharedGroqDecider, PersonaDecider
from challenger.models import Identity, Message
from challenger.reporting import snapshot, import_verified_points
from challenger.selection import validate_decision
from challenger.storage import Store
from challenger.telemetry import Outbox, Publisher
from challenger.usage import UsageLedger
from challenger.windows import window_start


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(1, 'secret-hash', -100, 'secret-groq', 'model',
                                 database_path=self.base / 'account.sqlite3', timezone='UTC')

    def store(self, **kwargs):
        store = Store(self.settings.database_path, 'account_1', -100, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_blank_optional_credentials_fall_back_and_five_personalities(self):
        env = dict(API_ID='1', API_HASH='secret', TARGET_CHAT_ID='-100', GROQ_API_KEY='key')
        for n in range(1,6):
            env.update({f'ACCOUNT_{n}_ENABLED':'true', f'ACCOUNT_{n}_API_ID':'',f'ACCOUNT_{n}_API_HASH':''})
        with patch.dict(os.environ, env, clear=True):
            app = load_application_settings(self.base)
        self.assertEqual(len({a.persona for a in app.accounts}), 5)
        self.assertEqual(tuple(a.persona for a in app.accounts), PERSONAS)
        self.assertTrue(all(a.api_hash == 'secret' for a in app.accounts))
        self.assertEqual(len({a.session_path for a in app.accounts}),5)
        self.assertEqual(app.group_max_actions,25)

    def test_all_manual_bot_combinations_across_all_accounts(self):
        for account in range(1,6):
            for manual in range(7):
                for automated in range(6):
                    with self.subTest(account=account,manual=manual,automated=automated):
                        store = Store(':memory:', f'account_{account}', -100)
                        try:
                            for i in range(manual):
                                outgoing=Message(100+i,-100,account,'Manual message',10000,outgoing=True)
                                store.observe_outgoing(outgoing)
                                store.observe_outgoing(outgoing)
                            allowed=0
                            for i in range(automated):
                                target=Message(200+i,-100,99,'What caused transfer delays?',10000)
                                store.enqueue(target)
                                action=store.reserve(target,'Network delays affect transfers','live',10000)
                                if action is not None:
                                    allowed+=1
                                    echo=Message(300+i,-100,account,'Network delays affect transfers',10000,reply_to_id=target.message_id,outgoing=True)
                                    store.observe_outgoing(echo)
                                    store.finish(action,'sent',10000,echo.message_id)
                                    store.observe_outgoing(echo)
                            self.assertEqual(allowed,min(automated,max(0,5-manual)))
                            self.assertEqual(store.quota_remaining(10000),max(0,5-manual-allowed))
                            self.assertEqual(store.quota_remaining(13600),5)
                        finally:
                            store.close()

    def test_fixed_boundary_rolling_boundary_and_restart(self):
        for mode in ('fixed','rolling'):
            path=self.base/f'{mode}.sqlite3'
            store=Store(path,'account_1',-100,window_mode=mode,timezone='UTC')
            for i in range(5):store.observe_outgoing(Message(i+1,-100,1,'Manual text',3590+i,outgoing=True))
            store.close()
            store=Store(path,'account_1',-100,window_mode=mode,timezone='UTC')
            try:
                self.assertEqual(store.quota_remaining(3599),0)
                self.assertEqual(store.quota_remaining(3600),5 if mode=='fixed' else 0)
                self.assertEqual(store.quota_wait(3600),0 if mode=='fixed' else 3590)
            finally:store.close()

    def test_identical_manual_send_cannot_disappear_when_bot_id_is_returned(self):
        store=self.store()
        coordinator=Coordinator(self.base/'coordinator.sqlite3',25)
        try:
            coordinator.register('account_1',1)
            target=Message(1,-100,2,'What caused network issues today?',10000)
            store.enqueue(target)
            action=store.reserve(target,'Maintenance caused network issues','live',10000)
            claim=coordinator.reserve('account_1',-100,1,'Maintenance caused network issues','live',10000)
            manual=Message(101,-100,1,'Maintenance caused network issues',10001,reply_to_id=1,outgoing=True)
            store.observe_outgoing(manual);coordinator.observe_outgoing('account_1',manual)
            store.finish(action,'sent',10002,102);coordinator.finish(claim,'sent',10002,102)
            self.assertEqual(store.quota_remaining(10002),3)
            self.assertEqual(coordinator.quota_remaining(-100,10002),23)
            actions=store.export_actions(10002)
            self.assertEqual({a['telegram_id']:a['mode'] for a in actions},{101:'observed',102:'live'})
            store.observe_outgoing(manual)
            self.assertEqual(store.quota_remaining(10002),3)
        finally:coordinator.close()

    def test_short_output_english_and_pidgin(self):
        target=Message(1,-100,2,'@challenger what caused transfer delays?',10000,mentions=('challenger',))
        context=[Message(2,-100,3,'Network issues caused transfer delays yesterday',9990)]
        raw=dict(action='REPLY',target_id=1,text='Network issues caused transfer delays',emoji=None,reason='Grounded')
        result=validate_decision(raw,[target],context,Identity(1,'challenger'),[],now=10000,max_words=5)
        self.assertEqual(result.action,'REPLY',result.reason)
        raw['text']='Network issues caused transfer delays yesterday'
        self.assertEqual(validate_decision(raw,[target],context,Identity(1,'challenger'),[],now=10000,max_words=5).action,'IGNORE')
        pidgin=replace(target,text='@challenger shey you be bot?')
        self.assertEqual(validate_decision(raw,[pidgin],[],Identity(1,'challenger'),[],now=10000,max_words=5).action,'REPLY')

    def test_quiet_starter_requires_topic_context_and_human_response(self):
        settings=replace(self.settings,discussion_topics=('What makes transfers reliable?','Which fees affect transfers?'))
        context=[Message(1,-100,8,'Transfers suffer delays every morning',9000)]
        decision=choose_discussion(settings,context,[],[],{1},10000)
        self.assertEqual(decision.action,'START_DISCUSSION')
        self.assertEqual(choose_discussion(settings,context,[],[],{1},9500).action,'IGNORE')
        self.assertEqual(choose_discussion(settings,[],[],[],{1},10000).action,'IGNORE')
        action=dict(kind='START_DISCUSSION',activity_at=9500,status='sent')
        self.assertEqual(choose_discussion(settings,context,[action],[],{1},11000).action,'IGNORE')
        self.assertEqual(choose_discussion(settings,context,[],[decision.text],{1},10000).text,'Which fees affect transfers?')
        self.assertEqual(choose_discussion(settings,[replace(context[0],sender_id=1)],[],[],{1},10000).action,'IGNORE')

    def test_telemetry_redaction_daily_totals_and_points_are_separate(self):
        store=self.store()
        store.observe_outgoing(Message(10,-100,1,'secret private conversation',10000,outgoing=True))
        store.set_state('health','connected');store.set_state('heartbeat',10000);store.set_state('last_sync',10000)
        app=ApplicationSettings((self.settings,),self.base/'coordinator.sqlite3')
        data=snapshot(app,10001)
        account=data['accounts'][0]
        self.assertEqual((account['manual'],account['bot'],account['remaining'],account['today']),(1,0,4,1))
        self.assertIsNone(account['verified_today'])
        encoded=json.dumps(data)
        for secret in ('secret private conversation','secret-hash','secret-groq','telegram_id','sender_id'):
            self.assertNotIn(secret,encoded)
        self.assertEqual(snapshot(app,10100)['accounts'][0]['health'],'offline')

    def test_point_import_idempotent_with_explicit_evidence(self):
        self.store()
        path=self.base/'points.json'
        path.write_text(json.dumps([dict(reference='organizer export row 17',awarded_at=10000,points=2)]))
        import_verified_points(self.settings,path);import_verified_points(self.settings,path)
        data=snapshot(ApplicationSettings((self.settings,),self.base/'coordinator.sqlite3'),10001)
        self.assertEqual(data['accounts'][0]['verified_today'],2)
        self.assertEqual(data['accounts'][0]['today'],0)

    def test_outbox_is_bounded_and_restart_does_not_lose_latest_snapshot(self):
        path=self.base/'outbox.sqlite3'
        outbox=Outbox(path)
        for n in range(100):outbox.put(dict(generated_at=n,accounts=[]))
        restarted=Outbox(path)
        self.assertEqual(restarted.get()['generated_at'],99)
        restarted.ack(1)
        self.assertIsNotNone(restarted.get())
        restarted.ack(99)
        self.assertIsNone(restarted.get())

    def test_request_token_limits_and_restart(self):
        path=self.base/'usage.sqlite3'
        ledger=UsageLedger(path,rpm=2,rpd=3,tpm=100,tpd=200)
        request=ledger.reserve(10000,'account_1','selection_generation',40)
        ledger.finish(request,200,{'total_tokens':10})
        ledger.reserve(10000,'account_2','selection_generation',40)
        with self.assertRaises(GroqBackoff):ledger.reserve(10001,'account_3','selection_generation',1)
        ledger.close();ledger=UsageLedger(path,rpm=2,rpd=3,tpm=100,tpd=200)
        try:
            with self.assertRaises(GroqBackoff):ledger.reserve(10001,'account_3','selection_generation',1)
            ledger.reserve(10061,'account_3','selection_generation',40)
            with self.assertRaises(GroqBackoff):ledger.reserve(10122,'account_4','selection_generation',1)
        finally:ledger.close()


class UpgradeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_workers_have_independent_budgets_and_twenty_five_combined(self):
        from tests.test_hourly_only import Service, TOPICS
        with tempfile.TemporaryDirectory() as directory:
            coordinator=Coordinator(Path(directory)/'coordinator.sqlite3',25)
            stores=[]
            workers=[]
            for slot in range(1,6):
                coordinator.register(f'account_{slot}',slot)
            try:
                for slot in range(1,6):
                    service=Service()
                    store=Store(Path(directory)/f'{slot}.sqlite3',f'account_{slot}',-100)
                    stores.append(store)
                    settings=Settings(1,'hash',-100,'key','model',account_id=f'account_{slot}',auto_send=True)
                    worker=Worker(settings,store,Identity(slot),service,service,clock=lambda:10000,coordinator=coordinator)
                    workers.append(worker)
                    # Four manual sends leave one automated slot independently.
                    for i in range(4):
                        worker.ingest(Message(slot*100+i,-100,slot,f'Manual account {slot} message {i}',9990,outgoing=True))
                    question,evidence,answer=TOPICS[slot-1]
                    context=Message(slot*100+10,-100,99,evidence,9980)
                    target=Message(slot*100+11,-100,98,question,10000)
                    service.messages={context.message_id:context,target.message_id:target}
                    service.answers[target.message_id]=answer
                    # Unique Telegram IDs across the fake shared group.
                    async def send(text,target_id,slot=slot,service=service):
                        service.sent.append((text,target_id,10000))
                        return Message(slot*100+50,-100,slot,text,10000,outgoing=True,reply_to_id=target_id)
                    service.send=send
                    await worker.process_once()
                    self.assertEqual(len(service.sent),1)
                    self.assertEqual(store.quota_remaining(10000),0)
                self.assertEqual(coordinator.quota_remaining(-100,10000),0)
                for worker in workers:
                    calls=worker.decider.calls
                    await worker.process_once()
                    self.assertEqual(worker.decider.calls,calls)
                self.assertEqual(coordinator.quota_remaining(-100,13600),25)
            finally:
                for store in stores:store.close()
                coordinator.close()

    async def test_personalities_and_usage_are_scoped_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator=Coordinator(Path(directory)/'coordinator.sqlite3',25)
            ledger=UsageLedger(Path(directory)/'coordinator.sqlite3',100,1000,1000000,10000000)
            prompts=[]
            def response(request):
                prompts.append(json.loads(request.content)['messages'][0]['content'])
                return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(dict(action='IGNORE',target_id=None,text=None,emoji=None,reason='Nothing useful'))}}], 'usage':{'total_tokens':25}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                shared=SharedGroqDecider('key','model',coordinator,http=http,clock=lambda:10000,ledger=ledger)
                try:
                    for n,persona in enumerate(PERSONAS,1):
                        settings=Settings(1,'hash',-100,'key','model',account_id=f'account_{n}',persona=persona)
                        decider=PersonaDecider(shared,settings)
                        message=Message(n,-100,10,'Why are transfers delayed today?',10000)
                        await decider.decide([message],[],[])
                        await decider.decide([message],[],[])
                    self.assertEqual(len(prompts),5)
                    self.assertTrue(all(persona in prompt for persona,prompt in zip(PERSONAS,prompts)))
                    self.assertEqual(ledger.db.execute('SELECT SUM(tokens) FROM groq_usage').fetchone()[0],125)
                finally:
                    await shared.aclose();ledger.close();coordinator.close()

    async def test_preview_and_live_starters_are_not_replies_and_cannot_repeat_after_restart(self):
        from tests.test_hourly_only import Service
        for live in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'account.sqlite3'
                settings=Settings(1,'hash',-100,'key','model',database_path=path,auto_send=live,
                                  discussion_topics=('What makes transfers reliable?',))
                service=Service()
                service.messages[1]=Message(1,-100,22,'Transfers suffer delays every morning',9000)
                store=Store(path,'account_1',-100)
                worker=Worker(settings,store,Identity(11),service,service,clock=lambda:10000)
                try:
                    await worker.process_once()
                    self.assertEqual(store.quota_remaining(10000),4)
                    self.assertEqual(service.calls,0)
                    self.assertEqual(len(service.sent),int(live))
                    if live:self.assertIsNone(service.sent[0][1])
                finally:store.close()
                store=Store(path,'account_1',-100)
                try:
                    worker=Worker(settings,store,Identity(11),service,service,clock=lambda:11000)
                    await worker.process_once()
                    self.assertEqual(store.quota_remaining(11000),4)
                    self.assertEqual(len(service.sent),int(live))
                finally:store.close()

    async def test_publisher_failed_upload_retains_queue_and_does_not_expose_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(TELEMETRY_SUPABASE_URL='https://test.supabase.co',TELEMETRY_SUPABASE_PUBLISHABLE_KEY='public',
                     TELEMETRY_WORKER_EMAIL='worker@example.test',TELEMETRY_WORKER_PASSWORD='private-password')
            app=ApplicationSettings((Settings(1,'hash',-100,'key','model'),),Path(directory)/'coordinator.sqlite3')
            payloads=[]
            def response(request):
                if '/auth/' in str(request.url):return httpx.Response(200,json={'access_token':'private-token','expires_in':3600})
                payloads.append(json.loads(request.content))
                return httpx.Response(503 if len(payloads)==1 else 200)
            with patch.dict(os.environ,env), tempfile.TemporaryDirectory():
                async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                    publisher=Publisher(app,http)
                    publisher.outbox.put(dict(generated_at=10000,accounts=[]))
                    with self.assertRaises(httpx.HTTPStatusError):await publisher.publish_once()
                    self.assertIsNotNone(publisher.outbox.get())
                    await publisher.publish_once()
                    self.assertIsNone(publisher.outbox.get())
                    self.assertNotIn('private-password',json.dumps(payloads))
                    self.assertNotIn('private-token',json.dumps(payloads))
