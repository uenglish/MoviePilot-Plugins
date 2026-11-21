"""Shared configuration, migration and one-engine-per-site routing for PTSignInKeeper."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from app.log import logger
from app.db.site_oper import SiteOper

from .ptengine.rewards import normalize_reward
from .ptengine.tokens import browserless_tokens
from .ptengine.browserless_policy import browserless_site_ids
from .result import SiteResult
from .ptengine.core import (
    BrowserlessPool, DEFAULT_BROWSERLESS_URL, account_from_site, account_ready,
    normalize_host, parse_rules, preset_for_host, resolve_rule, site_module_for, message_bonus,
)
from .ptengine.runtime import RuleEngine

EXTRA_DEFAULTS = {
    'rich_notify': True, 'timeout': 30, 'retry_count': 1, 'retry_delay_minutes': 10,
    'read_activity': True, 'quiz_ai_enabled': False, 'quiz_ai_timeout': 60,
    'browserless_url': DEFAULT_BROWSERLESS_URL,
    'browserless_tokens': None,
    'browserless_sites': [],
    'browserless_token': '', 'browserless_token2': '', 'browserless_token3': '',
    'browserless_token4': '', 'browserless_timeout': 100, 'site_rules': '',
}
MIGRATION_VERSION = 1
_SITE_LOCKS = {}
_SITE_LOCKS_LOCK = Lock()


def selection_union(*values):
    """Normalize mixed saved IDs while retaining custom IDs and the dynamic all marker."""
    selected = []
    for value in values:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = re.split(r'[\s,;，；]+', value.strip())
        if not isinstance(value, (list, tuple, set)):
            value = [] if value is None else [value]
        for item in value:
            if str(item).strip().lower() == 'all':
                return ['all']
            if item is None or isinstance(item, bool) or not str(item).strip():
                continue
            item = int(item) if str(item).isdigit() else str(item).strip()
            if item not in selected:
                selected.append(item)
    return selected


def merge_config(current, ordinary, pt):
    """Migrate once; the enhanced schedule and explicit settings always take priority."""
    result = deepcopy(current or {})
    if result.get('consolidation_version') == MIGRATION_VERSION:
        result['browserless_sites'] = browserless_site_ids(result.get('browserless_sites'))
        return result
    if not result:
        result = deepcopy(ordinary or {})
        if not result and pt:
            result = {key: deepcopy(pt[key]) for key in ('enabled', 'notify', 'cron') if key in pt}
    # Only active sources contribute selections. A disabled plugin must not enlarge work.
    active_ordinary = ordinary if ordinary.get('enabled') else {}
    active_pt = pt if pt.get('enabled') else {}
    result['sign_sites'] = selection_union(result.get('sign_sites'),
        active_ordinary.get('sign_sites'), active_pt.get('site_ids'))
    result['login_sites'] = selection_union(result.get('login_sites'),
        active_ordinary.get('login_sites'), active_pt.get('login_site_ids'))
    for key, default in EXTRA_DEFAULTS.items():
        source = ordinary if key == 'rich_notify' else pt
        result.setdefault(key, deepcopy(source.get(key, default)))
    result.setdefault('queue_cnt', pt.get('concurrency') or ordinary.get('queue_cnt') or 5)
    result.setdefault('retry_time', '09:01')
    result['consolidation_version'] = MIGRATION_VERSION
    # Never replay another plugin's pending one-shot action during migration.
    result.setdefault('onlyonce', False)
    result.setdefault('clean', False)
    result['browserless_sites'] = browserless_site_ids(result.get('browserless_sites'))
    return result


def extend_form(form, defaults, current):
    """Append rule/browser controls to the existing enhanced configuration form."""
    form = deepcopy(form)
    defaults = {**EXTRA_DEFAULTS, 'retry_time': '09:01', **defaults}
    defaults.update({key: current[key] for key in defaults if key in current})
    defaults['browserless_tokens'] = '\n'.join(browserless_tokens(current))
    controls = [
        ('VSwitch', 'rich_notify', '分组通知', {}),
        ('VSwitch', 'read_activity', '读取最近动向', {}),
        ('VSwitch', 'quiz_ai_enabled', 'AI 辅助答题（沿用智能助手）', {}),
        ('VTextField', 'timeout', 'HTTP 超时（秒）', {'type': 'number', 'min': 5, 'max': 120}),
        ('VTextField', 'retry_count', '失败重试次数', {'type': 'number', 'min': 0, 'max': 5}),
        ('VTextField', 'retry_delay_minutes', '失败重试间隔（分钟）', {'type': 'number', 'min': 1, 'max': 1440}),
        ('VTextField', 'browserless_url', 'Browserless 地址', {}),
        ('VTextarea', 'browserless_tokens', 'Browserless Token 列表', {'rows': 3,
            'hint': '每行一个 Token，数量不限；空行和重复项会自动忽略。', 'persistent-hint': True}),
        ('VTextField', 'browserless_timeout', '浏览器超时（秒）', {'type': 'number', 'min': 15, 'max': 300}),
        ('VTextarea', 'site_rules', '自定义站点规则 JSON', {'rows': 5,
            'hint': '支持站点 ID、域名和 API Key；留空使用内置规则。', 'persistent-hint': True}),
    ]
    rows = [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'class': 'my-3'},
             'text': '答题优先使用已知答案或 AI；未配置时沿用原有签到方式，AI 仍可能答错。'}]
    rows.append({'component': 'VRow', 'content': [
        {'component': 'VCol', 'props': {'cols': 12, 'md': 12 if model == 'site_rules' else 6},
         'content': [{'component': kind, 'props': {'model': model, 'label': label, **props}}]}
        for kind, model, label, props in controls]})
    form[0]['content'].extend(rows)
    return form, defaults


def _data_items(value):
    """Handle host dictionary and PluginData-row return shapes."""
    if isinstance(value, dict):
        return value.items()
    return [(getattr(row, 'key', None), getattr(row, 'value', None)) for row in value or []]


def _history_site_names(host, collections):
    """Prefer IDs, and use a saved display name only when it identifies one site."""
    names = {}
    resolver = getattr(host, '_build_sites_info', None)
    if callable(resolver):
        for sid, name in (resolver() or {}).items():
            if sid is not None and name:
                names.setdefault(str(name), set()).add(str(sid))
    for items in collections:
        for _, value in items:
            for row in value if isinstance(value, list) else []:
                if isinstance(row, dict) and row.get('site_id') is not None and row.get('site'):
                    names.setdefault(str(row['site']), set()).add(str(row['site_id']))
    return names


def _history_site_id(row, names):
    """Do not assign a same-name historical record to an arbitrary current site."""
    if row.get('site_id') is not None and str(row['site_id']).strip():
        return str(row['site_id'])
    name = str(row.get('site') or row.get('site_name') or '').strip()
    candidates = names.get(name, set())
    if len(candidates) == 1:
        return next(iter(candidates))
    return 'legacy-name:' + name if name else ''


def _legacy_history_result(row, action):
    """Recover explicit old receipts; the old do list alone is not success evidence."""
    message = str(row.get('message') or row.get('status') or '').strip()
    failed = bool(re.search(r'失败|失敗|错误|錯誤|未成功|未检测到|未檢測到|未确认|需要重试|失效|超时', message))
    if isinstance(row.get('success'), bool):
        success = row['success']
    elif action == '签到':
        success = not failed and bool(re.search(r'签到成功|簽到成功|已签到|已簽到|已签过到|签到已得', message))
    else:
        success = not failed and bool(re.search(r'登录成功|登錄成功|登入成功', message))
    logged_in = bool(row.get('logged_in')) or success
    if not logged_in and not failed and re.search(r'登录成功|登錄成功|登入成功', message):
        logged_in = True
    return {'success': success, 'logged_in': logged_in, 'message': message or '旧记录未保存结果详情',
            'bonus': (reward_text(row.get('bonus')) or reward_text(message)) if success and action == '签到' else '',
            'activity': str(row.get('activity') or ''), 'engine': str(row.get('engine') or '旧版记录'),
            'site_name': str(row.get('site') or row.get('site_name') or ''),
            'signin_supported': row.get('signin_supported', not bool(re.search(r'无签到|无需签到|不支持签到', message)))}


def _merge_history_result(host, day, action, sid, result):
    """Keep confirmed completion and enrich it without losing enhanced receipts."""
    key = f'{action}-{day}'
    existing = host.get_data(key=key) or {}
    existing = existing if isinstance(existing, dict) else {}
    results = {str(k): v for k, v in (existing.get('results') or {}).items()}
    previous = results.get(sid) or {}
    if previous.get('success'):
        merged = dict(previous)
        if result.get('success'):
            for field in ('bonus', 'activity', 'site_name'):
                if not merged.get(field) and result.get(field):
                    merged[field] = result[field]
    elif result.get('success') or not previous or not previous.get('logged_in'):
        merged = dict(result)
    else:
        merged = dict(previous)
    results[sid] = merged
    retry = selection_union(existing.get('retry'))
    if merged.get('success'):
        retry = [item for item in retry if str(item) != sid]
    elif not sid.startswith('legacy-name:') and sid not in {str(item) for item in retry}:
        retry.append(int(sid) if sid.isdigit() else sid)
    # Unmatched old names stay visible, but are never interpreted as executable site IDs.
    done = selection_union(existing.get('do'), [] if sid.startswith('legacy-name:') else [sid])
    host.save_data(key=key, value={**existing, 'results': results, 'do': done, 'retry': retry})


def migrate_history(host, now=None):
    """Preserve enhanced/ordinary receipts and PT's last 14 days, once per schema."""
    if host.get_data(key='consolidation_history_v2'):
        return
    now = now or datetime.today()
    days = [(now - timedelta(days=offset)).strftime('%Y-%m-%d') for offset in range(14)]
    month_days = {f'{int(day[5:7])}月{int(day[8:10])}日': day for day in days}
    own_items = list(_data_items(host.get_data()))
    ordinary_items = list(_data_items(host.get_data(plugin_id='AutoSignIn')))
    pt_items = list(_data_items(host.get_data(plugin_id='PTSiteSignIn')))
    names = _history_site_names(host, (own_items, ordinary_items, pt_items))
    for source, items in [('AutoSignIn', ordinary_items), ('PTSiteSignIn', pt_items)]:
        for key, value in items:
            if key:
                host.save_data(key=f'legacy:{source}:{key}', value=value)
    # The enhanced plugin owns the existing keys. Merge raw ordinary rows first,
    # retaining the original month/day receipts and daily attempts for audit.
    for key, value in ordinary_items:
        if not key:
            continue
        if key in month_days and isinstance(value, list):
            existing = host.get_data(key=key) or []
            existing = existing if isinstance(existing, list) else [existing]
            merged = list(existing)
            for row in value:
                if row not in merged:
                    merged.append(row)
            host.save_data(key=key, value=merged)
        elif re.fullmatch(r'(?:签到|登录)-\d{4}-\d{2}-\d{2}', key) and isinstance(value, dict):
            existing = host.get_data(key=key) or {}
            result = {**value, **existing}
            result['results'] = {**value.get('results', {}), **existing.get('results', {})}
            result['do'] = selection_union(existing.get('do'), value.get('do'))
            result['retry'] = selection_union(existing.get('retry'), value.get('retry'))
            host.save_data(key=key, value=result)
    # Convert old month/day receipts; IDs did not exist in the earliest schema.
    # Only the recent window is mapped to a year, matching the original retention.
    for month_key, day in month_days.items():
        rows = host.get_data(key=month_key) or []
        for row in rows if isinstance(rows, list) else [rows]:
            if not isinstance(row, dict):
                continue
            sid = _history_site_id(row, names)
            if not sid:
                continue
            message = str(row.get('message') or row.get('status') or '')
            action = row.get('type') if row.get('type') in ('签到', '登录') else (
                '登录' if re.search(r'登录|登錄|登入', message) else '签到')
            _merge_history_result(host, day, action, sid, _legacy_history_result(row, action))
    # Preserve attempts even when an old version omitted the detailed receipt.
    # Unknown results remain explicit; do/retry were not a reliable success flag.
    for day in days:
        for action in ('签到', '登录'):
            existing = host.get_data(key=f'{action}-{day}') or {}
            for sid in selection_union(existing.get('do')):
                if str(sid) == 'all' or str(sid) in (existing.get('results') or {}):
                    continue
                saved_name = next((name for name, ids in names.items() if str(sid) in ids), '')
                _merge_history_result(host, day, action, str(sid), {
                    'success': False, 'logged_in': False, 'message': '旧记录已执行，未保存可确认的结果',
                    'bonus': '', 'activity': '', 'engine': '旧版记录', 'site_name': saved_name,
                    'unconfirmed': True})
    # Convert both PT's daily history and latest; latest alone drops earlier days.
    for key, value in pt_items:
        if not (key == 'latest' or re.fullmatch(r'history_\d{8}', str(key))) or not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            day = str(row.get('ran_at') or '')[:10]
            if day not in days:
                continue
            sid = _history_site_id(row, names)
            if not sid:
                continue
            status = row.get('status')
            action = '登录' if status in {'login', 'login_failed'} else '签到'
            converted = row_result(row, action)
            result = {'success': converted.success, 'logged_in': converted.logged_in,
                      'message': converted.message, 'bonus': converted.bonus,
                      'activity': converted.activity, 'engine': converted.engine,
                      'site_name': converted.site_name, 'signin_supported': getattr(converted, 'signin_supported', True)}
            _merge_history_result(host, day, action, sid, result)
            if action == '签到' and row.get('dual'):
                login_message = str(row.get('login') or '')
                login_ok = converted.success or (row.get('login_status') == 'login' and bool(
                    row.get('activity') or re.search(r'^成功|登录成功|登錄成功', login_message)))
                _merge_history_result(host, day, '登录', sid, {
                    'success': bool(login_ok), 'logged_in': bool(login_ok),
                    'message': login_message or ('签到任务已确认登录' if login_ok else '旧记录未确认登录'),
                    'bonus': '', 'activity': str(row.get('activity') or ''),
                    'engine': converted.engine, 'site_name': converted.site_name})
    host.save_data(key='consolidation_history_v1', value=True)
    host.save_data(key='consolidation_history_v2', value=True)


