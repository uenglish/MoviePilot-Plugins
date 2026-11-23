"""躺平新版普通签到表单，参考 PTSiteSignIn 3.1.13。"""
from urllib.parse import urljoin, urlsplit


class TangPT:
    site_url = 'tangpt.top'
    authoritative = True

    def signin(self, site, ctx):
        from ..core import classify, cloudflare_pending, login_page
        from ..quiz import clean_tree
        rule = {'strict_status_keywords': True,
                'success_keywords': ['签到成功', '簽到成功'],
                'already_keywords': ['签到已得', '今日已签到', '今天已签到', '已经签到',
                                     '已签到', '已簽到', '请不要重复签到']}
        url = urljoin(str(site.get('url') or 'https://tangpt.top/'), '/attendance.php')
        session = None
        submitted = False

        def read():
            response = session.get(url, timeout=ctx.timeout, proxies=ctx.proxies,
                                   allow_redirects=False)
            if response is None:
                return '', '签到页未返回响应'
            if response.status_code != 200:
                return '', f'无法读取签到页（HTTP {response.status_code}）'
            html = response.text or ''
            if login_page(html):
                return '', 'Cookie 已失效，请更新站点登录信息'
            if cloudflare_pending(html) or 'cf-turnstile' in html:
                return '', '站点需要人工验证，请在站点内完成后重试'
            return html, ''

        try:
            session = ctx._session()
            page, error = read()
            if not page:
                return False, '签到失败：' + error
            receipt = classify(page, rule)
            if receipt.status in {'success', 'already'}:
                return True, receipt.message
            # Ignore hidden decoy forms while retaining hidden CSRF fields.
            tree = clean_tree(page)
            forms = tree.xpath('//form[.//input[@type="submit"] or .//button]') if tree is not None else []
            matches = []
            for form in forms:
                action = urljoin(url, form.get('action') or url)
                if urlsplit(action).netloc != urlsplit(url).netloc or urlsplit(action).scheme != urlsplit(url).scheme:
                    continue
                if urlsplit(action).path != '/attendance.php' or (form.get('method') or 'get').lower() != 'post':
                    continue
                buttons = form.xpath('.//input[@type="submit" and not(@disabled)] | .//button[not(@disabled)]')
                buttons = [b for b in buttons if any(w in (b.get('value') or ''.join(b.itertext()))
                                                    for w in ('立即签到', '立即簽到'))]
                if len(buttons) == 1:
                    matches.append((form, buttons[0], action))
            if len(matches) != 1:
                return False, '签到失败：未找到唯一的签到表单，未提交'
            form, button, action = matches[0]
            if form.xpath('.//input[@name="imagestring" or @name="imagehash" or @name="cf-turnstile-response"]'):
                return False, '签到失败：表单要求人工验证，请在站点内完成'
            data = {el.get('name'): el.get('value', '') for el in form.xpath(
                './/input[@type="hidden" and @name and not(@disabled)]')}
            if button.get('name'):
                data[button.get('name')] = button.get('value', '')
            submitted = True
            try:
                session.post(action, data=data, headers={'Referer': url}, timeout=ctx.timeout,
                             proxies=ctx.proxies, allow_redirects=False)
            except Exception:
                pass  # 结果未知时仍回读，绝不重复提交。
            after, error = read()
            receipt = classify(after, rule)
            if receipt.status in {'success', 'already'}:
                return True, receipt.message + '（回读确认）'
            return False, '签到未确认：' + (error or '提交后尚未取得有效回执')
        except Exception:
            return False, ('签到未确认：已提交，请稍后检查站点记录' if submitted else '签到失败：无法读取站点，请检查网络后重试')
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
