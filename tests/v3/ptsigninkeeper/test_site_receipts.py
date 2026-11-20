import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app.plugins.ptsigninkeeper.sites.pterclub import PTerClub
from app.plugins.ptsigninkeeper.sites.yema import YemaPT
from app.plugins.ptsigninkeeper.ptengine.sites.hdarea import HDArea
from app.plugins.ptsigninkeeper.integration import SignInBridge

class SiteReceiptsTests(unittest.TestCase):
    def test_hdarea_api_preserves_earned_fields_and_rejects_balances(self):
        site={'url':'https://hdarea.club','apikey':'test-only'}
        for payload, message, expected in [({'reward':100},'签到成功','100 魔力值'),
                ({'already_signed':True,'reward':0},'今日已签到','0 魔力值'),
                ({'seedbonus':99999,'bonus':88888},'签到成功',''),
                ({'message':'本次签到获得12点魔力值'},'成功','12 魔力值')]:
            with patch.object(HDArea,'_api_request',return_value=(payload,message)):
                ok,result=HDArea().signin(site,Mock())
            self.assertTrue(ok)
            if expected:self.assertIn(expected,result)
            else:self.assertEqual(result,'签到成功')

    def test_hxpt_already_preserves_own_dated_calendar_reward_without_submit(self):
        from app.plugins.ptsigninkeeper.ptengine.sites.hxpt import Hxpt
        from datetime import datetime
        day=datetime.now().date().isoformat()
        attendance={'checked_today':True,'reward':9999,'calendar':[
            {'date':'1999-01-01','reward':900}, {'date':day,'reward':8}]}
        ctx=Mock()
        ctx.get.return_value=SimpleNamespace(status_code=200,text=json.dumps({'code':0,'data':{'attendance':attendance}}))
        ok,message=Hxpt().signin({'cookie':'flux.session=test'},ctx)
        self.assertTrue(ok);self.assertIn('8 魔力值',message)
        ctx.post.assert_not_called()

    def test_haitang_already_preserves_reward_without_submit(self):
        from app.plugins.ptsigninkeeper.ptengine.sites.htpt import Htpt
        ctx=Mock();ctx.page.return_value='今日已签到 ✓ 连续5天，今日获得 <b>25</b> 魔力值'
        ok,message=Htpt().signin({'url':'https://www.htpt.cc'},ctx)
        self.assertTrue(ok);self.assertIn('25 魔力值',message)
        ctx.post.assert_not_called()

    def test_yema_daily_reward_uses_own_date_not_balance_or_leaderboard(self):
        info={'checkedInToday':True,'bonus':56696,'topList':[{'point':80}],
              'attendanceList':[{'checkDay':20261119,'point':999},{'checkDay':20261120,'point':70}]}
        self.assertEqual(YemaPT._daily_reward(info, '20261120'),'70')
        self.assertEqual(YemaPT._daily_reward(info, '20261121'),'')
        info['attendanceList'][1]['point']=0
        self.assertEqual(YemaPT._daily_reward(info, '20261120'),'0')

    def test_pter_preserves_html_reward_and_rejects_unknown_failure(self):
        info = {'name': '猫站', 'url': 'https://pterclub.net'}
        cases = [({'status':'1','message':'第237次，连续237天。本次签到获得<b>300</b>克猫粮。'}, True, '300 猫粮'),
                 ({'status':'0','message':'您今天已经签到过了，请勿重复刷新。'}, True, ''),
                 ({'status':'0','message':'请先登录'}, False, ''), ([], False, '')]
        for payload, ok, reward in cases:
            with patch.object(PTerClub, 'get_page_source', return_value=json.dumps(payload)):
                result = SignInBridge._legacy_result(info, '签到', PTerClub)
            self.assertEqual(result.success, ok)
            self.assertEqual(result.bonus, reward)

    def test_hdarea_keeps_actual_receipt(self):
        ctx = Mock()
        ctx.post.return_value = SimpleNamespace(status_code=200,text='已连续签到278天，此次签到您获得了100魔力值奖励!')
        ok, message = HDArea().signin({'url':'https://hdarea.club'},ctx)
        self.assertTrue(ok)
        self.assertIn('100魔力值', message)

    def test_yema_unchecked_submits_once_and_preserves_zero(self):
        info = {'name':'野马','url':'https://www.yemapt.org','apikey':'test'}
        response = SimpleNamespace(text='{}',json=lambda:{'success':True,'data':{'point':0}})
        with patch.object(YemaPT,'_checkin_info',return_value={'checkedInToday':False}), \
             patch.object(YemaPT,'_get_challenge',return_value={'parameters':{'test':True}}), \
             patch.object(YemaPT,'_altcha_payload',return_value='test-only'), \
             patch.object(YemaPT,'_request',return_value=(200,response)) as request:
            ok,message=YemaPT().signin(info)
        self.assertTrue(ok)
        self.assertIn('0 积分',message)
        request.assert_called_once()

    def test_yema_already_and_unknown_state_never_submit(self):
        for daily, expected in [({'checkedInToday':True,'continuousCheckInDays':10},True),
                                ({'checkedInToday':'false'},False),(None,False)]:
            with patch.object(YemaPT,'_checkin_info',return_value=daily), \
                 patch.object(YemaPT,'_get_challenge',side_effect=AssertionError('repeat submit')):
                ok,message=YemaPT().signin({'name':'野马','apikey':'test'})
            self.assertEqual(ok,expected)
            if not expected:self.assertNotIn('失效',message)
