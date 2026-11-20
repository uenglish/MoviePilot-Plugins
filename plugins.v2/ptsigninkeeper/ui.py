"""Compact sign-in dashboard data; history is loaded only for an opened site."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
from threading import RLock
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.db.site_oper import SiteOper
from .result import signin_entry_unavailable
from .ptengine.rewards import normalize_reward

_LOCK = RLock()
_CACHE_SECONDS = 5
_COLORS = {'success': 'success', 'failed': 'error', 'pending': 'warning',
           'no_sign': 'info', 'inactive': 'grey'}
_LABELS = {'success': '已完成', 'failed': '需处理', 'pending': '待执行',
           'no_sign': '无签到功能', 'inactive': '已停用'}


def _text(value, limit=2000):
    """Bound site-provided text without interpreting it as completion evidence."""
    return str(value if value is not None else '').strip()[:limit]


def _today():
    """Match the existing runner's configured process timezone."""
    return datetime.today().date()


def _day(value):
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        return _today().isoformat()


def _safe_url(value):
    """Only expose ordinary site links; never forward connection credentials."""
    try:
        parts = urlsplit(str(value or ''))
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return ''
        query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if key.lower() not in {'key', 'token', 'passkey', 'apikey', 'api_key', 'auth', 'authorization'}]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    except (ValueError, TypeError):
        return ''


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _selection(value):
    if not isinstance(value, (list, tuple, set)):
        value = [value] if value is not None else []
    return {str(item) for item in value if item is not None and not isinstance(item, bool)}


def _metadata(host):
    """Use site IDs and active flags, retaining custom sites and current ordering."""
    try:
        sites = list(SiteOper().list_order_by_pri())
    except Exception:
        sites = []
    custom = getattr(host, '_PTSignInKeeper__custom_sites', None)
    if callable(custom):
        sites += list(custom() or [])
    result = {}
    for position, item in enumerate(sites):
        sid = _get(item, 'id')
        if sid is None or _get(item, 'public', False):
            continue
        sid = str(sid)
        active = _get(item, 'is_active', True)
        result[sid] = {'id': sid, 'name': _text(_get(item, 'name') or sid, 250),
                       'url': _safe_url(_get(item, 'url')), 'active': active not in (False, 0, '0', 'false'),
                       'priority': position}
    if not result:
        # Tests and older host adapters can expose a name map without SiteOper.
        build = getattr(host, '_build_sites_info', None)
        if callable(build):
            for sid, name in (build() or {}).items():
                sid = str(sid)
                result.setdefault(sid, {'id': sid, 'name': _text(name, 250), 'url': '',
                                        'active': True, 'priority': len(result)})
    return result


def _daily(host, day):
    """Exactly two key reads supply a list snapshot, regardless of site count."""
    records = []
    for action in ('签到', '登录'):
        value = host.get_data(key=f'{action}-{day}')
        value = value.get('results', {}) if isinstance(value, dict) else {}
        records.append({str(key): record for key, record in value.items() if isinstance(record, dict)}
                       if isinstance(value, dict) else {})
    return records[0], records[1]


def _snapshot(host, day):
    """Cache daily records for five seconds; date/config/site changes invalidate it."""
    metadata = _metadata(host)
    sign_selection = _selection(getattr(host, '_sign_sites', []))
    login_selection = _selection(getattr(host, '_login_sites', []))
    key = (day, tuple(sorted(sign_selection)), tuple(sorted(login_selection)),
           getattr(host, '_generation', None),
           tuple((sid, row['name'], row['url'], row['active'], row['priority']) for sid, row in metadata.items()))
    now = time.monotonic()
    with _LOCK:
        cached = getattr(host, '_signin_ui_snapshot', None)
        if cached and cached['key'] == key and now - cached['created'] < _CACHE_SECONDS:
            return cached
        signs, logins = _daily(host, day)
        selected = set(metadata) if 'all' in sign_selection or 'all' in login_selection else sign_selection | login_selection
        selected |= set(signs) | set(logins)
        rows = []
        for sid in selected:
            sign, login = signs.get(sid, {}), logins.get(sid, {})
            meta = metadata.get(sid) or {
                'id': sid, 'name': _text(sign.get('site_name') or login.get('site_name') or sid, 250),
                'url': '', 'active': False, 'priority': len(metadata) + 1}
            rows.append(_row(meta, sign, login, day,
                             'all' in sign_selection or sid in sign_selection,
                             'all' in login_selection or sid in login_selection))
        snapshot = {'key': key, 'created': now, 'rows': rows, 'signs': signs, 'logins': logins,
                    'metadata': metadata, 'sign_selection': sign_selection, 'login_selection': login_selection}
        host._signin_ui_snapshot = snapshot
        return snapshot


def _login_record(sign, login):
    if login:
        return login
    if _confirmed_success(sign) or sign.get('logged_in') is True:
        return {'success': True, 'logged_in': True,
                'message': '账号校验通过' if sign.get('signin_supported') is False else '签到任务已确认登录'}
    if sign.get('signin_supported') is False:
        return {'success': False, 'message': _text(sign.get('message')),
                'unconfirmed': bool(sign.get('unconfirmed'))}
    return {}


