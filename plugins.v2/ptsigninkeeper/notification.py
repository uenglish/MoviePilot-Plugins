"""汇总站点任务的语义结果，渠道能力、转义及消息分段由宿主处理。"""

import re
from html import unescape
from typing import List

from app.plugins.ptsigninkeeper.result import SiteResult, merge_failure_messages, signin_entry_unavailable
from app.plugins.ptsigninkeeper.host_compat import NotificationBlock
from app.plugins.ptsigninkeeper.host_compat import NotificationContent

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


def notification_title(action: str) -> str:
    return f"PT 签到管家 · {'签到结果' if action == '签到' else '登录检测结果'}"


def notification_result_changed(previous: object, current: dict) -> bool:
    """Avoid repeating an automatic retry notice when its visible result is unchanged."""
    if not isinstance(previous, dict):
        return True
    visible_fields = ("success", "message", "logged_in", "bonus", "signin_supported")
    return any(previous.get(key) != current.get(key) for key in visible_fields)


def _site_names(rows, limit=8):
    names = [_compact(result.site_name, 40) for _, result in rows]
    text = '、'.join(names[:limit])
    if len(names) > limit:
        text += f' 等 {len(names)} 站'
    return text


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


def _group_results(rows: list) -> list[str]:
    """合并相同原因并限制站点名数量，避免大量失败挤占通知首屏。"""
    reasons: dict[str, list[str]] = {}
    for _, result in rows:
        message = result.message if result.completed else _failure_reason(result.message)
        reason = re.sub(r"\s+", " ", str(message)).strip()
        name = _compact(result.site_name, 32)
        reasons.setdefault(reason, []).append(name)
    entries = []
    for reason, names in reasons.items():
        sites = '、'.join(names[:4])
        if len(names) > 4:
            sites += f' 等 {len(names)} 站'
        entries.append(f'• {sites}：{_compact(reason, 100)}')
    return entries


def build_notification_content(day: str, action: str, results: List[SiteResult],
                               total: int, retry_ids: list, site_ids: list) -> NotificationContent:
    """首屏摘要、异常优先、成功合并；完整回执和奖励留在插件详情中。"""
    rows = list(zip(site_ids, results))
    unavailable = [(sid, result) for sid, result in rows if action == '签到' and not result.completed and signin_entry_unavailable(result.message)]
    ordinary = [(sid, result) for sid, result in rows if (sid, result) not in unavailable]
    failed = [(sid, result) for sid, result in ordinary if not result.completed and not (action == '签到' and result.logged_in)]
    signin_failed = [(sid, result) for sid, result in ordinary if action == '签到' and not result.completed and result.logged_in]
    login_only = [(sid, result) for sid, result in rows if result.completed and result.logged_in and not result.success]
    succeeded = [(sid, result) for sid, result in rows if result.success]
    success_label = '签到成功' if action == '签到' else '登录成功'
    count_text = f'{success_label} {len(succeeded)} · 仅登录 {len(login_only)} · 失败 {len(failed) + len(signin_failed)}'
    if unavailable:
        count_text += f'\n签到入口暂不可用 {len(unavailable)} 站'
    if action != '签到':
        count_text = f'登录成功 {len(succeeded) + len(login_only)} · 失败 {len(failed)}'
    blocks = [NotificationBlock(text=f'{day} · 本次 {len(results)} / 已选 {total} 站\n{count_text}')]
    for label, items in [('⚠ 登录或访问失败', failed), ('⚠ 签到失败 · 登录有效', signin_failed)]:
        if not items:
            continue
        groups = _group_results(items)
        text = '\n'.join(groups[:6])
        if len(groups) > 6:
            text += f'\n另有 {len(groups) - 6} 类异常，详见插件页面。'
        blocks.append(NotificationBlock(title=f'{label} · {len(items)} 站', text=text))
    if unavailable:
        entries = [f"• {_compact(result.site_name, 32)}：{'登录已确认' if result.logged_in else '登录未确认'}"
                   for _, result in unavailable[:8]]
        if len(unavailable) > 8:
            entries.append(f'另有 {len(unavailable) - 8} 站，详见插件页面。')
        entries.append('入口未找到（HTTP 404），可能暂时关闭或地址变更；后续任务会重新检查。')
        blocks.append(NotificationBlock(title=f'⚠ 签到入口暂不可用 · {len(unavailable)} 站', text='\n'.join(entries)))
    if login_only:
        blocks.append(NotificationBlock(title=f'无签到功能 · 登录有效 · {len(login_only)} 站', text=_site_names(login_only)))
    if succeeded:
        reward_count = sum(result.bonus is not None and str(result.bonus).strip() != '' for _, result in succeeded)
        text = _site_names(succeeded) if len(succeeded) <= 8 else ''
        if action == '签到':
            rewards = f'已返回奖励 {reward_count} 站'
            if reward_count < len(succeeded):
                rewards += f' · 未返回 {len(succeeded) - reward_count} 站'
            text = '\n'.join(part for part in (text, rewards) if part)
        blocks.append(NotificationBlock(title=f'✅ {success_label} · {len(succeeded)} 站', text=text))
    footer = f'待重试 {len(retry_ids)} 站。\n' if retry_ids else ''
    if action != '签到':
        footer += '登录有效不等于保活已确认，请结合最近访问记录与站点规则。\n'
    blocks.append(NotificationBlock(text=footer + '完整结果：MoviePilot → PT 签到管家'))
    return NotificationContent(title=notification_title(action), blocks=blocks)


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
    args = (day, '签到', list(results), len(rows), [], list(ids))
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
