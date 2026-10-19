import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from challenger.config import Settings
from challenger.coordinator import Coordinator
from challenger.discussions import discussion_context, validate_starter
from challenger.engine import Worker
from challenger.groq_client import GroqDecider, PersonaDecider
from challenger.models import Identity, Message
from challenger.reporting import account_snapshot
from challenger.storage import Store


def decision(text='Which Bitcoin risks worry you?'):
    return dict(action='START_DISCUSSION',target_id=None,text=text,emoji=None,reason='Contextual question')


class QualificationTests(unittest.TestCase):
    def test_qualified_is_independent_of_sending_limit_and_recomputed_from_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'history.sqlite3'
            settings=Settings(1,'fake',-1,'fake','model',database_path=path)
            store=Store(path,'account_1',-1)
            try:
                for i,text in enumerate(['Only three words','Four useful words here','Omo this one strong 😂',"That's quite a surprise",'Glad to hear']):
                    store.observe_outgoing(Message(i+1,-1,1,text,4000,outgoing=True))
                self.assertEqual(store.quota_remaining(4680),0)
                report=account_snapshot(settings,4680)
                self.assertEqual((report['qualified'],report['total'],report['remaining']),(3,5,0))
                self.assertEqual(report['qualified_remaining'],2)
                self.assertNotIn('Four useful words',json.dumps(report))
            finally:store.close()
            report=account_snapshot(settings,4680)
            self.assertEqual(report['qualified'],3)
            report=account_snapshot(settings,7200)
            self.assertEqual((report['qualified'],report['total'],report['remaining']),(0,0,5))
            self.assertEqual(len(report['events']),5)

    def test_previews_and_uncertain_sends_do_not_qualify(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(1,'fake',-1,'fake','model',database_path=Path(directory)/'history.sqlite3')
            store=Store(settings.database_path,'account_1',-1)
            try:
                for i,status in enumerate(['previewed','uncertain']):
                    msg=Message(i+1,-1,99,'A useful source question',4000)
                    store.enqueue(msg)
                    action=store.reserve(msg,'Four useful words here','preview' if i==0 else 'live',4000)
                    store.finish(action,status,4000)
                report=account_snapshot(settings,4680)
                self.assertEqual((report['qualified'],report['total'],report['remaining']),(0,0,3))
            finally:store.close()


class StarterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.settings=Settings(1,'fake',-1,'fake','model',database_path=self.base/'account.sqlite3')
        self.store=Store(self.settings.database_path,'account_1',-1)
        self.addCleanup(self.store.close)
        self.human=Message(100,-1,99,'Bitcoin risks need more discussion',8000)
        self.store.enqueue(self.human)
        self.decider=SimpleNamespace(start_discussion=AsyncMock(return_value=decision()))
        self.gateway=SimpleNamespace(sync=AsyncMock(return_value=[self.human]),send=AsyncMock())
        self.worker=Worker(self.settings,self.store,Identity(1),self.decider,self.gateway,clock=lambda:10000)

    async def test_fresh_ai_question_preview_once_and_persistent_attempt(self):
        await self.worker.process_once()
        self.decider.start_discussion.assert_awaited_once()
        self.gateway.send.assert_not_called()
        self.assertEqual(self.store.quota_remaining(10000),4)
        self.assertEqual(self.store.export_actions(10000)[0]['kind'],'START_DISCUSSION')
        await self.worker.process_once()
        self.decider.start_discussion.assert_awaited_once()
        self.store.close()
        self.store=Store(self.settings.database_path,'account_1',-1)
        self.addCleanup(self.store.close)
        self.worker.store=self.store
        await self.worker.process_once()
        self.decider.start_discussion.assert_awaited_once()

    async def test_human_arrival_during_generation_discards_starter_and_keeps_message(self):
        arriving=Message(101,-1,98,'Bitcoin discussion continues here',10000)
        async def generate(*args):
            self.worker.ingest(arriving)
            self.gateway.sync.return_value=[self.human,arriving]
            return decision()
        self.decider.start_discussion.side_effect=generate
        await self.worker.start_discussion()
        self.assertEqual(self.store.quota_remaining(10000),5)
        self.assertIsNotNone(self.store.get(101))
        self.gateway.send.assert_not_called()

    async def test_ignore_does_not_regenerate_for_unchanged_quiet_context(self):
        self.decider.start_discussion.return_value=dict(action='IGNORE',target_id=None,text=None,emoji=None,reason='Nothing useful')
        await self.worker.start_discussion()
        await self.worker.start_discussion()
        self.decider.start_discussion.assert_awaited_once()
        self.assertEqual(self.store.quota_remaining(10000),5)

    async def test_full_account_never_generates_while_other_account_can(self):
        for i in range(5):self.store.observe_outgoing(Message(i+1,-1,1,'Existing short text',9900,outgoing=True))
        await self.worker.start_discussion()
        self.decider.start_discussion.assert_not_called()
        self.assertEqual(self.worker.delay(),800)
        other=Store(self.base/'other.sqlite3','account_2',-1)
        try:
            other.enqueue(self.human)
            worker=Worker(self.settings,other,Identity(2),self.decider,self.gateway,clock=lambda:10000)
            await worker.start_discussion()
            self.decider.start_discussion.assert_awaited_once()
        finally:other.close()

    async def test_group_claim_prevents_controlled_back_and_forth(self):
        group=Coordinator(self.base/'group.sqlite3',25)
        group.register('account_1',1);group.register('account_2',2)
        other=Store(self.base/'other.sqlite3','account_2',-1)
        try:
            self.worker.coordinator=group
            await self.worker.start_discussion()
            other.enqueue(self.human)
            decider=SimpleNamespace(start_discussion=AsyncMock(return_value=decision('What Bitcoin risks matter most?')))
            worker=Worker(self.settings,other,Identity(2),decider,self.gateway,clock=lambda:12000,coordinator=group)
            await worker.start_discussion()
            decider.start_discussion.assert_not_called()
        finally:other.close();group.close()

    def test_context_and_validator_reject_unrelated_fake_news_private_or_repeated_questions(self):
        self.assertEqual(validate_starter(decision(),[self.human],[]).action,'START_DISCUSSION')
        for text in ['Why did Bitcoin crash today?','Which Bitcoin price reaches 99999?','Which Trump Bitcoin risks matter?','Glad to hear that']:
            self.assertEqual(validate_starter(decision(text),[self.human],[]).action,'IGNORE')
        self.assertEqual(validate_starter(decision(),[self.human],[decision()['text']]).action,'IGNORE')
        self.assertEqual(discussion_context(self.settings,[self.human],[],{99},10000),[])
        private=Message(101,-1,99,'Bitcoin risks need more discussion',8000,reply_to_id=77)
        self.assertEqual(discussion_context(self.settings,[private],[],set(),10000),[])

    async def test_groq_starter_has_no_fake_candidate_and_uses_structured_output(self):
        seen=[]
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(decision())}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            delegate=GroqDecider('fake','openai/gpt-oss-20b',http)
            result=await PersonaDecider(delegate,self.settings).start_discussion([self.human],['Previous question'])
        self.assertEqual(result['action'],'START_DISCUSSION')
        payload=json.loads(seen[0]['messages'][1]['content'])
        self.assertEqual(payload['CANDIDATES'],[])
        self.assertEqual(payload['CHAT_CONTEXT'][0]['id'],100)
        self.assertEqual(payload['RECENT_REPLIES'],['Previous question'])
        self.assertIn('START_DISCUSSION',seen[0]['response_format']['json_schema']['schema']['properties']['action']['enum'])
