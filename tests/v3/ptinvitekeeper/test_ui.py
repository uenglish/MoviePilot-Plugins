"""Dashboard regressions: real data shapes, lazy members and local cache invalidation."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.plugins.ptinvitekeeper import ui


def fixture(records=None, history=None):
    records = records or {}
    return SimpleNamespace(data_manager=SimpleNamespace(
        get_site_data=lambda: deepcopy(records), load_history=lambda: deepcopy(history or [])),
        sites=SimpleNamespace(get_indexers=lambda: [{'name': name} for name in reversed(records)]),
        _refreshing=False)


def record(quota=2, members=None, reason='', can_invite=True, updated=1000, **extra):
    return {'last_update': updated, 'data': {
        'invite_status': {'can_invite': can_invite, 'reason': reason, 'permanent_count': quota,
                          'temporary_count': 0, 'bonus': 0, **extra},
        'invitees': members or [],
    }}


class PaginatedViewTests(unittest.TestCase):
    def test_native_icon_identity_uses_site_management_id_without_connection_secrets(self):
        host = fixture({'本站': record(), '旧记录': record()})
        host.sites.get_indexers = lambda: [{'name':'本站','id':12,'url':'https://x.test/?token=secret-url','cookie':'secret-cookie'}]
        rows = {row['name']: row for row in ui.list_sites(host)['items']}
        self.assertEqual(rows['本站']['site_id'], '12')
        self.assertIsNone(rows['旧记录']['site_id'])
        self.assertNotIn('secret-', json.dumps(rows))

    def test_default_page_is_bounded_and_members_are_not_eager(self):
        members=[{'username':f'private-member-{i}', 'email':'private@example.test', 'cookie':'secret'} for i in range(400)]
        host=fixture({f'Site {i:03d}':record(members=members if i==0 else []) for i in range(63)})
        response=ui.list_sites(host, now=1001)
        self.assertEqual(response['total'],63)
        self.assertEqual(response['pages'],4)
        self.assertEqual(len(response['items']),20)
        self.assertEqual(response['summary']['members'],400)
        text=json.dumps(response)
        self.assertNotIn('private-member-',text)
        self.assertNotIn('private@example.test',text)
        self.assertLess(len(text),26000)

    def test_search_filter_and_sort_keep_global_totals(self):
        host=fixture({'Alpha':record(quota=2),'Beta':record(can_invite=False,reason='Cookie 已失效'),
                      'Charlie':record(quota=9)})
        response=ui.list_sites(host,category='problem',query='COOKIE',now=1001)
        self.assertEqual(response['total'],1)
        self.assertEqual(response['items'][0]['name'],'Beta')
        self.assertEqual(response['summary']['sites'],3)
        self.assertEqual(response['summary']['problem'],1)
        self.assertEqual(ui.list_sites(host,sort='quota')['items'][0]['name'],'Charlie')

    def test_page_controls_clamp_after_filter_changes(self):
        host=fixture({'Alpha':record()})
        response=ui.list_sites(host,page=999,page_size=10000)
        self.assertEqual((response['page'],response['page_size'],response['pages']),(1,100,1))
        response=ui.list_sites(host,page='bad',page_size='bad',query='no result')
        self.assertEqual((response['page'],response['page_size'],response['total']),(1,20,0))

    def test_zero_bonus_and_unavailable_quota_are_distinct(self):
        host=fixture({'known':record(),'unknown':{'data':{'invite_status':{'reason':'站点服务异常'}}}})
        rows={row['name']:row for row in ui.list_sites(host)['items']}
        self.assertEqual(rows['known']['bonus'],0)
        self.assertIsNone(rows['unknown']['bonus'])
        self.assertIsNone(rows['unknown']['quota'])

    def test_mteam_reported_balance_and_purchase_count_remain_visible(self):
        host=fixture({'Test':{'data':{'invite_status':{
            'can_invite':True,'reason':'用户等级(User)魔力值(180000.5)可购买2个临时邀请',
            'permanent_count':1,'temporary_count':0}}}})
        row=ui.list_sites(host)['items'][0]
        self.assertEqual(row['bonus'],180000.5)
        self.assertEqual(row['reported_purchasable_temporary'],2)
        self.assertIsNone(row['temporary_price'])
        self.assertIsNone(row['purchasable_temporary'])

    def test_staleness_uses_each_record_and_preserves_fetch_failure(self):
        host=fixture({'fresh':record(updated=100000),'old':record(updated=1)})
        host.data_manager.get_site_data=lambda: {'fresh':record(updated=100000),
            'old':{'last_update':1,'data':{'invite_status':{'reason':'HTTP 500'},'fetch_failed':True}}}
        result=ui.list_sites(host,category='stale',now=100010)
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['name'],'old')
        self.assertTrue(result['items'][0]['cached_after_failure'])


class LazyDetailsTests(unittest.TestCase):
    def test_member_page_search_and_health_are_bounded(self):
        members=[{'username':f'member{i:03d}','ratio':'0.5' if i%2 else '2','enabled':'no' if i==0 else 'yes'} for i in range(55)]
        host=fixture({'Test':record(members=members)})
        response=ui.site_detail(host,'Test',page=2,page_size=10)
        self.assertEqual((response['total'],len(response['items'])),(55,10))
        self.assertEqual(ui.site_detail(host,'Test',member_status='banned')['total'],1)
        self.assertEqual(ui.site_detail(host,'Test',member_query='member054')['total'],1)
        self.assertFalse(ui.site_detail(host,'missing')['found'])

    def test_member_whitelist_and_profile_links_do_not_expose_secrets(self):
        members=[{'username':'safe','ratio':'∞','cookie':'secret-cookie','apikey':'secret-api',
                  'profile_url':'https://example.test/userdetails.php?id=2&passkey=secret-url'},
                 {'username':'unsafe','profile_url':'javascript:alert(1)'}]
        result=ui.site_detail(fixture({'Test':record(members=members)}),'Test')
        text=json.dumps(result)
        self.assertNotIn('secret-',text)
        safe=next(row for row in result['items'] if row['username']=='safe')
        self.assertEqual(safe['profile_url'],'https://example.test/userdetails.php?id=2')
        self.assertEqual(safe['ratio'],'∞')
        self.assertEqual(next(row for row in result['items'] if row['username']=='unsafe')['profile_url'],'')

    def test_global_and_site_trend_are_compact_and_source_is_unchanged(self):
        history=[{'time':1,'sites':{'Test':{'category':'cookie','permanent':0,'temporary':0,'invitees':2,'secret':'no'}}},
                 {'time':2,'sites':{'Test':{'category':'invitable','permanent':3,'temporary':1,'invitees':4,'secret':'no'}}}]
        original=deepcopy(history)
        host=fixture({'Test':record()},history)
        result=ui.list_sites(host)
        self.assertEqual(result['trend']['points'][-1]['quota'],4)
        self.assertEqual(len(result['trend']['changes']['recovered']),1)
        self.assertEqual(ui.site_detail(host,'Test')['history'][-1]['members'],4)
        self.assertEqual(history,original)
        self.assertNotIn('secret',json.dumps(result))


class FileCacheTests(unittest.TestCase):
    def test_snapshot_cache_invalidates_after_file_change(self):
        with TemporaryDirectory() as root:
            data=Path(root)/'site_data.json';history=Path(root)/'history.json'
            data.write_text(json.dumps({'Test':record(quota=2)}));history.write_text('[]')
            host=SimpleNamespace(data_manager=SimpleNamespace(data_file=data,history_file=history))
            with patch.object(ui,'_row',wraps=ui._row) as flatten:
                self.assertEqual(ui.list_sites(host)['summary']['quota'],2)
                self.assertEqual(ui.list_sites(host)['summary']['quota'],2)
                self.assertEqual(flatten.call_count,1)
                data.write_text(json.dumps({'Test':record(quota=300)}))
                self.assertEqual(ui.list_sites(host)['summary']['quota'],300)
                self.assertEqual(flatten.call_count,2)
                data.write_text('{ partial')
                self.assertEqual(ui.list_sites(host)['summary']['quota'],300)
                data.write_text(json.dumps({'Test':record(quota=9)}))
                self.assertEqual(ui.list_sites(host)['summary']['quota'],9)

    def test_exports_use_current_filter_without_member_details(self):
        host=fixture({'Alpha':record(),'Beta':record(can_invite=False,reason='等级不足')})
        exported=ui.export_rows(host,category='invitable')
        self.assertEqual(len(exported),1)
        self.assertEqual(exported[0]['站点'],'Alpha')
        self.assertNotIn('email',json.dumps(exported))


if __name__=='__main__':
    unittest.main(verbosity=2)
