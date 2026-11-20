import unittest
from app.plugins.ptsigninkeeper.ptengine import core
from app.plugins.ptsigninkeeper.ptengine.rewards import normalize_reward, daily_header_reward, pttime_daily_reward, rousi_daily_reward
from app.plugins.ptsigninkeeper.integration import row_result

class RewardsTests(unittest.TestCase):
    def test_pttime_readonly_record_matches_date_and_receipt(self):
        page = '''<p><b>7天签到记录</b>（用户ID：<a href="/userdetails.php?id=42">42</a>）</p>
        <span>时间：2026-11-20 00:01:03</span><span>获得魔力值：<b>600</b></span>
        <span>连续天数：124天</span><br>
        <span>时间：2026-11-19 00:01:02</span><span>获得魔力值：999</span>
        <h2>说明</h2><span>时间：2026-11-21 00:01:03</span><span>获得魔力值：888</span>'''
        self.assertEqual(pttime_daily_reward(page, '2026-11-20'), '600 魔力值')
        self.assertEqual(pttime_daily_reward(page, '2026-11-21'), '')
        self.assertEqual(pttime_daily_reward(page.replace('600', '0'), '2026-11-20'), '0 魔力值')
        self.assertEqual(pttime_daily_reward(page.replace('7天签到记录', '示例'), '2026-11-20'), '')

    def test_rousi_receipt_uses_total_not_policy_balance_or_experience(self):
        payload={'claimed_today':True,'today':'2026-11-20','policy':{'fixed_reward':'900'},'magic_balance':'99999',
                 'today_record':{'attendance_date':'2026-11-20','base_reward':'100','streak_reward':'50',
                                 'experience_reward':'1','total_reward':'150'}}
        self.assertEqual(rousi_daily_reward(payload,'2026-11-20'),'150 魔力值')
        self.assertEqual(rousi_daily_reward(payload,'2026-11-21'),'')
        payload['today_record']['total_reward']='0'
        self.assertEqual(rousi_daily_reward(payload,'2026-11-20'),'0 魔力值')
        payload['claimed_today']=False
        self.assertEqual(rousi_daily_reward(payload,'2026-11-20'),'')

    def test_city_receipt_retains_full_currency(self):
        self.assertEqual(normalize_reward(core.extract_bonus('本次签到获得魅力值：123',{})), '123 魅力值')

    def test_optional_counters_do_not_change_currency_or_amount(self):
        for receipt in ('100个魔力值', '100 点魔力值', '100 魔力值', '100 點魔力值'):
            self.assertEqual(normalize_reward(receipt), '100 魔力值')
        for receipt, expected in [('12朵茉莉', '12 茉莉'), ('22粒爆米花', '22 爆米花'),
                                  ('0枚金币', '0 金币'), ('1,234.50 个憨豆', '1,234.50 憨豆'),
                                  ('100 做种积分', '100 做种积分'), ('5 points', '5 points')]:
            self.assertEqual(normalize_reward(receipt), expected)
        self.assertEqual(daily_header_reward('签到已得 100 个魔力值'), '100 魔力值')

    def test_rules_do_not_override_actual_daily_receipt(self):
        page = '初次签到获得10个魔力值。连续签到可获得奖励。<p>本次签到获得115个魔力值</p>'
        self.assertEqual(normalize_reward(core.extract_bonus(page, {})), '115 魔力值')
        self.assertEqual(core.extract_bonus('初次签到获得10个魔力值。签到成功', {}), '')
        self.assertEqual(daily_header_reward('魔力值: 90000 [签到已得115, 补签卡: 10]'), '115 魔力值')
        self.assertEqual(daily_header_reward('魔力值: 90000 [签到已得1,150, 补签卡: 10]'), '1,150 魔力值')

    def test_units_zero_html_and_trailing_notes(self):
        for receipt, expected in [('获得了 12 朵茉莉','12 茉莉'),('获得 22 粒爆米花','22 爆米花'),
                                  ('+18 UCoin','18 UCoin'),('获得 0 魔力','0 魔力')]:
            self.assertEqual(normalize_reward(core.extract_bonus('签到成功，'+receipt,{})),expected)
            self.assertEqual(normalize_reward(core.message_bonus('签到成功（'+receipt+'）（已自动改用 Browserless）')),expected)
        self.assertEqual(normalize_reward(core.extract_bonus('签到成功，获得 <b>12</b> 朵茉莉',{})),'12 茉莉')
        self.assertEqual(normalize_reward(core.message_bonus('签到成功（获得 20 魔力）（/index.php?action=addbonus）')),'20 魔力')

    def test_daily_amount_uses_label_never_balance(self):
        self.assertEqual(daily_header_reward('猫粮 [使用]: 90000 (签到已得110)'),'110 猫粮')
        self.assertEqual(daily_header_reward('魔力值: 90000 (签到已得35)'),'35 魔力值')
        self.assertEqual(daily_header_reward('电力值: 90000 已签到(85)'),'')
        self.assertEqual(daily_header_reward('魔力值: 27493.7 签到详情'),'')
        self.assertEqual(core.extract_bonus('<template>签到已得999</template>签到成功，获得 10 魔力',{}),'获得 10 魔力')
        self.assertEqual(core.extract_bonus('论坛获得 999 魔力；猫粮: 90000 (签到已得110)',{}),'110 猫粮')

    def test_verified_domain_units_and_unknown_are_distinct(self):
        for domain,unit in [('ubits.club','U币'),('zmpt.cc','电力值'),('qingwapt.com','蝌蚪'),('agsvpt.com','冰晶'),
                            ('xingyungept.org','星焱'),('cspt.top','元宝'),('ptlgs.org','工分'),('crabpt.vip','蟹币值'),('luckpt.de','幸运星'),
                            ('cyanbug.net','魔力值'),('gamegamept.com','G值')]:
            self.assertEqual(normalize_reward('100','https://'+domain),'100 '+unit)
        self.assertEqual(normalize_reward('100','https://unknown.test'),'100')
        self.assertEqual(normalize_reward('100','https://ubits.club.evil.test'),'100')
        self.assertEqual(normalize_reward('100','', '签到成功（获得100 茉莉）'),'100 茉莉')
        self.assertEqual(normalize_reward('100','', '签到成功（获得200 茉莉）'),'100')

    def test_failure_cannot_gain_reward(self):
        result=row_result({'status':'failed','bonus':'100 魔力','message':'签到失败，获得100 魔力'},'签到')
        self.assertEqual(result.bonus,'')

    def test_old_unitless_history_views_do_not_modify_storage(self):
        from app.plugins.ptsigninkeeper import ui
        original = {'success':True, 'bonus':'315', 'message':'今日已签到'}
        viewed, _ = ui._view_records({'url':'https://ubits.club'}, original, {})
        self.assertEqual(viewed['bonus'],'315 U币')
        self.assertEqual(original['bonus'],'315')

    def test_header_reward_read_failure_does_not_change_success(self):
        from unittest.mock import patch
        from app.plugins.ptsigninkeeper.ptengine.runtime import RuleEngine
        info={'id':25,'name':'猫站','url':'https://pterclub.net','cookie':'test'}
        engine=RuleEngine({'read_activity':False})
        for header,expected in [('<a href="logout.php">退出</a>猫粮: 90000 (签到已得110)','110 猫粮'),
                                ('猫粮: 90000 (签到已得110)',''),('', '')]:
            with patch('app.plugins.ptsigninkeeper.ptengine.runtime.run_site_module',return_value=core.SignResult('already','今日已签到')), \
                 patch('app.plugins.ptsigninkeeper.ptengine.runtime.fetch_page',return_value=header):
                row=engine._sign_one(info,{},None)
            self.assertEqual(row['status'],'already')
            self.assertEqual(row['bonus'],expected)
