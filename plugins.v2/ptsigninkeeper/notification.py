"""汇总站点任务的语义结果，渠道能力、转义及消息分段由宿主处理。"""

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from html import unescape
from typing import List

from app.plugins.ptsigninkeeper.result import SiteResult, merge_failure_messages, signin_entry_unavailable
from app.plugins.ptsigninkeeper.host_compat import NotificationBlock
from app.plugins.ptsigninkeeper.host_compat import NotificationContent

_BATCH = ContextVar('pt_signin_notification_batch', default=None)


def _deliver(host, batch):
    if not getattr(host, '_notify', False) or not batch['changed'] or not batch['batches']:
        return
    if batch['generation'] != getattr(host, '_generation', None):
        return
    from app.schemas.types import NotificationType
    content = build_run_notification(batch['day'], batch['batches'], len(batch['selected']),
                                     len(batch['retry']), time.monotonic() - batch['started'])
    extra = {'rich_content': content} if host._rich_notify else {}
    host.post_message(mtype=NotificationType.SiteMessage, title=content.title,
                      text=notification_plain_text(content), **extra)


def _new_batch(host, day):
    return {'host': host, 'day': day, 'batches': [], 'selected': set(), 'retry': set(),
            'changed': False, 'started': time.monotonic(), 'generation': getattr(host, '_generation', None)}


@contextmanager
def notification_batch(host, day):
    """同轮签到和登录合并发送；异常或配置重载时不发送误导性的完成通知。"""
    batch = _new_batch(host, day)
    token = _BATCH.set(batch)
    try:
        yield
        _deliver(host, batch)
    finally:
        _BATCH.reset(token)


def queue_notification(host, day, action, results, site_ids, selected_ids, retry_ids, changed):
    """收集真实执行结果；单站点 API 无批次时仍可独立通知。"""
    batch = _BATCH.get()
    standalone = batch is None or batch['host'] is not host
    if standalone:
        batch = _new_batch(host, day)
    batch['batches'].append((action, results, site_ids))
    batch['selected'].update(map(str, selected_ids))
    batch['retry'].update(map(str, retry_ids))
    batch['changed'] |= changed
    if standalone:
        _deliver(host, batch)

_LOGIN_FAILURE = "Cookie / Token / 登录状态异常"
_UNCONFIRMED_LOGIN = r"未识别到登录状态|未确认(?:到)?登录状态"
# CSRF token 是页面提交参数，不是登录凭据，不能因同含 Token 而归为登录失效。
_CREDENTIAL = r"(?<![a-z_])(?:cookie|(?<!csrf-)token|authorization|api[-_]?key)"
_HTTP_STATUS = r"(?:状态码|http(?:status(?:code)?)?|status(?:code)?)[:：=]?"
_UNCONFIRMED_RESULT = (r"未确认(?:到)?签到(?:结果|成功状态)|接口未确认访问时间更新|"
                       r"签到(?:页)?状态未知|未返回可确认的结果|签到接口未返回明确的成功或重复签到结果")