def _confirmed_success(record):
    return record.get('success') is True and not record.get('unconfirmed')


def _view_records(meta, sign, login):
    """Interpret legacy account-only receipts without changing stored history.

    M-Team's adapter never signs in. Its older receipts used ``success`` for
    account verification and did not save the capability flag. Match only known
    domain families; neither a site name nor a message can supply this evidence.
    Other adapters retain their explicit stored capability, since routing may
    have changed across versions (for example HDDolby's cookie/API modes).
    """
    sign = dict(sign)
    domain = (urlsplit(meta.get('url') or '').hostname or '').lower().rstrip('.')
    known_login_only = any(domain == root or domain.endswith('.' + root)
                           for root in ('m-team.cc', 'm-team.io'))
    if sign and (sign.get('signin_supported') is False or known_login_only):
        sign['signin_supported'] = False
    if _confirmed_success(sign):
        sign['bonus'] = normalize_reward(sign.get('bonus'), meta.get('url', ''), sign.get('message'))
    login = _login_record(sign, dict(login))
    if sign.get('signin_supported') is False:
        # ``success`` in an account-only receipt must not increase sign-in or
        # reward totals; the separate login result retains the original proof.
        sign['success'] = False
    return sign, login


def _state(record, action, selected, active):
    if not active:
        return {'label': '已停用', 'color': 'grey'}
    if not record:
        return {'label': '未执行' if selected else '未选择', 'color': 'warning' if selected else 'grey'}
    if action == '签到' and record.get('signin_supported') is False:
        return {'label': '无签到功能', 'color': 'info'}
    if action == '签到' and str(record.get('message') or '').startswith('待答题'):
        return {'label': '待答题', 'color': 'warning'}
    if record.get('unconfirmed'):
        return {'label': '结果未确认', 'color': 'warning'}
    if _confirmed_success(record):
        return {'label': '签到成功' if action == '签到' else '登录成功', 'color': 'success'}
    if action == '签到' and signin_entry_unavailable(record.get('message')):
        return {'label': '入口暂不可用', 'color': 'error'}
    return {'label': '签到失败' if action == '签到' else '登录失败', 'color': 'error'}


