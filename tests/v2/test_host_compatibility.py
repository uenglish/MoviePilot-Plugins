"""V2 host boundaries: module discovery, notifications and configured AI."""
import asyncio
import importlib
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from app.core.config import settings
from app.core.plugin import PluginManager
from app.plugins import _PluginBase as HostPluginBase
from app.plugins.ptsigninkeeper import PTSignInKeeper
from app.plugins.ptinvitekeeper import PTInviteKeeper
from app.plugins.ptsigninkeeper.host_ai import LLMHelper
from app.plugins.ptsigninkeeper.host_compat import NotificationBlock, NotificationContent
from app.plugins.ptsigninkeeper.ptengine.http import _LegacySdkSession, new_http_session
from app.plugins.ptsigninkeeper.ptengine.quiz import AssistantSolver
from app.scheduler import Scheduler


class HostCompatibilityTests(unittest.TestCase):
    def test_v2_host_retains_both_random_firings_and_replaces_them_on_reload(self):
        pid = 'PTSignInKeeper'
        tz = ZoneInfo('Asia/Shanghai')
        today = datetime(2026, 11, 27, tzinfo=tz)
        first, second = today.replace(hour=11, minute=13), today.replace(hour=16, minute=47)
        plugin = object.__new__(PTSignInKeeper)
        plugin._enabled, plugin._sign_sites, plugin._cron = True, ['all'], '  '
        host = object.__new__(Scheduler)
        host._jobs, host._lock = {}, RLock()
        host._scheduler = BackgroundScheduler(timezone=tz)
        host._scheduler.start(paused=True)
        self.addCleanup(host._scheduler.shutdown)
        with patch.dict(PluginManager()._running_plugins, {pid: plugin}), \
             patch.object(settings, 'TZ', 'Asia/Shanghai'), \
             patch('app.plugins.ptsigninkeeper.TimerUtils.random_scheduler',
                   return_value=[first, second]) as random_times:
            for _ in range(2):
                host.update_plugin_job(pid)
                jobs = host._scheduler.get_jobs()
                self.assertEqual(len(jobs), 1)
                trigger = jobs[0].trigger
                self.assertEqual(trigger.get_next_fire_time(None, today), first)
                self.assertEqual(trigger.get_next_fire_time(first, first), second)
                self.assertEqual(trigger.get_next_fire_time(second, second), first + timedelta(days=1))
            random_times.assert_called_once()
            plugin._enabled = False
            host.update_plugin_job(pid)
            self.assertEqual(host._scheduler.get_jobs(), [])

    def test_every_site_module_imports_on_real_v2(self):
        for name in ('ptsigninkeeper', 'ptinvitekeeper'):
            plugin = importlib.import_module('app.plugins.' + name)
            root = Path(plugin.__file__).parent
            for package in ('sites', 'standard_sites', 'ptengine/sites'):
                for file in sorted((root / package).glob('*.py')):
                    suffix = '.'.join(file.relative_to(root).with_suffix('').parts)
                    with self.subTest(module=name + '.' + suffix):
                        importlib.import_module('app.plugins.' + name + '.' + suffix)

    def test_both_plugins_use_the_native_v2_base(self):
        for cls in (PTSignInKeeper, PTInviteKeeper):
            self.assertTrue(issubclass(cls, HostPluginBase))
            self.assertEqual(cls.get_render_mode(), ('vue', 'dist/assets'))
            nav = cls.get_sidebar_nav(None)
            self.assertEqual(len(nav), 1)
            self.assertEqual(nav[0]['section'], 'organize')

    def test_v2_http_sessions_use_the_legacy_request_adapter(self):
        self.assertIsInstance(new_http_session(), _LegacySdkSession)

    def test_v2_notification_preserves_plain_text_and_drops_unsupported_content(self):
        content = NotificationContent(title='签到结果', blocks=[
            NotificationBlock(title='失败 1', text='测试站：请求超时'),
            NotificationBlock(title='成功 2', text='已返回奖励 2 站')])
        plugin = object.__new__(PTSignInKeeper)
        with patch.object(HostPluginBase, 'post_message') as send:
            plugin.post_message(title='签到结果', text='已有正文', rich_content=content)
            self.assertEqual(send.call_args.kwargs, {'title': '签到结果', 'text': '已有正文'})
            plugin.post_message(rich_content=content)
            self.assertEqual(send.call_args.kwargs['text'], '失败 1\n测试站：请求超时\n\n成功 2\n已返回奖励 2 站')

    def test_configured_v2_model_uses_host_provider_metadata(self):
        solver = AssistantSolver(True)
        runtime = Mock()
        runtime.get_auth_status.return_value = {}
        with patch.object(settings, 'LLM_PROVIDER', 'test-provider'), \
             patch.object(settings, 'LLM_MODEL', 'test-model'), \
             patch.object(settings, 'LLM_API_KEY', 'test-only-not-a-key'), \
             patch('app.plugins.ptsigninkeeper.host_ai.resolve_llm_provider_runtime', return_value=runtime):
            runtime.resolve_cached_model_metadata.return_value = {'modalities': {'input': ['text']}}
            self.assertTrue(solver.status()['available'])
            self.assertFalse(solver.status()['image'])
            runtime.resolve_cached_model_metadata.return_value = {'modalities': {'input': ['text', 'image']}}
            self.assertTrue(solver.status()['image'])
            self.assertNotIn('test-only-not-a-key', str(solver.status()))

    def test_v2_without_ai_remains_unavailable(self):
        with patch.object(settings, 'LLM_MODEL', ''):
            self.assertFalse(AssistantSolver(True).status()['available'])
        self.assertFalse(AssistantSolver(False).status()['available'])

    def test_invitation_notification_uses_its_own_compatibility_module(self):
        from app.plugins.ptinvitekeeper.notification import build_refresh_content, notification_plain_text

        content = build_refresh_content({'本站': {'data': {'invite_status': {
            'can_invite': True, 'permanent_count': 2, 'temporary_count': 1}}}}, 1, 0)
        self.assertEqual(type(content).__module__, 'app.plugins.ptinvitekeeper.host_compat')
        plugin = object.__new__(PTInviteKeeper)
        with patch.object(HostPluginBase, 'post_message') as send:
            plugin.post_message(title=content.title, text=notification_plain_text(content), rich_content=content)
        self.assertNotIn('rich_content', send.call_args.kwargs)
        self.assertEqual(send.call_args.kwargs['text'], notification_plain_text(content))
        self.assertIn('永久 2 · 临时 1', send.call_args.kwargs['text'])

    def test_v2_ai_request_reads_response_and_releases_its_client(self):
        transport = SimpleNamespace(aclose=AsyncMock())
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='test-answer')),
                                http_async_client=transport)
        with patch.object(LLMHelper, 'get_llm', new=AsyncMock(return_value=model)) as create:
            reply = asyncio.run(AssistantSolver(True)._ask('题目', {'a': '选项'}, False, None))
        self.assertEqual(reply, 'test-answer')
        self.assertEqual(create.call_args.kwargs['web_search_mode'], 'disabled')
        transport.aclose.assert_awaited_once()

    def test_ai_cleanup_closes_async_and_sync_clients_once(self):
        async_client = SimpleNamespace(aclose=AsyncMock())
        sync_client = SimpleNamespace(close=Mock())
        model = SimpleNamespace(root_async_client=async_client, http_async_client=async_client,
                                root_client=sync_client, http_client=sync_client)
        asyncio.run(LLMHelper.close_llm(model))
        async_client.aclose.assert_awaited_once()
        sync_client.close.assert_called_once()

    def test_ai_cleanup_failure_does_not_discard_an_answer(self):
        failed = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError('secret-provider-error')))
        remaining = SimpleNamespace(close=Mock())
        asyncio.run(LLMHelper.close_llm(SimpleNamespace(root_async_client=failed, root_client=remaining)))
        remaining.close.assert_called_once()