# 明确原因优先于泛化的登录证据缺失；404 可能只是签到入口不存在，不能拆散同类登录失败。
_FAILURE_RULES = (
    ("站点配置缺失", rf"(?:未配置|未填写|未设置|未提供|缺少)[^；;]*(?:站点(?:地址|信息)|{_CREDENTIAL})|"
                 rf"(?:站点(?:地址|信息)|{_CREDENTIAL})(?:为空|未配置|缺失|未填写)"),
    (_LOGIN_FAILURE, rf"{_CREDENTIAL}(?:登录(?:态|状态|信息)|格式)?(?:已|已经)?"
                     r"(?:失效|过期|无效|不正确|不对|错误|权限不足|(?:is)?(?:invalid|expired))|"
                     r"(?:登录态|登录状态|登录凭据)(?:已|已经)?(?:失效|过期|无效)|"
                     r"未(?:登录|登陆)|(?:请|需要|必须)(?:先|重新)?(?:登录|登陆)|"
                     r"(?:invalid|expired)(?:cookie|token)|unauthorized|fullauthenticationisrequired|"
                     rf"authenticationrequired|{_HTTP_STATUS}401(?!\d)"),
    ("Cloudflare 防护验证失败", r"cloudflare"),
    ("验证码获取或校验失败", r"验证码|captcha|invalid_imagehash"),
    ("网络请求超时", rf"超时|timeout|timedout|{_HTTP_STATUS}408(?!\d)"),
    ("网络连接失败", r"无法(?:打开|访问)(?:网站|签到页)|站点连通性|请求无响应|"
                   r"connection(?:error|refused|reset|aborted)|nameresolutionerror|failedtoresolve|"
                   r"(?:连接|域名解析|dns解析)(?:失败|错误)"),
    ("站点访问被拒绝（HTTP 403）", rf"{_HTTP_STATUS}403(?!\d)"),
    ("站点访问频率受限（HTTP 429）", rf"{_HTTP_STATUS}429(?!\d)"),
    ("站点服务异常（HTTP 5xx）", rf"{_HTTP_STATUS}5\d{{2}}(?!\d)"),
    ("站点页面或接口解析异常", r"空页面|空响应|接口(?:返回)?数据异常|csrf|"
                          r"未获取到(?:相关)?签到(?:参数|问题|图片)|未获取到(?:答案选项|匹配答案)|获取签到参数失败"),
)