def _row(meta, sign, login, day, sign_selected, login_selected):
    """Completion uses explicit booleans; messages never turn a failure into success."""
    sign, login = _view_records(meta, sign, login)
    active = meta['active']
    signin_status = _state(sign, '签到', sign_selected, active)
    login_status = _state(login, '登录', login_selected, active)
    if not active:
        category = 'inactive'
    elif signin_status['color'] == 'error' or login_status['color'] == 'error':
        category = 'failed'
    elif sign.get('unconfirmed') or login.get('unconfirmed'):
        category = 'pending'
    elif sign.get('signin_supported') is False:
        category = 'no_sign'
    elif _confirmed_success(sign) or (not sign_selected and _confirmed_success(login)):
        category = 'success'
    else:
        category = 'pending'
    known_reward = (_confirmed_success(sign) and sign.get('bonus') not in (None, '')
                    and not isinstance(sign.get('bonus'), bool))
    bonus = _text(sign.get('bonus'), 100) if known_reward else ('未获取' if _confirmed_success(sign) else '—')
    message = _text((login.get('message') if login_status['color'] == 'error'
                     and signin_status['color'] != 'error' else sign.get('message'))
                    or sign.get('message') or login.get('message'))
    activity = _text(sign.get('activity') or login.get('activity'), 300)
    # API timestamps use UTC; judge the day in MoviePilot's configured timezone.
    try:
        stamp = datetime.fromisoformat(activity.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            activity = stamp.astimezone(ZoneInfo(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, KeyError):
        pass
    visit_date = re.search(r'\d{4}-\d{2}-\d{2}', activity)
    visit_label = ('已读取当日访问记录' if visit_date and visit_date.group() == day else
                   '访问记录早于当日' if visit_date and visit_date.group() < day else '保活未确认')
    return {**meta, 'category': category, 'label': _LABELS[category], 'color': _COLORS[category],
            'signin_status': signin_status, 'login_status': login_status, 'bonus': bonus,
            'message': message, 'activity': activity, 'visit_label': visit_label,
            'engine': _text(sign.get('engine') or login.get('engine'), 80), 'date': day,
            'signin_selected': sign_selected, 'login_selected': login_selected,
            'signin_success': _confirmed_success(sign), 'logged_in': _confirmed_success(login),
            'reward_known': known_reward}


def _pagination(page, page_size, total):
    try:
        size = min(100, max(1, int(page_size)))
    except (ValueError, TypeError, OverflowError):
        size = 20
    pages = max(1, math.ceil(total / size))
    try:
        current = min(pages, max(1, int(page)))
    except (ValueError, TypeError, OverflowError):
        current = 1
    return current, size, pages


def _reward_amount(row):
    match = re.search(r'-?\d[\d,]*(?:\.\d+)?', row['bonus']) if row['reward_known'] else None
    return float(match.group(0).replace(',', '')) if match else -1


def _selected_rows(all_rows, query, category, sort, include_inactive):
    """Keep list and full export filtering identical, independently of pagination."""
    show_inactive = include_inactive is True or str(include_inactive).lower() in ('1', 'true') or category == 'inactive'
    query = _text(query, 200).casefold()
    rows = []
    for row in all_rows:
        if not row['active'] and not show_inactive:
            continue
        if query and query not in (row['name'] + ' ' + row['message']).casefold():
            continue
        if category == 'problem' and row['category'] not in ('failed', 'pending'):
            continue
        if category == 'success' and not row['signin_success']:
            continue
        if category == 'logged_in' and not row['logged_in']:
            continue
        if category == 'rewards' and not row['reward_known']:
            continue
        if category not in ('', 'all', 'problem', 'success', 'logged_in', 'rewards') and row['category'] != category:
            continue
        rows.append(row)
    default = lambda row: (row['priority'], row['name'].casefold())
    if sort == 'name':
        rows.sort(key=lambda row: row['name'].casefold())
    elif sort == 'status':
        order = {'failed': 0, 'pending': 1, 'no_sign': 2, 'success': 3, 'inactive': 4}
        rows.sort(key=lambda row: (order[row['category']], *default(row)))
    elif sort == 'reward':
        rows.sort(key=lambda row: (-_reward_amount(row), *default(row)))
    else:
        rows.sort(key=default)
    return rows


def list_sites(host, query='', category='all', sort='priority', page=1, page_size=20,
               day='', include_inactive=False):
    """Return one page plus active-only metrics; disabled sites have a separate count."""
    day = _day(day)
    snapshot = _snapshot(host, day)
    all_rows = snapshot['rows']
    active = [row for row in all_rows if row['active']]
    summary = {'total_sites': len(active), 'success': sum(row['signin_success'] for row in active),
               'failed': sum(row['category'] == 'failed' for row in active),
               'pending': sum(row['category'] == 'pending' for row in active),
               'no_sign': sum(row['category'] == 'no_sign' for row in active),
               'logged_in': sum(row['logged_in'] for row in active),
               'rewards': sum(row['reward_known'] for row in active),
               'inactive': sum(not row['active'] for row in all_rows)}
    rows = _selected_rows(all_rows, query, category, sort, include_inactive)
    current, size, pages = _pagination(page, page_size, len(rows))
    lock = getattr(host, '_run_lock', None)
    refreshing = bool(lock.locked()) if lock is not None and hasattr(lock, 'locked') else False
    return {'summary': summary, 'items': [dict(row) for row in rows[(current - 1) * size:current * size]],
            'total': len(rows), 'page': current, 'page_size': size, 'pages': pages,
            'date': day, 'today': _today().isoformat(), 'updated_at': None,
            'generated_at': int(time.time()), 'refreshing': refreshing,
            'days': [(_today() - timedelta(days=index)).isoformat() for index in range(14)]}


def _history_record(record):
    """Whitelist history fields rather than returning arbitrary plugin data."""
    if not record:
        return {}
    return {'success': record.get('success') is True, 'logged_in': record.get('logged_in') is True,
            'message': _text(record.get('message')), 'bonus': _text(record.get('bonus'), 100),
            'signin_supported': record.get('signin_supported') is not False,
            'activity': _text(record.get('activity'), 300), 'engine': _text(record.get('engine'), 80),
            'unconfirmed': bool(record.get('unconfirmed'))}


def site_detail(host, site, day='', **_):
    """Load fourteen days only when one site's detail panel is opened."""
    day = _day(day)
    snapshot = _snapshot(host, day)
    site = str(site)
    row = next((dict(item) for item in snapshot['rows'] if item['id'] == site), None)
    history = []
    end = date.fromisoformat(day)
    for offset in range(14):
        history_day = (end - timedelta(days=offset)).isoformat()
        signs, logins = ((snapshot['signs'], snapshot['logins']) if offset == 0 else _daily(host, history_day))
        sign, login = signs.get(site, {}), logins.get(site, {})
        if not sign and not login:
            continue
        if row is None:
            meta = snapshot['metadata'].get(site) or {'id': site,
                'name': _text(sign.get('site_name') or login.get('site_name') or site, 250),
                'url': '', 'active': False, 'priority': len(snapshot['metadata']) + 1}
            row = _row(meta, {}, {}, day, False, False)
        sign, login = _view_records(row, sign, login)
        history.append({'day': history_day, 'sign': _history_record(sign), 'login': _history_record(login),
                        'signin_status': _state(sign, '签到', True, True),
                        'login_status': _state(login, '登录', True, True)})
    return {'found': row is not None, 'site': row, 'history': history}


def export_rows(host, query='', category='all', day='', include_inactive=False, sort='priority'):
    """Export every matching site, retaining actual zero rewards and safe fields."""
    day = _day(day)
    rows = _selected_rows(_snapshot(host, day)['rows'], query, category, sort, include_inactive)
    return [{'日期': day, '站点': row['name'], '状态': row['label'],
             '签到状态': row['signin_status']['label'], '登录状态': row['login_status']['label'],
             '本次奖励': row['bonus'], '结果说明': row['message'], '最近访问': row['activity']}
            for row in rows]
