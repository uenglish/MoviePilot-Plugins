"""One combined site table with explicit earned rewards and optional recent history."""
from datetime import datetime, timedelta
from .result import signin_entry_unavailable


def _status(record, action):
    """Return display state from booleans, never from success words in failure text."""
    if not record:
        return '未执行', 'grey'
    if record.get('unconfirmed'):
        return '结果未确认', 'grey'
    if record.get('success'):
        already = action == '签到' and str(record.get('message', '')).startswith('已签到')
        return ('已签到' if already else '成功'), 'success'
    if action == '签到' and signin_entry_unavailable(record.get('message')):
        return '入口暂不可用', 'warning'
    if action == '签到' and record.get('logged_in'):
        return ('无签到功能', 'warning') if record.get('signin_supported') is False else ('失败（已登录）', 'error')
    return '失败', 'error'


def collect_rows(host, now=None):
    """Join both daily histories by site ID; retain the site's reward unit verbatim."""
    now = now or datetime.today()
    names = {str(key): value for key, value in host._build_sites_info().items()}
    selected = {str(key) for key in host._resolve_site_ids(host._sign_sites)}
    selected.update(str(key) for key in host._resolve_site_ids(host._login_sites))
    daily = []
    for offset in range(14):
        day = (now - timedelta(days=offset)).strftime('%Y-%m-%d')
        sign = (host.get_data(key='签到-' + day) or {}).get('results') or {}
        login = (host.get_data(key='登录-' + day) or {}).get('results') or {}
        daily.append((day, {str(k): v for k, v in sign.items()}, {str(k): v for k, v in login.items()}))
    for _, signs, logins in daily:
        selected.update(signs)
        selected.update(logins)
    rows = []
    for sid in selected:
        history = []
        for day, signs, logins in daily:
            sign, login = signs.get(sid, {}), logins.get(sid, {})
            # A successful check-in also proves this session was logged in.
            if not login and (sign.get('success') or sign.get('logged_in')):
                login = {'success': True, 'message': '签到任务已确认登录'}
            if sign or login:
                history.append({'day': day, 'sign': sign, 'login': login})
        today = next((x for x in history if x['day'] == daily[0][0]), {'sign': {}, 'login': {}})
        sign, login = today['sign'], today['login']
        bonus = str(sign['bonus']) if sign.get('success') and sign.get('bonus') not in (None, '') else ''
        saved_name = next((entry[action].get('site_name') for entry in history
                           for action in ('sign', 'login') if entry[action].get('site_name')), '')
        name = names.get(sid) or saved_name or sid
        if sid.startswith('legacy-name:'):
            name += '（旧记录，未匹配站点 ID）'
        rows.append({'id': sid, 'name': name, 'sign': sign, 'login': login,
                     'bonus': bonus or ('未获取' if sign.get('success') else '—'),
                     'activity': sign.get('activity') or login.get('activity') or '未获取',
                     'history': history})
    order = {'error': 0, 'warning': 1, 'grey': 2, 'success': 3}
    rows.sort(key=lambda row: (min(order[_status(row['sign'], '签到')[1]],
                                  order[_status(row['login'], '登录')[1]]), str(row['name'])))
    return rows


def _chip(record, action):
    """State chip with full response available in its tooltip."""
    label, color = _status(record, action)
    return {'component': 'VChip', 'props': {'size': 'small', 'color': color, 'variant': 'tonal',
            'title': str(record.get('message') or '暂无执行记录')}, 'text': label}


def _cell(text=None, content=None):
    """A wrapping cell compatible with the host's declarative renderer."""
    node = {'component': 'td', 'props': {'style': 'padding:10px 12px;vertical-align:top;overflow-wrap:anywhere'}}
    if content is not None:
        node['content'] = content
    else:
        node['text'] = str(text or '')
    return node


def render_dashboard(host):
    """Render one status list; each site's details show both actions together."""
    rows = collect_rows(host)
    if not rows:
        return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
                 'text': '选择站点并执行后，这里会显示签到、登录、本次魔力和最近动向。'}]
    content = []
    for row in rows:
        details = []
        for entry in row['history']:
            sign, login = entry['sign'], entry['login']
            reward = (str(sign['bonus']) if sign.get('bonus') not in (None, '') else '未获取') if sign.get('success') else '—'
            text = (f'{entry["day"]} · 签到：{sign.get("message") or "未执行"}'
                    f' · 登录：{login.get("message") or "未执行"} · 本次奖励：{reward}')
            details.append({'component': 'div', 'props': {'style': 'margin-top:7px;font-size:12px'}, 'text': text})
        site = [{'component': 'div', 'props': {'style': 'font-weight:600'}, 'text': str(row['name'])},
                {'component': 'details', 'content': [
                    {'component': 'summary', 'props': {'style': 'font-size:12px;cursor:pointer;margin-top:5px'},
                     'text': '最近记录'}, *details]}]
        content.append({'component': 'tr', 'props': {'style': 'border-bottom:1px solid rgba(var(--v-theme-on-surface),.1)'},
                        'content': [_cell(content=site), _cell(content=[_chip(row['sign'], '签到')]),
                                    _cell(content=[_chip(row['login'], '登录')]), _cell(row['bonus']),
                                    _cell(row['activity'])]})
    headers = ['站点', '签到状态', '登录状态', '本次魔力 / 奖励', '最近动向']
    succeeded = sum(bool(row['sign'].get('success')) for row in rows)
    logged = sum(bool(row['login'].get('success')) for row in rows)
    return [{'component': 'VCard', 'props': {'variant': 'flat'}, 'content': [
        {'component': 'VCardTitle', 'text': '站点签到与登录'},
        {'component': 'VCardSubtitle', 'text': f'{len(rows)} 个站点 · 今日签到成功 {succeeded} · 登录成功 {logged}'},
        {'component': 'VCardText', 'content': [
            {'component': 'div', 'props': {'style': 'overflow-x:auto'}, 'content': [
                {'component': 'table', 'props': {'style': 'width:100%;min-width:680px;border-collapse:collapse;text-align:left'},
                 'content': [
                     {'component': 'thead', 'content': [{'component': 'tr', 'content': [
                         {'component': 'th', 'props': {'style': 'padding:10px 12px;white-space:nowrap'}, 'text': title}
                         for title in headers]}]},
                     {'component': 'tbody', 'content': content}]}]},
            {'component': 'div', 'props': {'style': 'margin-top:10px;font-size:12px;opacity:.7'},
             'text': '奖励保留站点返回的数量与单位；未返回奖励时显示“未获取”。最近记录保留 14 天。'}]}]}]