def row_result(row, action):
    """Convert PT's explicit statuses without treating cookie presence as login evidence."""
    status = str(row.get('status') or '')
    signin_supported = status != 'nosign' and row.get('signin_supported', True) is not False
    message = str(row.get('message') or '').strip()
    success = status in ({'success', 'already'} if action == '签到' else {'success', 'login'})
    logged_in = (success or row.get('logged_in') is True
                 or (row.get('login_status') == 'login' and bool(row.get('activity'))))
    if action == '签到':
        prefix = ('无签到功能' if not signin_supported else
                  '已签到' if status == 'already' else '签到成功' if success else
                  message.split('：', 1)[0] if status == 'pending' and message.startswith(('待开放：', '待确认：')) else
                  '待答题' if status == 'pending' else '签到失败')
    else:
        prefix = '登录成功' if success else '登录失败'
    if not message.startswith(prefix):
        message = f'{prefix}：{message or "未返回可确认的结果"}'
    if row.get('activity'):
        message += f'；最近动向：{row["activity"]}'
    return SiteResult(str(row.get('site') or ''), message, success, logged_in,
                      reward_text(row.get('bonus'), row.get('url', ''), row.get('message'))
                      if success and action == '签到' else '',
                      str(row.get('activity') or ''), str(row.get('engine') or ''), signin_supported)


