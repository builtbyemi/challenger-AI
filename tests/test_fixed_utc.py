from dataclasses import replace
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from challenger.config import Settings, load_application_settings
from challenger.coordinator import Coordinator
from challenger.models import Message
from challenger.reporting import account_snapshot
from challenger.storage import Store
from challenger.windows import window_start


class FixedUtcTests(unittest.TestCase):
    def test_fixed_boundary_ignores_reporting_timezone_including_fractional_offsets(self):
        for timezone in ('UTC','Africa/Lagos','Asia/Kathmandu','America/New_York'):
            self.assertEqual(window_start(3599.999,'fixed',timezone),0)
            self.assertEqual(window_start(3600,'fixed',timezone),3600)
            self.assertEqual(window_start(86399.999,'fixed',timezone),82800)
            self.assertEqual(window_start(86400,'fixed',timezone),86400)

    def test_application_defaults_to_fixed_without_changing_daily_report_timezone(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,dict(
                API_ID='1',API_HASH='fake',TARGET_CHAT_ID='-1',GROQ_API_KEY='fake',REPORT_TIMEZONE='Asia/Kathmandu'),clear=True):
            app=load_application_settings(Path(directory))
            self.assertEqual(app.accounts[0].window_mode,'fixed')
            self.assertEqual(app.accounts[0].timezone,'Asia/Kathmandu')

    def test_partial_and_full_hours_reset_all_five_and_group_with_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            group=Coordinator(base/'group.sqlite3',25,window_mode='fixed',timezone='Asia/Kathmandu')
            try:
                for slot in range(1,6):
                    account=f'account_{slot}'
                    group.register(account,slot)
                    store=Store(base/f'{slot}.sqlite3',account,-1,window_mode='fixed',timezone='Asia/Kathmandu')
                    try:
                        for i in range(4):
                            message=Message(slot*100+i,-1,slot,'Existing manual text',3599,outgoing=True)
                            store.observe_outgoing(message);group.observe_outgoing(account,message)
                        self.assertEqual(store.quota_remaining(3599.999),1)
                        self.assertEqual(store.quota_remaining(3600),5)
                        settings=Settings(1,'fake',-1,'fake','model',account_id=account,database_path=base/f'{slot}.sqlite3',window_mode='fixed')
                        before=account_snapshot(settings,3599)
                        self.assertEqual((before['total'],before['resets_at']),(4,3600))
                        self.assertEqual(account_snapshot(settings,3600)['total'],0)
                    finally:store.close()
                self.assertEqual(group.quota_remaining(-1,3599.999),5)
                self.assertEqual(group.quota_remaining(-1,3600),25)
                for slot in range(1,6):
                    account=f'account_{slot}'
                    store=Store(base/f'{slot}.sqlite3',account,-1,window_mode='fixed')
                    try:
                        for i in range(5):
                            message=Message(slot*100+i+10,-1,slot,'Existing manual text',3600,outgoing=True)
                            store.observe_outgoing(message);group.observe_outgoing(account,message)
                        self.assertEqual(store.quota_remaining(7199),0)
                        self.assertEqual(store.quota_wait(7199),1)
                        self.assertIsNone(store.reserve_start('No quota left','live',7199))
                        self.assertEqual(store.quota_remaining(7200),5)
                    finally:store.close()
                self.assertEqual(group.quota_remaining(-1,7199),0)
                self.assertEqual(group.quota_wait(-1,7199),1)
                self.assertIsNone(group.reserve('account_1',-1,9999,'No quota left','live',7199))
            finally:group.close()
            group=Coordinator(base/'group.sqlite3',25,window_mode='fixed')
            try:
                self.assertEqual(group.quota_remaining(-1,7199),0)
                self.assertEqual(group.quota_remaining(-1,7200),25)
            finally:group.close()

    def test_preview_allowance_and_floodwait_survive_hour_reset(self):
        store=Store(':memory:','account_1',-1,window_mode='fixed')
        try:
            for i in range(5):
                message=Message(i+1,-1,99,'A suitable question',3599)
                store.enqueue(message)
                action=store.reserve(message,f'Relevant response number {i}','preview',3599)
                self.assertIsNotNone(action)
                store.finish(action,'previewed',3599)
            store.set_block('telegram',4000)
            self.assertEqual(store.quota_remaining(3599),0)
            self.assertEqual(store.quota_remaining(3600),5)
            self.assertEqual(store.blocked_until('telegram'),4000)
        finally:store.close()
