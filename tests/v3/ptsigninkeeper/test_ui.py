"""Regressions for bounded sign-in views and independent site/login state."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper import ui


class Host:
    def __init__(self, signs=None, logins=None):
        self._sign_sites=['all']
        self._login_sites=['all']
        self._generation=1
        self.data={'签到-2026-11-16':{'results':signs or {}},'登录-2026-11-16':{'results':logins or {}}}
        self.get_data=Mock(side_effect=lambda key:deepcopy(self.data.get(key)))
        self._PTSignInKeeper__custom_sites=lambda:[]


def site(id, active=True, name=None):
    return SimpleNamespace(id=id,name=name or f'站点{id}',url=f'https://example{id}.test/',is_active=active,public=False)


class SigninViewTests(unittest.TestCase):
    def test_visit_evidence_converts_utc_and_never_promises_keepalive(self):
        host=Host({'1':{'success':True,'activity':'2026-11-15T16:04:20.83786Z'},
                   '2':{'success':True,'activity':'2026-11-15 16:04:20'}})
        with patch.object(ui.settings,'TZ','Asia/Shanghai'):
            rows={row['id']:row for row in ui.list_sites(host)['items']}
        self.assertEqual(rows['1']['activity'],'2026-11-16 00:04:20')
        self.assertEqual(rows['1']['visit_label'],'已读取当日访问记录')
        self.assertEqual(rows['2']['visit_label'],'访问记录早于当日')

    def setUp(self):
        self.sites=[site(1),site(2),site(3,False)]
        self.today=patch.object(ui,'_today',return_value=date(2026,11,16))
        self.oper=patch.object(ui,'SiteOper',return_value=SimpleNamespace(list_order_by_pri=lambda:self.sites))
        self.today.start();self.oper.start()
        self.addCleanup(self.today.stop);self.addCleanup(self.oper.stop)

    def test_list_reads_only_two_daily_keys_and_caches_for_five_seconds(self):
        host=Host({'1':{'success':True,'bonus':'20 魔力'}})
        with patch.object(ui.time,'monotonic',return_value=1):
            first=ui.list_sites(host)
            ui.list_sites(host,query='站点',page=2)
            self.assertEqual(host.get_data.call_count,2)
        self.assertNotIn('history',json.dumps(first))
        self.assertIsNone(first['updated_at'])
        with patch.object(ui.time,'monotonic',return_value=7):
            ui.list_sites(host)
            self.assertEqual(host.get_data.call_count,4)

    def test_disabled_sites_are_separate_and_never_count_as_failures(self):
        host=Host({'1':{'success':True},'2':{'success':False},'3':{'success':False}})
        response=ui.list_sites(host)
        self.assertEqual(response['summary']['total_sites'],2)
        self.assertEqual(response['summary']['inactive'],1)
        self.assertEqual(response['summary']['failed'],1)
        self.assertEqual(len(response['items']),2)
        stopped=ui.list_sites(host,category='inactive')['items'][0]
        self.assertFalse(stopped['active'])
        self.assertEqual(stopped['signin_status'],{'label':'已停用','color':'grey'})
        self.assertEqual(len(ui.list_sites(host,include_inactive=True)['items']),3)

    def test_message_cannot_make_failed_signin_success_and_login_is_independent(self):
        host=Host({'1':{'success':False,'logged_in':True,'message':'未检测到签到成功，获得 10 魔力','bonus':'10'},
                   '2':{'success':True,'bonus':0}})
        response=ui.list_sites(host)
        rows={row['id']:row for row in response['items']}
        self.assertEqual(rows['1']['category'],'failed')
        self.assertEqual(rows['1']['login_status']['color'],'success')
        self.assertEqual(rows['1']['bonus'],'—')
        self.assertEqual(rows['2']['bonus'],'0')
        self.assertEqual(response['summary']['success'],1)
        self.assertEqual(response['summary']['rewards'],1)

    def test_metric_filters_match_summary_and_keep_zero_rewards(self):
        host = Host({'1': {'success': False, 'logged_in': True},
                     '2': {'success': True, 'bonus': 0}})
        logged = ui.list_sites(host, category='logged_in')
        rewarded = ui.list_sites(host, category='rewards')
        self.assertEqual(logged['total'], logged['summary']['logged_in'])
        self.assertEqual(rewarded['total'], rewarded['summary']['rewards'])
        self.assertEqual([row['id'] for row in rewarded['items']], ['2'])
        self.assertEqual(rewarded['items'][0]['bonus'], '0')

    def test_no_signin_and_unknown_reward_have_distinct_display(self):
        host=Host({'1':{'success':False,'logged_in':True,'signin_supported':False},
                   '2':{'success':True}})
        response=ui.list_sites(host)
        rows={row['id']:row for row in response['items']}
        self.assertEqual(rows['1']['category'],'no_sign')
        self.assertEqual(rows['2']['bonus'],'未获取')
        self.assertEqual(response['summary']['no_sign'],1)

    def test_missing_entry_keeps_separate_login_and_remains_actionable(self):
        host=Host({'1':{'success':False,'message':'签到入口 HTTP 404'}},
                  logins={'1':{'success':True}})
        row=ui.list_sites(host,category='failed')['items'][0]
        self.assertEqual(row['signin_status']['label'],'入口暂不可用')
        self.assertEqual(row['login_status']['label'],'登录成功')
        self.assertFalse(row['signin_success'])
        self.assertTrue(row['signin_selected'])

    def test_selection_respects_login_only_and_excludes_unselected_empty_sites(self):
        host=Host(logins={'2':{'success':True}})
        host._sign_sites=[];host._login_sites=[2]
        response=ui.list_sites(host)
        self.assertEqual(response['total'],1)
        self.assertEqual(response['items'][0]['signin_status']['label'],'未选择')
        self.assertEqual(response['summary']['success'],0)
        self.assertEqual(response['summary']['logged_in'],1)
        self.assertEqual(ui.list_sites(host,category='success')['total'],0)

    def test_legacy_mteam_account_check_is_login_only_in_list_and_history(self):
        self.sites[0].url='https://kp.m-team.cc/'
        original={'success':True,'message':'账号校验通过','bonus':20}
        host=Host({'1':original})
        result=ui.list_sites(host)
        row=next(row for row in result['items'] if row['id']=='1')
        self.assertEqual(row['category'],'no_sign')
        self.assertEqual(row['signin_status']['label'],'无签到功能')
        self.assertEqual(row['login_status']['label'],'登录成功')
        self.assertEqual(row['bonus'],'—')
        self.assertEqual((result['summary']['success'],result['summary']['rewards']),(0,0))
        receipt=ui.site_detail(host,'1')['history'][0]
        self.assertFalse(receipt['sign']['success'])
        self.assertFalse(receipt['sign']['signin_supported'])
        self.assertTrue(receipt['login']['success'])
        self.assertEqual(receipt['signin_status']['label'],'无签到功能')
        self.assertEqual(host.data['签到-2026-11-16']['results']['1'],original)

    def test_legacy_mteam_failure_remains_an_account_failure(self):
        self.sites[0].url='https://www.m-team.io/'
        host=Host({'1':{'success':False,'message':'API 密钥无效'}})
        row=ui.list_sites(host,category='failed')['items'][0]
        self.assertEqual(row['signin_status']['label'],'无签到功能')
        self.assertEqual(row['login_status']['label'],'登录失败')
        self.assertEqual(row['message'],'API 密钥无效')
        self.assertEqual(ui.list_sites(host,category='no_sign')['total'],0)

    def test_known_no_sign_cannot_override_explicit_failed_login_or_lookalike_domain(self):
        self.sites[0].url='https://api.m-team.io/'
        self.sites[1].url='https://m-team.io.example.test/'
        host=Host({'1':{'success':True,'message':'账号校验通过'},
                   '2':{'success':False,'message':'M-Team 站点无签到功能，账号校验通过'}},
                  {'1':{'success':False,'message':'会话失效'}})
        rows={row['id']:row for row in ui.list_sites(host)['items']}
        self.assertEqual(rows['1']['category'],'failed')
        self.assertEqual(rows['1']['message'],'会话失效')
        self.assertEqual(rows['2']['signin_status']['label'],'签到失败')

    def test_unconfirmed_success_does_not_count_completion_or_rewards(self):
        host=Host({'1':{'success':True,'unconfirmed':True,'bonus':25},
                   '2':{'success':False}})
        result=ui.list_sites(host,category='problem')
        self.assertEqual(result['total'],result['summary']['failed']+result['summary']['pending'])
        self.assertEqual(result['total'],2)
        self.assertEqual((result['summary']['success'],result['summary']['rewards']),(0,0))
        self.assertEqual(ui.list_sites(host,category='failed')['total'],1)

    def test_export_includes_all_filtered_rows_and_no_credentials(self):
        self.sites=[site(index) for index in range(1,122)]
        self.sites[-1].is_active=False
        signs={str(index):{'success':True,'bonus':0,'cookie':'do not export','token':'private'}
               for index in range(1,122)}
        host=Host(signs)
        exported=ui.export_rows(host,category='success')
        self.assertEqual(len(exported),120)
        self.assertEqual(exported[0]['本次奖励'],'0')
        self.assertNotIn('private',json.dumps(exported))
        self.assertNotIn('cookie',json.dumps(exported))
        self.assertEqual(len(ui.export_rows(host,include_inactive=True)),121)
        self.assertEqual(len(ui.export_rows(host,query='站点121')),0)
        self.assertEqual(len(ui.export_rows(host,query='站点121',include_inactive=True)),1)

    def test_historical_selection_returns_matching_detail_export_and_today_marker(self):
        host=Host({'1':{'success':False}})
        host.data['签到-2026-11-15']={'results':{'1':{'success':True,'bonus':'15'}}}
        result=ui.list_sites(host,day='2026-11-15')
        self.assertEqual((result['date'],result['today']),('2026-11-15','2026-11-16'))
        detail=ui.site_detail(host,'1',day=result['date'])
        self.assertEqual(detail['site']['bonus'],'15')
        self.assertEqual(detail['history'][0]['day'],'2026-11-15')
        self.assertEqual(ui.export_rows(host,day=result['date'],category='success')[0]['本次奖励'],'15')

    def test_config_date_and_active_changes_invalidate_cache_immediately(self):
        host=Host()
        with patch.object(ui.time,'monotonic',return_value=1):
            ui.list_sites(host)
            host._sign_sites=[1];host._login_sites=[]
            ui.list_sites(host)
            self.assertEqual(host.get_data.call_count,4)
            ui.list_sites(host,day='2026-11-15')
            self.assertEqual(host.get_data.call_count,6)
            self.sites[0].is_active=False
            ui.list_sites(host,day='2026-11-15')
            self.assertEqual(host.get_data.call_count,8)

    def test_page_bounds_search_and_reward_sort(self):
        host=Host({'1':{'success':True,'bonus':'1,000 魔力'},'2':{'success':True,'bonus':'5 魔力'}})
        response=ui.list_sites(host,sort='reward',page_size=1)
        self.assertEqual(response['items'][0]['id'],'1')
        self.assertEqual(response['pages'],2)
        response=ui.list_sites(host,query='站点2',page=999,page_size=10000)
        self.assertEqual((response['page'],response['page_size'],response['total']),(1,100,1))

    def test_custom_sites_use_ids_and_safe_links(self):
        host=Host()
        host._PTSignInKeeper__custom_sites=lambda:[{'id':'custom-1','name':'自定义',
            'url':'https://custom.test/?passkey=private','cookie':'secret'}]
        row=next(row for row in ui.list_sites(host)['items'] if row['id']=='custom-1')
        self.assertEqual(row['url'],'https://custom.test/')
        self.assertNotIn('secret',json.dumps(row))

    def test_detail_alone_loads_fourteen_days_and_whitelists_receipts(self):
        host=Host({'1':{'success':True,'bonus':'1','cookie':'never expose'}})
        host.data['签到-2026-11-15']={'results':{'1':{'success':False,'logged_in':True,'message':'稍后重试'}}}
        ui.list_sites(host)
        self.assertEqual(host.get_data.call_count,2)
        result=ui.site_detail(host,'1')
        self.assertTrue(result['found'])
        self.assertEqual(len(result['history']),2)
        self.assertEqual(host.get_data.call_count,28)
        self.assertNotIn('cookie',json.dumps(result))
        self.assertTrue(result['history'][1]['login']['success'])
        self.assertFalse(ui.site_detail(host,'missing')['found'])

    def test_deleted_site_history_can_be_opened_without_polluting_active_totals(self):
        host=Host({'99':{'success':False,'site_name':'旧站点'}})
        response=ui.list_sites(host)
        self.assertEqual(response['summary']['failed'],0)
        archived=ui.site_detail(host,'99')
        self.assertTrue(archived['found'])
        self.assertFalse(archived['site']['active'])
        self.assertEqual(archived['site']['name'],'旧站点')


if __name__=='__main__':
    unittest.main(verbosity=2)
