"""Prevent guessing, duplicate submissions and false completion for quiz sites."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper import PTSignInKeeper, ui
from app.plugins.ptsigninkeeper.ptengine import core, quiz
from app.plugins.ptsigninkeeper.ptengine.sites import u2
from app.plugins.ptsigninkeeper.ptengine.runtime import RuleEngine
from app.plugins.ptsigninkeeper.integration import row_result

FORM = '''<a href="logout.php">退出</a><aside>魔力值: 50000</aside>
<form method="post"><input type="hidden" name="questionid" value="qid-secret">
<input type="hidden" name="csrf" value="csrf-secret"><div>请问：哪个选项正确？</div>
<label><input name="choice[]" type="radio" value="8">甲</label>
<label><input name="choice[]" type="radio" value="2">乙</label>
<input type="submit" name="submit" value="提交"><input type="submit" name="wantskip" value="不会"></form>'''
SIGNED = '<a href="logout.php">退出</a><div>今天已经签过到了</div><div>获得 10 魔力</div>'
U2_FORM = '''<a href="logout.php">退出</a><form action="showup.php" method="get"><input type="date" name="date"></form>
<form action="showup.php?action=show"><input name="req" type="hidden" value="secret-req">
<input name="hash" type="hidden" value="secret-hash"><input name="form" type="hidden" value="secret-form">
<input name="_csrf" type="hidden" value="secret-csrf"><img src="/question.png">
<input type="submit" name="answer0" value="动漫甲"><input type="submit" name="answer1" value="动漫乙">
<input type="submit" name="answer2" value="动漫丙"><input type="submit" name="answer3" value="动漫丁"></form>'''
U2_SIGNED = '<a href="logout.php">退出</a><p>感谢，今天已签到。</p><form><input name="date"></form>'


class QuizTests(unittest.TestCase):
    def test_discovered_u2_keeps_relative_imports_and_readback(self):
        module = next(item for item in core.load_site_modules(refresh=True)
                      if item.site_url == 'u2.dmhy.org')
        ctx = Mock()
        ctx.page.return_value = U2_SIGNED
        self.assertEqual(module().signin({'url':u2.BASE}, ctx), (True, '今日已签到'))
        ctx.post.assert_not_called()

    def rule(self, **extra):
        return {**core.resolve_rule({'url': 'https://ptchdbits.co'}, {}), **extra}

    def test_form_is_scoped_and_labels_survive_order(self):
        form = core.extract_quiz_form('<input name="csrf" value="unrelated">' + FORM)
        self.assertEqual(form['options'], {'8': '甲', '2': '乙'})
        self.assertEqual(form['question'], '哪个选项正确？')
        self.assertEqual(form['fields']['csrf'], 'csrf-secret')
        self.assertFalse(form['multiple'])
        self.assertTrue(core.extract_quiz_form(FORM.replace('radio', 'checkbox'))['multiple'])
        self.assertIsNone(core.extract_quiz_form('<template>' + FORM + '</template>'))

    def test_labels_with_tail_and_for_attribute(self):
        form = FORM.replace('<label><input name="choice[]" type="radio" value="8">甲</label>',
                            '<input name="choice[]" type="radio" value="8">甲<br>')
        self.assertEqual(core.extract_quiz_form(form)['options']['8'], '甲')
        form = FORM.replace('<label><input name="choice[]" type="radio" value="8">甲</label>',
                            '<input id="a" name="choice[]" type="radio" value="8"><label for="a">甲</label>')
        self.assertEqual(core.extract_quiz_form(form)['options']['8'], '甲')

    def test_known_answers_match_exact_question_and_label(self):
        rule = {'quiz_answers': {'哪个选项正确？': '乙'}}
        self.assertEqual(quiz.choose(rule, ' 哪个选项正确? ', {'8': '甲', '2': '乙'}), ['2'])
        with self.assertRaises(ValueError):
            quiz.choose(rule, '哪个选项正确？注意选择错误答案', {'8': '甲', '2': '乙'})
        with self.assertRaises(ValueError):
            core.select_quiz_choices({}, '未知题目', ['8', '2'])

    def test_no_ai_uses_original_skip_payload(self):
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200, text=FORM)
        session.get.return_value = SimpleNamespace(status_code=200, text=SIGNED)
        result = core.http_quiz_sign(session, 'https://ptchdbits.co/bakatest.php', FORM,
                                     'https://ptchdbits.co', self.rule(), 2, None)
        self.assertEqual(result.status, 'success')
        self.assertEqual(session.post.call_args.kwargs['data']['wantskip'], '不会')
        self.assertNotIn('submit', session.post.call_args.kwargs['data'])
        self.assertEqual(session.post.call_count, 1)

    def test_one_submission_has_submit_not_skip_and_requires_readback(self):
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200, text=FORM)
        session.get.return_value = SimpleNamespace(status_code=200, text=SIGNED)
        solver, claim = Mock(spec=[], return_value=['2']), Mock(return_value=True)
        result = core.http_quiz_sign(session, 'https://ptchdbits.co/bakatest.php', FORM,
                                     'https://ptchdbits.co', self.rule(_quiz_solver=solver, _quiz_claim=claim,
                                     quiz_fields={'wantskip': '不会', 'questionid': 'evil'}), 2, None)
        self.assertEqual(result.status, 'success')
        data = session.post.call_args.kwargs['data']
        self.assertEqual(data['choice[]'], '2')
        self.assertEqual(data['questionid'], 'qid-secret')
        self.assertEqual(data['submit'], '提交')
        self.assertNotIn('wantskip', data)
        self.assertNotIn('csrf-secret', str(solver.call_args))
        self.assertNotIn('50000', str(solver.call_args))
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.get.call_count, 1)
        claim.assert_called_once()

    def test_remaining_form_and_wrong_feedback_are_not_success(self):
        for post, read in [(SIGNED, FORM), ('<p>答案错误，失去 1 魔力</p>', SIGNED), (SIGNED, '<div hidden>'+SIGNED+'</div>')]:
            session = Mock()
            session.post.return_value = SimpleNamespace(status_code=200, text=post)
            session.get.return_value = SimpleNamespace(status_code=200, text=read)
            result = core.http_quiz_sign(session, 'https://ptchdbits.co/bakatest.php', FORM,
                                         'https://ptchdbits.co', self.rule(quiz_answer='2'), 2, None)
            self.assertEqual(result.status, 'failed')

    def test_active_form_wins_over_header_already(self):
        self.assertIsNone(core._quiz_already_result(FORM + SIGNED, self.rule()))
        self.assertIsNone(core._quiz_already_result(SIGNED.replace('logout.php', 'login.php'), self.rule()))

    def test_invalid_or_unavailable_ai_and_duplicate_do_not_post(self):
        for solver, claim in [(Mock(spec=[],return_value=['other']), None), (Mock(spec=[],return_value=['2','8']), None),
                              (Mock(spec=[],side_effect=ValueError('待答题：AI 超时')), None),
                              (Mock(spec=[],return_value=['2']), Mock(return_value=False))]:
            session = Mock()
            result = core.http_quiz_sign(session, 'https://ptchdbits.co/bakatest.php', FORM,
                                         'https://ptchdbits.co', self.rule(_quiz_solver=solver, _quiz_claim=claim), 2, None)
            self.assertEqual(result.status, 'pending')
            session.post.assert_not_called()

    def test_ai_response_requires_valid_ids_texts_and_confidence(self):
        options = {'8': '甲', '2': '乙'}
        good = {'answer_ids':['2'], 'answer_texts':['乙'], 'confident':True, 'confidence':.96}
        self.assertEqual(quiz.AssistantSolver.parse_reply(json.dumps(good), options), ['2'])
        for overrides in [{'confidence':.8}, {'confidence':True}, {'confidence':2}, {'confident':False},
                          {'answer_ids':['9']}, {'answer_ids':['2','2']}, {'answer_texts':['甲']}, {'answer_ids':[]}]:
            with self.assertRaises(ValueError):
                quiz.AssistantSolver.parse_reply(json.dumps({**good, **overrides}), options)
        with self.assertRaises(ValueError):
            quiz.AssistantSolver.parse_reply('not json', options)

    def test_model_errors_are_sanitized(self):
        solver = quiz.AssistantSolver(True)
        async def broken(*args):
            raise RuntimeError('Authorization: secret-key')
        with patch.object(solver, 'status', return_value={'available':True,'image':True}), patch.object(solver, '_ask', broken):
            with self.assertRaises(ValueError) as caught:
                solver('题目', {'a':'甲'})
            self.assertNotIn('secret-key', str(caught.exception))

    def test_model_timeout_is_pending(self):
        import asyncio
        solver = quiz.AssistantSolver(True)
        solver.timeout = .01
        async def slow(*args):
            await asyncio.sleep(1)
        with patch.object(solver, 'status', return_value={'available':True,'image':True}), patch.object(solver, '_ask', slow):
            with self.assertRaisesRegex(ValueError, '超时'):
                solver('题目', {'a':'甲'})

    def test_image_request_rejects_external_redirect_large_and_wrong_type(self):
        session = Mock()
        for source in ('https://other.test/a.png', 'http://u2.dmhy.org/a.png', 'https://u2.dmhy.org:81/a.png', '//user:pass@u2.dmhy.org/a.png'):
            with self.assertRaises(ValueError):
                quiz.read_question_image(session, u2.SHOWUP_URL, source)
        session.get.assert_not_called()
        for code, headers in [(302,{}), (200,{'Content-Type':'text/html'}),
                              (200,{'Content-Type':'image/png','Content-Length':str(quiz.MAX_IMAGE_BYTES+1)})]:
            session.get.return_value = Mock(status_code=code, headers=headers)
            with self.assertRaises(ValueError):
                quiz.read_question_image(session, u2.SHOWUP_URL, '/question.png')
        response = Mock(status_code=200,headers={'Content-Type':'image/png'})
        response.iter_content.return_value = [b'png']
        session.get.return_value = response
        self.assertEqual(quiz.read_question_image(session,u2.SHOWUP_URL,'/question.png'), ('image/png','cG5n'))
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        response.close.assert_called_once()

    def test_failure_instructions_are_not_result_feedback(self):
        self.assertFalse(quiz.wrong_feedback('<p>回答错误者扣1分，回答正确者加5分</p>'))
        self.assertFalse(quiz.wrong_feedback('<table><tr><td><span>答案错误！</span></td></tr></table>'))
        self.assertTrue(quiz.wrong_feedback('<p>你的答案错误，请重新作答</p>'))

    def test_daily_submission_guard_survives_reload(self):
        saved = {}
        host = SimpleNamespace(get_data=lambda key:saved.get(key),save_data=lambda key,value:saved.update({key:value}))
        self.assertTrue(PTSignInKeeper._claim_quiz_attempt(host, '22'))
        self.assertFalse(PTSignInKeeper._claim_quiz_attempt(host, '22'))
        self.assertTrue(PTSignInKeeper._claim_quiz_attempt(host, '45'))
        self.assertNotIn('qid', str(saved))

    def test_pending_display_is_not_failure_or_success(self):
        result=row_result({'site':'彩虹岛','status':'pending','message':'待答题：未配置 AI'},'签到')
        self.assertFalse(result.success)
        self.assertTrue(result.message.startswith('待答题：'))
        self.assertEqual(ui._state({'success':False,'message':result.message},'签到',True,True),
                         {'label':'待答题','color':'warning'})

    def test_metadata_declared_vision_overrides_checkbox(self):
        fake = {'LLM_PROVIDER':'provider','LLM_MODEL':'model','LLM_API_KEY':'key', 'LLM_SUPPORT_IMAGE_INPUT':True}
        runtime=Mock()
        runtime.get_auth_status.return_value={}
        solver=quiz.AssistantSolver(True)
        with patch.object(solver, '_setting', side_effect=lambda key,default=None:fake.get(key,default)), \
             patch('app.agent.llm.gateway.resolve_llm_provider_runtime',return_value=runtime):
            runtime.resolve_cached_model_metadata.return_value={'modalities':{'input':['text']}}
            self.assertFalse(solver.status()['image'])
            runtime.resolve_cached_model_metadata.return_value={'modalities':{'input':['text','image']}}
            self.assertTrue(solver.status()['image'])
            self.assertTrue(solver.status()['available'])

    def test_unconfigured_assistant_keeps_legacy_mode(self):
        solver=quiz.AssistantSolver(True)
        with patch.object(solver,'status',return_value={'available':False,'image':False}):
            answer,mode=quiz.choose_text_quiz({'_quiz_solver':solver},core.extract_quiz_form(FORM))
        self.assertEqual(mode,'legacy')
        self.assertIn(answer[0],['8','2'])

    def test_local_browser_uncertain_result_does_not_submit_again(self):
        engine=RuleEngine({'read_activity':False})
        browser=Mock()
        with patch('app.plugins.ptsigninkeeper.ptengine.runtime.playwright_sign',return_value=core.SignResult('failed','已点击，未确认')):
            result,mode=engine._maybe_fallback({'url':'https://hddolby.com'}, {'auto_fallback':True,'local_browser_fallback':True},core.SignResult('failed','未匹配到'),browser)
        self.assertEqual(mode,'playwright')
        browser.sign.assert_not_called()


class U2Tests(unittest.TestCase):
    def test_reward_requires_own_account_and_today_receipt(self):
        header = '<div id="info_block"><a href="userdetails.php?id=7">本人</a>UCoin: 99999</div>'
        row = '<tr><td><a href="userdetails.php?id=7">本人</a><time title="2026-11-20 15:01:46">9分钟前</time>奖励UCoin: 28</td></tr>'
        other = row.replace('id=7', 'id=8').replace('28', '999')
        page = U2_SIGNED + header + '<table>' + other + row + '</table>'
        self.assertEqual(u2._daily_reward(page, day='2026-11-20'), '28 UCoin')
        self.assertEqual(u2._daily_reward(page.replace('UCoin: 28', 'UCoin: 0'), day='2026-11-20'), '0 UCoin')
        for invalid in (page.replace(header, ''), page.replace('2026-11-20', '2026-11-19'),
                        page.replace(row, '<template>' + row + '</template>'),
                        page.replace(row, row.replace('href="userdetails', 'href="https://other.test/userdetails')),
                        U2_SIGNED + header + '<table>' + other + '</table>'):
            self.assertEqual(u2._daily_reward(invalid, day='2026-11-20'), '')

    def test_signed_readback_retains_reward_without_resubmitting(self):
        ctx = self.ctx([U2_SIGNED])
        with patch.object(u2, '_daily_reward', return_value='28 UCoin'):
            self.assertEqual(u2.U2().signin({}, ctx), (True, '今日已签到（获得 28 UCoin）'))
        ctx.post.assert_not_called()

    def ctx(self, pages, solver=None):
        ctx = Mock(timeout=2, proxies=None, rule={'_quiz_solver':solver} if solver else {})
        ctx.page.side_effect = pages
        ctx.post.return_value = SimpleNamespace(status_code=200, text='redirect')
        return ctx

    def test_signed_requires_visible_today_and_no_answer_form(self):
        self.assertTrue(u2._is_signed(U2_SIGNED))
        for text in ['<a href="logout.php">退出</a><a href="showup.php">Show Up</a>',
                     '<a href="logout.php">退出</a><template>今天已签到</template>',
                     U2_FORM + U2_SIGNED,
                     '<a href="logout.php">退出</a><div hidden>今天已签到</div>']:
            self.assertFalse(u2._is_signed(text))
        self.assertEqual(len(u2._showup_fields(U2_FORM)[1]), 4)
        self.assertEqual(u2._showup_fields('<template>'+U2_FORM+'</template>'), ({},[]))

    def test_no_ai_uses_original_random_and_signed_does_not_post(self):
        ctx=self.ctx([U2_FORM,U2_SIGNED])
        with patch.object(u2.random,'choice',return_value=('answer2','动漫丙')) as random_pick:
            ok,message=u2.U2().signin({},ctx)
        self.assertTrue(ok)
        self.assertIn('随机签到',message)
        self.assertEqual(ctx.post.call_args.kwargs['data']['answer2'],'动漫丙')
        random_pick.assert_called_once()
        ctx=self.ctx([U2_SIGNED])
        self.assertTrue(u2.U2().signin({},ctx)[0])
        ctx.post.assert_not_called()

    def test_submit_once_then_three_readbacks_and_no_history_image(self):
        def solver(question, options, **kwargs):
            self.assertEqual(options['answer1'], '动漫乙')
            self.assertNotIn('secret', str((question,options,kwargs)))
            return ['answer1']
        ctx=self.ctx([U2_FORM,U2_FORM,U2_FORM,U2_SIGNED],solver)
        with patch.object(quiz,'read_question_image', return_value=('image/png','cG5n')) as picture, patch.object(u2.time,'sleep'):
            self.assertEqual(u2.U2().signin({},ctx),(True,'签到成功'))
        self.assertEqual(ctx.post.call_count,1)
        self.assertEqual(ctx.page.call_count,4)
        self.assertEqual(ctx.post.call_args.kwargs['data']['answer1'],'动漫乙')
        self.assertEqual(picture.call_args.args[2],'/question.png')

    def test_invalid_answer_cannot_reach_post(self):
        def solver(*args,**kwargs):return ['nonexistent']
        ctx=self.ctx([U2_FORM],solver)
        with patch.object(quiz,'read_question_image',return_value=('image/png','cG5n')):
            ok,message=u2.U2().signin({},ctx)
        self.assertFalse(ok)
        ctx.post.assert_not_called()

    def test_authoritative_pending_does_not_use_browser(self):
        info={'id':45,'name':'U2','url':'https://u2.dmhy.org','cookie':'test'}
        engine=RuleEngine({'read_activity':False})
        with patch('app.plugins.ptsigninkeeper.ptengine.runtime.run_site_module',return_value=core.SignResult('pending','待答题：未配置 AI')), \
             patch.object(engine,'_sign_with_engine',side_effect=AssertionError('must not fallback')):
            row=engine._sign_one(info,{},None)
        self.assertEqual(row['status'],'pending')
