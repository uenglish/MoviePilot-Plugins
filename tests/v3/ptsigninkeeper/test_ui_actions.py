"""Explicit UI actions reuse scoped runners; data reads never launch work."""
from threading import Lock
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from app.plugins.ptsigninkeeper import PTSignInKeeper, ui_actions

class UIActionTests(TestCase):
    def host(self):
        return SimpleNamespace(get_state=lambda: True, _run_lock=Lock(), _generation=1,
            _sign_sites=[1, 2], _login_sites=[1, 2], _resolve_site_ids=lambda values: values,
            _PTSignInKeeper__do=Mock(return_value={}), _schedule_short_retry=Mock())

    def test_invalid_and_disabled_actions_never_spawn_threads(self):
        host=self.host()
        with patch.object(ui_actions, 'Thread') as thread:
            self.assertFalse(ui_actions.start_run(host, 'signin', scope='invalid')['success'])
            host.get_state=lambda: False
            self.assertFalse(ui_actions.start_run(host, 'signin')['success'])
            thread.assert_not_called()

    def test_pending_scope_retries_only_failure_and_preserves_login_reuse(self):
        host=self.host()
        rows=[{'id':'1','category':'success','login_status':{'color':'success'}},
              {'id':'2','category':'failed','login_status':{'color':'success'}}]
        with patch('app.plugins.ptsigninkeeper.ui.list_sites', return_value={'items':rows}), \
             patch.object(ui_actions, 'Thread') as thread:
            self.assertTrue(ui_actions.start_run(host,'signin')['success'])
            self.assertTrue(host._ui_task['running'])
            thread.call_args.kwargs['target']()
        calls=host._PTSignInKeeper__do.call_args_list
        self.assertEqual(calls[0].args[1:], ('签到',[2]))
        self.assertEqual(calls[1].kwargs, {'signin_results':{}})
        self.assertFalse(host._ui_task['running'])
        self.assertFalse(host._run_lock.locked())

    def test_existing_runner_and_lifecycle_change_prevent_second_execution(self):
        host=self.host();host._run_lock.acquire()
        with patch('app.plugins.ptsigninkeeper.ui.list_sites', return_value={'items':[{'id':'1','category':'failed'}]}), \
             patch.object(ui_actions, 'Thread') as thread:
            ui_actions.start_run(host,'signin');thread.assert_not_called()
            host._run_lock.release()
            ui_actions.start_run(host,'signin');host._generation+=1
            thread.call_args.kwargs['target']()
            host._PTSignInKeeper__do.assert_not_called()

    def test_vue_contract_and_ui_routes_are_authenticated(self):
        host=object.__new__(PTSignInKeeper)
        host.get_config=Mock(return_value={'secret_extension':'kept','cron':'1 0 * * *'})
        self.assertEqual(host.get_render_mode(), ('vue','dist/assets'))
        self.assertEqual(host.get_page(), [])
        self.assertEqual(host.get_form()[1]['secret_extension'],'kept')
        for route in host.get_api():
            if route['path'].startswith('/ui/'):
                self.assertEqual(route['auth'],'bear')