def _compact(text: str, limit: int) -> str:
    """压缩站点结果的换行并限制展示长度，完整原文保留在插件详情。"""
    text = unescape(str(text))
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "[站点地址]", text, flags=re.I)
    text = re.sub(r"\b(cookie|authorization)\s*[:=]\s*[^\r\n]+", r"\1=[已隐藏]", text, flags=re.I)
    text = re.sub(r"\b(token|passkey|api[_-]?key)\s*[:=]\s*[^\s,;，；]+", r"\1=[已隐藏]", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def notification_result_changed(previous: object, current: dict) -> bool:
    """Avoid repeating an automatic retry notice when its visible result is unchanged."""
    if not isinstance(previous, dict):
        return True
    visible_fields = ("success", "message", "logged_in", "bonus", "signin_supported")
    return any(previous.get(key) != current.get(key) for key in visible_fields)


def notification_plain_text(content: NotificationContent) -> str:
    """所有纯文本渠道保留同样的摘要、段落和阅读顺序。"""
    return '\n\n'.join('\n'.join(part for part in (block.title, block.text) if part)
                        for block in content.blocks)


def build_notification_text(day: str, action: str, results: List[SiteResult],
                            total: int, retry_ids: list, site_ids: list) -> str:
    """普通通知按站点列出异常，成功结果合并，避免重复回执淹没重点。"""
    content = build_notification_content(day, action, results, total, retry_ids, site_ids)
    return notification_plain_text(content)


def _failure_reason(message: str) -> str:
    """
    为通知摘要选择稳定的失败类别，不改写历史中的真实错误和重试依据。

    登录证据缺失不等于 Cookie 已失效，因此共用中性标题；明确防护、网络和页面错误仍独立分类。
    未知业务错误保留完整原因作分组键，不能因为展示截断或包含 Cookie 字样而误合并。

    :param message: 站点执行结果中的原始失败文案
    :return: 通知展示类别，无法识别时返回去重后的原始原因
    """
    reason = re.sub(r"\s+", " ", merge_failure_messages(message)).strip()
    key = re.sub(r"\s+", "", reason)
    for label, pattern in _FAILURE_RULES:
        if re.search(pattern, key, re.IGNORECASE):
            return label
    status_codes = sorted(set(re.findall(rf"{_HTTP_STATUS}([1-5]\d{{2}})(?!\d)", key, re.IGNORECASE)))
    # 没有更具体的 HTTP 错误时才使用登录证据缺失；签到入口 404 可作为伴随错误。
    if re.search(_UNCONFIRMED_LOGIN, key) and all(code == "404" for code in status_codes):
        return _LOGIN_FAILURE
    if status_codes:
        return f"HTTP 状态异常（{' / '.join(status_codes)}）"
    if re.search(_UNCONFIRMED_RESULT, key, re.IGNORECASE):
        return "站点返回结果未确认"
    return reason or "未返回具体失败原因"


def _result_rows(batches):
    """按站点合并同轮签到与登录回执，保持一个站点只出现一次。"""
    rows = {}
    for action, results, ids in batches:
        for sid, result in zip(ids, results):
            row = rows.setdefault(str(sid), {'name': result.site_name})
            row['sign' if action == '签到' else 'login'] = result
    return list(rows.values())


def _waiting(result):
    return bool(result and not result.completed and result.message.startswith(('待开放：', '待确认：', '待答题：')))


def _reason_help(reason):
    if reason == _LOGIN_FAILURE or reason == '站点配置缺失':
        return '检查站点管理中的凭据与登录状态'
    if 'Cloudflare' in reason or '验证码' in reason:
        return '检查站点验证与浏览器设置'
    if '网络' in reason or 'HTTP 5' in reason or '频率' in reason:
        return '稍后重试，持续失败时检查站点连通性'
    if '入口' in reason:
        return '检查站点是否更改或暂时关闭签到入口'
    if 'use_session' in reason or '执行出现未预期错误' in reason:
        return '检查插件版本并查看错误日志'
    return '查看插件详情中的完整回执'


def build_run_notification(day, batches, selected_total, retry_count, elapsed_seconds=None):
    """一轮一条通知：总览、需要处理、等待、奖励与已完成站点。"""
    rows = _result_rows(batches)
    signed, accounts, issues, waiting = [], [], [], []
    logged = 0
    for row in rows:
        sign, login = row.get('sign'), row.get('login')
        row['logged'] = bool((login and login.success) or (sign and (sign.success or sign.logged_in)))
        logged += row['logged']
        if sign and sign.success:
            signed.append(row)
        elif sign and sign.completed:
            accounts.append(row)
        elif _waiting(sign):
            waiting.append(row)
        elif sign:
            issues.append(row)
        elif login and login.success:
            accounts.append(row)
        else:
            issues.append(row)
    summary = [f'{day} · 本轮处理 {len(rows)} / 已选 {selected_total} 站',
               f'签到完成 {len(signed)} · 仅登录 {len(accounts)}',
               f'需处理 {len(issues)} · 等待 {len(waiting)} · 登录有效 {logged}']
    if elapsed_seconds is not None:
        seconds = max(0, int(elapsed_seconds))
        summary.append(f'耗时 {seconds // 60} 分 {seconds % 60} 秒' if seconds >= 60 else f'耗时 {seconds} 秒')
    blocks = [NotificationBlock(text='\n'.join(summary))]
    if issues:
        groups = {}
        for row in issues:
            result = row.get('sign') or row.get('login')
            reason = ('签到入口暂不可用' if row.get('sign') and signin_entry_unavailable(result.message)
                      else _failure_reason(result.message))
            groups.setdefault(reason, []).append(row)
        parts = []
        for reason, items in list(groups.items())[:5]:
            names = '、'.join(_compact(r['name'], 24) for r in items[:4])
            if len(items) > 4:
                names += f' 等 {len(items)} 站'
            sessions = sum(r['logged'] for r in items)
            parts.append(f'• {_compact(reason, 64)} · {len(items)} 站\n  {names}' +
                         (f'（{sessions} 站登录有效）' if sessions else '') +
                         f'\n  {_reason_help(reason)}')
        if len(groups) > 5:
            parts.append(f'另有 {len(groups) - 5} 类问题，见插件详情')
        blocks.append(NotificationBlock(title='需要处理', text='\n'.join(parts)))
    if waiting:
        lines = [f"• {_compact(r['name'], 24)}：{_compact(r['sign'].message, 90)}" for r in waiting[:4]]
        if len(waiting) > 4:
            lines.append(f'另有 {len(waiting) - 4} 站等待处理')
        blocks.append(NotificationBlock(title='等待后续处理', text='\n'.join(lines)))
    if signed:
        rewards = [r for r in signed if r['sign'].bonus not in (None, '') and not isinstance(r['sign'].bonus, bool)]
        lines = [f"• {_compact(r['name'], 24)} · {_compact(r['sign'].bonus, 40)}" for r in rewards[:6]]
        if len(rewards) > 6:
            lines.append(f'另有 {len(rewards) - 6} 站返回奖励，见插件详情')
        missing = len(signed) - len(rewards)
        if missing:
            names = [_compact(r['name'], 24) for r in signed if r not in rewards]
            lines.append(f'另 {missing} 站已完成，未返回奖励数额' + ('：' + '、'.join(names) if missing <= 4 else ''))
        lines.append('奖励按站点分别记录，已签到回执不代表本轮新增收益')
        blocks.append(NotificationBlock(title=f'签到与奖励 · {len(signed)} 站', text='\n'.join(lines)))
    if accounts:
        names = '、'.join(_compact(r['name'], 24) for r in accounts[:6])
        if len(accounts) > 6:
            names += f' 等 {len(accounts)} 站'
        blocks.append(NotificationBlock(title=f'仅登录 · {len(accounts)} 站',
            text=names + '\n登录有效不代表访问时间已更新；保活请查看最近访问记录'))
    footer = f'本轮未完成 {retry_count} 站；后续按已配置的计划检查。\n' if retry_count else ''
    blocks.append(NotificationBlock(text=footer + '完整结果与历史：MoviePilot → PT 签到管家'))
    state = '需要关注' if issues else '等待后续处理' if waiting else '本轮完成'
    return NotificationContent(title=f'PT 签到管家 · {state}', blocks=blocks)


def build_notification_content(day: str, action: str, results: List[SiteResult],
                               total: int, retry_ids: list, site_ids: list) -> NotificationContent:
    return build_run_notification(day, [(action, results, site_ids)], total, len(set(map(str, retry_ids))))


def send_preview(host):
    """仅使用当天缓存演示真实通知模板，不执行签到、不修改通知开关。"""
    from . import ui
    from app.schemas.types import NotificationType

    day = ui._today().isoformat()
    history = host.get_data(key=f'签到-{day}') or {}
    records = history.get('results') or {}
    logins = (host.get_data(key=f'登录-{day}') or {}).get('results') or {}
    metadata = ui._metadata(host)
    rows = [(str(sid), SiteResult(
        metadata.get(str(sid), {}).get('name', f'站点 {sid}'), str(value.get('message') or ''),
        value.get('success') is True, value.get('logged_in') is True or
        (logins.get(str(sid), {}).get('success') is True and not logins.get(str(sid), {}).get('unconfirmed')),
        value.get('bonus', ''), value.get('activity') or '', value.get('engine') or '',
        value.get('signin_supported') is not False)) for sid, value in records.items() if isinstance(value, dict)]
    if not rows:
        return {'success': False, 'message': '今天还没有可用于预览的签到记录。'}
    ids, results = zip(*rows)
    args = (day, '签到', list(results), len(rows),
            [sid for sid, result in rows if not result.completed], list(ids))
    title = 'PT 签到管家 · 排版预览'
    note = '新版排版预览 · 使用当天缓存，本次未执行签到。'
    extra = {}
    if host._rich_notify:
        content = build_notification_content(*args)
        content.title = title
        content.blocks.insert(0, NotificationBlock(text=note))
        extra['rich_content'] = content
    host.post_message(mtype=NotificationType.SiteMessage, title=title,
                      text=note + '\n\n' + build_notification_text(*args), **extra)
    return {'success': True, 'message': '签到排版预览已提交到现有通知渠道。'}
