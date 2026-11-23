"""邀请简报：变化优先，区分现有名额、兑换估算和未更新的历史记录。"""
from html import unescape
import re
import time

from app.plugins.ptinvitekeeper.host_compat import NotificationBlock
from app.plugins.ptinvitekeeper.host_compat import NotificationContent

from .ui import _row

_LIMIT = 6
_DETAIL_BUDGET = 2400


def _text(value, limit=160):
    value = unescape(str(value or ''))
    value = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', value, flags=re.I | re.S)
    value = re.sub(r'<[^>]+>', '', value)
    value = re.sub(r'https?://\S+', '[站点地址]', value, flags=re.I)
    value = re.sub(r'''\b(cookie|authorization)["']?\s*[:=]\s*[^\r\n]+''',
                   r'\1=[已隐藏]', value, flags=re.I)
    value = re.sub(r'''\b(token|passkey|api[_-]?key)["']?\s*[:=]\s*[^\s,;，；]+''',
                   r'\1=[已隐藏]', value, flags=re.I)
    value = re.sub(r'\b[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', '[邮箱已隐藏]', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:limit] + ('…' if len(value) > limit else '')


def _amount(value):
    """邀请数量须为非负整数，异常数值不能冒充零名额。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and value == int(value):
        return int(value)
    return None


def _quota(row):
    permanent, temporary = (_amount(row[key]) for key in ('permanent', 'temporary'))
    total_only = row['permanent'] is None and row['temporary'] is None
    total = _amount(row['quota']) if total_only else (
        None if permanent is None and temporary is None else (permanent or 0) + (temporary or 0))
    complete = total is not None and (total_only or (permanent is not None and temporary is not None))
    return permanent, temporary, total, complete


def _value(value):
    return '未知' if value is None else str(value)


def _quota_text(row):
    permanent, temporary, total, complete = _quota(row)
    if total is None:
        return '名额未知（站点未返回有效数量）'
    details = ('类型未返回' if permanent is None and temporary is None else
               f'永久 {_value(permanent)} · 临时 {_value(temporary)}')
    return f"{'现有' if complete else '已知'} {total} 个（{details}）"


def _exchange(row):
    if row['category'] not in ('invitable', 'quota'):
        return ''
    permanent = _amount(row['purchasable_permanent'])
    temporary = _amount(row['purchasable_temporary'] if row['purchasable_temporary'] is not None
                        else row['reported_purchasable_temporary'])
    choices = [f'{label} {count} 个' for label, count in [('永久', permanent), ('临时', temporary)] if count]
    return ' 或 '.join(choices)


def _section(blocks, title, entries, budget, note=''):
    """摘要与省略计数始终保留，所有段落共用详情预算。"""
    if not entries:
        return budget
    visible = []
    for entry in entries[:_LIMIT]:
        # 外部字段在插入模板前清理；不能再次把已隐藏的 Cookie 后面的建议删掉。
        entry = entry[:190] + ('…' if len(entry) > 190 else '')
        if len(entry) > budget:
            break
        visible.append(entry)
        budget -= len(entry)
    omitted = len(entries) - len(visible)
    if omitted:
        visible.append(f'另有 {omitted} 站，请在插件页面查看。')
    if note:
        visible.append(note)
    blocks.append(NotificationBlock(title=f'{title} · {len(entries)} 站', text='\n'.join(visible)))
    return budget


def _delta(label, before, after):
    if before == after:
        return ''
    suffix = f'（{after - before:+d}）' if before is not None and after is not None else ''
    return f'{label} {_value(before)} → {_value(after)}{suffix}'


def _changes(fresh, previous_records):
    """直接比较刷新前的完整记录，历史趋势的缺省零值不参与通知判断。"""
    entries, first_count = [], 0
    if previous_records is None:
        return entries, first_count
    for row in fresh:
        previous = previous_records.get(row['id'])
        if not previous:
            first_count += 1
            continue
        old = _row(row['id'], previous)
        if old['cached_after_failure']:
            # 失败记录已经覆盖资格状态，只说明读取恢复，不把旧缓存当作上轮实测。
            parts = ['读取恢复', row['label'], _quota_text(row)]
        else:
            parts = []
            if old['category'] != row['category']:
                parts.append(f"{old['label']} → {row['label']}")
            for key, label in [('banned', '已禁用'), ('low_ratio', '低分享率'), ('no_data', '分享率待确认')]:
                parts.append(_delta(label, old[key], row[key]))
            before, after = _quota(old), _quota(row)
            if before[:2] == (None, None) and after[:2] == (None, None):
                parts.append(_delta('名额', before[2], after[2]))
            else:
                for i, label in enumerate(('永久', '临时')):
                    parts.append(_delta(label, before[i], after[i]))
            parts.append(_delta('成员记录', old['members'], row['members']))
        parts = [part for part in parts if part]
        if parts:
            entries.append(f"• {_text(row['name'], 32)}：" + '；'.join(parts))
    return entries, first_count


def _failure_hint(reason):
    """常见故障给出下一步；未知原因仅保留经过清理的短摘要。"""
    text = str(reason or '')
    rules = [
        (r'未配置|缺少.?cookie|站点信息不完整', '缺少站点配置', '补全站点 Cookie 或 API 密钥'),
        (r'cloudflare|cf\s*验证|验证页', '防护验证未通过', '检查站点防护验证与浏览器服务'),
        (r'cookie.*(?:失效|过期)|未登录|登录.*失效|需要有效的网页登录会话|(?:http\s*|状态码[:： ]*)401',
         '登录状态失效', '重新登录并同步 Cookie'),
        (r'超时|timeout|timed\s*out', '请求超时', '检查网络后重试'),
        (r'http\s*429|频率|请求过多', '访问频率受限', '稍后重试，避免连续刷新'),
        (r'http\s*5\d\d|站点服务异常|维护', '站点暂不可用', '等待站点恢复后重试'),
        (r'http\s*403', '访问被拒绝', '检查站点访问权限与防护状态'),
        (r'网络错误|connection|解析域名|dns', '网络连接失败', '检查网络或代理后重试'),
    ]
    for pattern, label, advice in rules:
        if re.search(pattern, text, re.I):
            return f'{label}；{advice}'
    return f"{_text(text, 64) or '未返回原因'}；查看插件详情后重试"


def _status_hint(row):
    reason = row['reason']
    if re.search(r'二步验证|两步验证|双重验证|2fa|two.factor', reason, re.I):
        return '需启用两步验证；请在站点安全设置中开启'
    if row['category'] == 'unknown' and re.search(r'可购买|可兑换', reason):
        return '邀请资格未确认；页面有兑换估算，请先到站点确认邀请权限'
    if row['category'] != 'unknown':
        return _failure_hint(reason)
    return f"{_text(reason, 72)}；{_text(row['advice'], 64)}"


def _stamp(value):
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(value)) if value else '时间未知'
    except (ValueError, OverflowError, OSError, TypeError):
        return '时间未知'


def notification_plain_text(content):
    return '\n\n'.join('\n'.join(part for part in (block.title, block.text) if part)
                        for block in content.blocks)


def build_refresh_content(records, success_count, error_count, error_details=None,
                          site_names=None, targeted=False, previous_records=None, timestamp=None,
                          preview=False):
    """同一内容模型供 V3 分段内容和 V2 纯文本使用，不访问站点、不包含成员身份。"""
    records = records or {}
    errors = error_details or []
    names = list(dict.fromkeys(site_names if site_names is not None else
                              [*records, *(item['site_name'] for item in errors if item.get('site_name'))]))
    failure_reasons = {item['site_name']: item.get('msg') for item in errors if item.get('site_name') in names}
    rows = [_row(name, records[name]) for name in names if isinstance(records.get(name), dict)]
    for row in rows:
        if row['cached_after_failure']:
            failure_reasons.setdefault(row['id'], row['reason'])
    fresh = [row for row in rows if row['id'] not in failure_reasons]
    error_count = max(error_count, len(failure_reasons))
    eligible = sorted((row for row in fresh if row['category'] == 'invitable'),
                      key=lambda row: (-(_quota(row)[2] or 0), row['name']))
    owned = [row for row in eligible if (_quota(row)[2] or 0) > 0]
    pending = [row for row in fresh if row['needs_action']]
    risks = [row for row in fresh if row['banned'] or row['low_ratio']]
    attention = set(failure_reasons) | {row['id'] for row in pending + risks}
    changes, first_count = _changes(fresh, previous_records)
    if preview:
        headline = '通知预览'
    elif error_count and not success_count:
        headline = '本次读取失败'
    elif attention or error_count:
        headline = f'{len(attention) + max(0, error_count - len(failure_reasons))} 站需关注'
    elif changes:
        headline = f'{len(changes)} 站有变化'
    else:
        headline = '邀请数据已更新'

    now = time.time() if timestamp is None else timestamp
    scope = '缓存预览' if preview else ('指定刷新' if targeted else '本次刷新')
    summary = [f'{_stamp(now)} · {scope} {len(names)} 站']
    if preview:
        summary.extend(['使用已存记录，本次未访问站点。', f'可用缓存 {len(fresh)} 站 · 失败缓存 {error_count} 站'])
        times = [row['updated_at'] for row in fresh if row['updated_at']]
        if times:
            summary.append('缓存记录时间：' + _stamp(min(times)) +
                           (' ～ ' + _stamp(max(times)) if min(times) != max(times) else ''))
        if len(times) < len(fresh):
            summary.append(f'{len(fresh) - len(times)} 站未记录缓存时间。')
    else:
        summary.append(f'已更新 {success_count} 站 · 读取失败 {error_count} 站')
    if fresh:
        known = sum(_quota(row)[2] or 0 for row in eligible)
        incomplete = any(not _quota(row)[3] for row in eligible)
        summary.append(f"{'已知' if incomplete else '现有'}可用名额 {known} 个 · {len(owned)} 站持有")
        members = sum(row['members'] for row in fresh)
        summary.append(f'成员记录 {members} 条（分站统计，未跨站去重）')
    else:
        summary.append('没有可用的成功缓存，无法汇总名额与成员。' if preview else
                       '本次没有可汇总的成功记录，名额与成员数据未更新。')
    blocks = [NotificationBlock(text='\n'.join(summary))]
    budget = _DETAIL_BUDGET
    if previous_records is not None and not preview:
        note = f'首次记录 {first_count} 站，尚无历史数据可比较。' if first_count else ''
        budget = _section(blocks, '本次变化', changes, budget, note)
        if not changes and fresh:
            blocks.append(NotificationBlock(title='本次变化', text=note or
                         '与上次成功记录相比，邀请资格、名额及成员统计无变化。'))

    by_name = {row['id']: row for row in rows}
    failures = []
    for name, reason in failure_reasons.items():
        row = by_name.get(name)
        cached = (f"；上次成功 {_stamp(row['updated_at'])}" if row and row['cached_after_failure'] and row['updated_at']
                  else '；没有可确认的成功缓存')
        failures.append(f'• {_text(name, 32)}：{_failure_hint(reason)}{cached}')
    budget = _section(blocks, '读取失败', failures, budget)
    if error_count:
        text = '失败站点的历史缓存不计入名额与成员统计，也不据此判断资格或名额减少。'
        if error_count > len(failure_reasons):
            text += f'另有 {error_count - len(failure_reasons)} 站未返回失败详情。'
        blocks.append(NotificationBlock(text=text))
    budget = _section(blocks, '邀请状态待确认',
                      [f"• {_text(row['name'], 32)}：{_status_hint(row)}" for row in pending], budget)
    budget = _section(blocks, '成员需关注',
                      [f"• {_text(row['name'], 32)}：" + ' · '.join(
                          f'{label} {row[key]} 人' for key, label in [('banned', '已禁用'), ('low_ratio', '低分享率')] if row[key])
                       for row in risks], budget,
                      '请到站点核实成员状态；同一成员在同一站只归入一类。')
    budget = _section(blocks, '成员数据待确认',
                      [f"• {_text(row['name'], 32)}：分享率待确认 {row['no_data']} 人" for row in fresh if row['no_data']], budget,
                      '暂无有效分享率数据，不据此判为低分享率或账号禁用。')
    budget = _section(blocks, '现有可用名额',
                      [f"• {_text(row['name'], 32)}：{_quota_text(row)}" for row in owned], budget)
    budget = _section(blocks, '暂无名额或数量待确认',
                      [f"• {_text(row['name'], 32)}：{_quota_text(row)}" for row in eligible if row not in owned], budget)
    exchange = [f"• {_text(row['name'], 32)}：{_exchange(row)}" for row in fresh if _exchange(row)]
    budget = _section(blocks, '可兑换名额', exchange, budget,
                      '按当前魔力估算，同站两种方案共用余额，不能相加；以站点规则为准，未计入现有名额。')
    restricted = [row for row in fresh if row['category'] != 'invitable' and not row['needs_action']]
    _section(blocks, '邀请受限',
             [f"• {_text(row['name'], 32)}：{row['label']}" for row in restricted], budget,
             '受限站点的名额不计入可用总数。')
    blocks.append(NotificationBlock(text='完整结果与成员详情：MoviePilot → PT 邀请管家'))
    return NotificationContent(title=f'PT 邀请管家 · {headline}', blocks=blocks)


def build_refresh_notification(*args, **kwargs):
    content = build_refresh_content(*args, **kwargs)
    return content.title, notification_plain_text(content)


def send_preview(host):
    """显式预览操作使用现有缓存，不访问站点或改动定时任务。"""
    from app.schemas.types import NotificationType

    records = host.data_manager.get_site_data() or {}
    if not records:
        return {'success': False, 'message': '还没有可用于预览的邀请记录。'}
    failed = sum(_row(name, record)['cached_after_failure'] for name, record in records.items())
    content = build_refresh_content(records, len(records) - failed, failed, preview=True)
    host.post_message(mtype=NotificationType.SiteMessage, title=content.title,
                      text=notification_plain_text(content), rich_content=content)
    return {'success': True, 'message': '邀请通知预览已提交到现有通知渠道。'}
