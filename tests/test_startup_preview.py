"""No real Telegram connections, credentials, sessions or group sends."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main
from challenger.application import run_application, validate_live_readiness
from challenger.config import load_application_settings
from challenger.groq_client import GroqConfigurationError
from challenger.models import Identity
from challenger.startup import InstanceAlreadyRunning, failure_summary
from challenger.telegram_app import TelethonGateway, instance_lock


class StartupPreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base=Path(self.directory.name)
        self.base.joinpath('.env').write_text(
            'API_ID=123\nAPI_HASH=synthetic-hash\nTARGET_CHAT_ID=-123\nGROQ_API_KEY=synthetic-key\n'
            'AUTO_SEND=true\nGROUP_AUTOMATION_ALLOWED=false\nAUTOMATION_DISCLOSED=false\n'
            + ''.join(f'ACCOUNT_{i}_ENABLED=true\n' for i in range(1,6)),encoding='utf-8')
        env=patch.dict(os.environ,{},clear=True)
        env.start()
        self.addCleanup(env.stop)

    def load(self, _, **kwargs):
        return load_application_settings(self.base, **kwargs)

    def test_environment_precedence_and_preview_check_config_report_effective_mode(self):
        for inherited in (None,'true','false'):
            with self.subTest(inherited=inherited), patch.dict(os.environ,{} if inherited is None else {'AUTO_SEND':inherited},clear=True):
                configured=self.load(None)
                self.assertEqual(all(a.auto_send for a in configured.accounts),inherited!='false')
                with patch.object(main,'load_application_settings',side_effect=self.load), \
                     patch('sys.argv',['main.py','--check-config','--preview']),redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main.main(),0)
                self.assertEqual(output.getvalue().count('AUTO_SEND=False'),5)

    def test_real_cli_initializes_all_five_workers_in_preview_before_live_validation(self):
        seen=[]
        async def identify(account,interactive=False):
            self.assertFalse(account.auto_send)
            return Identity(int(account.account_id[-1]))
        async def worker(account,**kwargs):
            self.assertFalse(account.auto_send)
            seen.append(account.account_id)
            raise GroqConfigurationError()  # Exit each synthetic supervisor cleanly.
        with patch.object(main,'load_application_settings',side_effect=self.load), \
             patch('sys.argv',['main.py','--preview']), \
             patch('challenger.application.import_history',return_value=0), \
             patch('challenger.application.authenticate',side_effect=identify), \
             patch('challenger.application.SharedGroqDecider',return_value=SimpleNamespace(aclose=AsyncMock())), \
             patch('challenger.application.run_account',side_effect=worker), \
             self.assertLogs('challenger.application',level='INFO') as logs:
            self.assertEqual(main.main(),0)
        self.assertEqual(set(seen),{f'account_{i}' for i in range(1,6)})
        self.assertEqual(sum('mode=PREVIEW' in line for line in logs.output),5)
        self.assertTrue(all(a.auto_send for a in self.load(None).accounts))

    def test_live_gates_remain_required_and_no_worker_starts(self):
        with patch.object(main,'load_application_settings',side_effect=self.load), \
             patch('sys.argv',['main.py']),patch('challenger.application.authenticate',new=AsyncMock()) as auth, \
             self.assertLogs(level='ERROR') as logs:
            self.assertEqual(main.main(),2)
        auth.assert_not_called()
        self.assertIn('stage=live_validation',' '.join(logs.output))
        self.assertIn('GROUP_AUTOMATION_ALLOWED',' '.join(logs.output))
        app=self.load(None)
        with self.assertRaises(ValueError):validate_live_readiness(replace(app,group_automation_allowed=True))

    def test_locked_coordinator_fails_with_safe_actionable_message_before_db_or_network(self):
        app=self.load(None)
        with instance_lock(app.coordinator_path.with_suffix('.coordinator.lock')), \
             patch.object(main,'load_application_settings',return_value=app), \
             patch('sys.argv',['main.py','--preview']), \
             patch('challenger.application.Coordinator') as database, \
             patch('challenger.application.authenticate',new=AsyncMock()) as auth, self.assertLogs(level='ERROR') as logs:
            self.assertEqual(main.main(),1)
        database.assert_not_called()
        auth.assert_not_called()
        text=' '.join(logs.output)
        self.assertIn('stage=coordinator_lock',text)
        self.assertIn('Ctrl+C',text)
        self.assertNotIn('synthetic-hash',text)
        with instance_lock(app.coordinator_path.with_suffix('.coordinator.lock')):pass

    def test_failed_partial_initialization_closes_coordinator_and_releases_lock(self):
        app=self.load(None)
        database=MagicMock()
        with patch.object(main,'load_application_settings',return_value=app), \
             patch('sys.argv',['main.py','--preview']), \
             patch('challenger.application.Coordinator',return_value=database), \
             patch('challenger.application.UsageLedger',side_effect=RuntimeError('private-key private-message')), \
             self.assertLogs(level='ERROR') as logs:
            self.assertEqual(main.main(),1)
        database.close.assert_called_once()
        text=' '.join(logs.output)
        self.assertIn('stage=usage_ledger',text)
        self.assertIn('source=application.py:',text)
        self.assertNotIn('private-',text)
        with instance_lock(app.coordinator_path.with_suffix('.coordinator.lock')):pass


class PreviewAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_refuses_replies_starters_and_repeated_attempts_before_telethon_call(self):
        from challenger.config import Settings
        client=SimpleNamespace(send_message=AsyncMock())
        gateway=TelethonGateway(client,Settings(1,'fake',-1,'fake','model',auto_send=False),Identity(1))
        for target in (42,None,42,None):
            with self.assertRaisesRegex(RuntimeError,'PREVIEW'):
                await gateway.send('Short relevant contribution',target)
        client.send_message.assert_not_called()


if __name__=='__main__':unittest.main()