def reward_text(value, url='', message=''):
    """Normalize real earned amounts and restore verified currency labels."""
    return normalize_reward(value, url, message)


class SignInBridge:
    """Use PT as primary; enhanced and ordinary modules cover its missing sites only."""

    def __init__(self, config):
        self.config = deepcopy(config)
        self.engine = RuleEngine(config)
        self.rules = parse_rules(config.get('site_rules') or '')
        self._signer = None
        self._lock = Lock()
        # Requests already in flight may outlive a configuration reload. Share
        # the guard with the next generation until those requests have returned.
        self._site_locks = _SITE_LOCKS
        self.stopped = False
        self._adapters = None

    def stop(self):
        """Invalidate this generation, leaving the next configuration independent."""
        self.stopped = True
        self.engine.stop()

    def _adapter(self, info):
        """Discover legacy handlers lazily without loading any other plugin entrypoint."""
        if self._adapters is None:
            modules = []
            for package in ('sites', 'standard_sites'):
                for path in sorted((Path(__file__).parent / package).glob('*.py')):
                    if path.name.startswith('__'):
                        continue
                    module = importlib.import_module(f'{__package__}.{package}.{path.stem}')
                    for value in vars(module).values():
                        if isinstance(value, type) and value.__module__ == module.__name__ and hasattr(value, 'match'):
                            modules.append((package, value))
            self._adapters = modules
        for package, adapter in self._adapters:
            try:
                if adapter.match(info.get('url') or ''):
                    return package, adapter
            except Exception:
                continue
        return None, None

    def route(self, info):
        """User rules and all PT-covered sites use PT; other adapters fill coverage gaps."""
        host = normalize_host(str(info.get('url') or ''))
        if any(key in self.rules for key in (str(info.get('id')), host, 'www.' + host)):
            return 'pt', None
        if host == 'yemapt.org':
            from .sites.yema import YemaPT
            return 'sites', YemaPT
        if (not str(info.get('cookie') or '').strip()
                and (info.get('apikey') or info.get('token'))):
            package, adapter = self._adapter(info)
            if adapter is not None and getattr(adapter, 'signin_supported', True) is False:
                return package, adapter
        if preset_for_host(host) is not None or site_module_for(info.get('url') or '') is not None:
            return 'pt', None
        package, adapter = self._adapter(info)
        return (package, adapter) if adapter is not None else ('pt', None)

    def _browser(self, info):
        """Share one token pool across this plugin generation."""
        if not self.engine._should_init_signer([info], self.rules):
            return None
        with self._lock:
            if self._signer is None and self.config.get('browserless_url'):
                self._signer = BrowserlessPool(self.config['browserless_url'],
                    self.engine._browserless_tokens(),
                    max(15, min(int(self.config.get('browserless_timeout') or 100), 300)), logger=logger)
            return self._signer

    @staticmethod
    def _enrich(info):
        """Include account notes/API credentials absent from legacy indexer dictionaries."""
        result = dict(info)
        for site in SiteOper().list_order_by_pri():
            if str(getattr(site, 'id', '')) != str(info.get('id')):
                continue
            for key in ('note', 'apikey', 'token', 'username', 'password'):
                value = getattr(site, key, None)
                if value is not None:
                    result[key] = value
            break
        return result

    def perform(self, info, action, legacy_login):
        """Execute one sign-in implementation, with login-only fallback after failure."""
        if self.stopped:
            return SiteResult(info.get('name'), f'{action}失败：任务已停止', False)
        key = str(info.get('id') or normalize_host(str(info.get('url') or '')))
        with _SITE_LOCKS_LOCK:
            lock = self._site_locks.setdefault(key, Lock())
        # A timed-out old worker cannot cause a second request to the same site.
        if not lock.acquire(blocking=False):
            return SiteResult(info.get('name'), f'{action}失败：该站点前次任务仍在执行', False)
        try:
            info = self._enrich(info)
            route, adapter = self.route(info)
            if route == 'pt':
                if action == '签到':
                    row = self.engine._sign_one(dict(info, dual=False), self.rules, self._browser(info))
                    result = row_result(row, action)
                    if (not result.success and row.get('browserless_not_started') is True
                            and not self.stopped and not self._has_custom_rule(info)):
                        host = normalize_host(str(info.get('url') or ''))
                        simple_sites = {'hhanclub.net', 'ptsbao.club', 'luckpt.de', 'pt.luckpt.de', 'hdkyl.in', 'dstudio.me'}
                        if host in simple_sites:
                            from .sites.nexusphp_attendance import NexusPhpAttendance
                            ok, message = NexusPhpAttendance.ordinary_receipt(info)
                            result = SiteResult(info.get('name'), message, ok, ok,
                                                reward_text(message) if ok else '', '', '站点 HTTP')
                        else:
                            package, local_adapter = self._adapter(info)
                            if (package == 'sites' and local_adapter is not None
                                    and getattr(local_adapter, 'ordinary_receipt_fallback', False) is True):
                                result = self._legacy_result(info, action, local_adapter)._replace(engine='站点 HTTP')
                else:
                    # PT's generic login treats cookie presence as success. Validate it
                    # through the strict legacy login path when no real login/API is used.
                    rule = resolve_rule(info, self.rules)
                    module = site_module_for(info.get('url') or '')
                    if (not account_ready(account_from_site(info)) and not rule.get('auth')
                            and not (module is not None and hasattr(module, 'login'))):
                        state, message = legacy_login(info)
                        activity = ''
                        if state is True and self.config.get('read_activity'):
                            activity, _ = self.engine._read_site_activity(info)
                        return SiteResult(info.get('name'), message, state is True, state is True, activity=activity)
                    result = row_result(self.engine._login_one(info), action)
            else:
                if action == '登录' and not hasattr(adapter, 'login'):
                    state, message = legacy_login(info)
                    result = SiteResult(info.get('name'), message, state is True, state is True)
                else:
                    result = self._legacy_result(info, action, adapter)
                if action == '签到' and result.success and info.get('cookie'):
                    result = result._replace(bonus=self.engine.read_header_reward(info) or result.bonus)
                if result.logged_in and self.config.get('read_activity'):
                    activity, _ = self.engine._read_site_activity(info)
                    result = result._replace(activity=activity)
            return result
        except Exception as error:
            logger.error(f'增强版签到：单站点执行异常（{type(error).__name__}）')
            return SiteResult(info.get('name'), f'{action}失败：执行异常，请查看日志', False)
        finally:
            lock.release()

    def _has_custom_rule(self, info):
        """自定义规则不自动改用另一套固定站点行为。"""
        host = normalize_host(str(info.get('url') or ''))
        return any(key in self.rules for key in (str(info.get('id')), host, 'www.' + host))

    @staticmethod
    def _legacy_result(info, action, adapter):
        """Preserve adapter booleans; login-only handlers never count as sign-in success."""
        method = getattr(adapter(), 'signin' if action == '签到' else 'login', None)
        if method is None:
            return SiteResult(info.get('name'), f'{action}失败：站点未提供该操作', False)
        state, message = method(info)
        message = str(message or '').strip()
        success = state is True and bool(message)
        supported = getattr(adapter, 'signin_supported', True) is not False
        login_only = action == '签到' and (
            not supported or (success and ('登录成功' in message or '无签到' in message)))
        if not success and '失败' not in message:
            message = f'{action}失败：{message or "未返回可确认的结果"}'
        return SiteResult(info.get('name'), message, success and not login_only, success,
                          reward_text(message, info.get('url', '')) if success and not login_only and action == '签到' else '',
                          '', '增强适配', supported and not login_only)
