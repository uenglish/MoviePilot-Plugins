"""邀请结果通知：统计本次读取成功的数据，失败缓存不冒充最新结果。"""
from html import unescape
import re
import time

from .ui import _row

_LIMIT = 8


def _text(value, limit=160):
    value = unescape(str(value or ''))
    value = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', value, flags=re.I | re.S)
    value = re.sub(r'<[^>]+>', '', value)
    value = re.sub(r'https?://\S+', '[站点地址]', value, flags=re.I)
    value = re.sub(r'\b(cookie|authorization)\s*[:=]\s*[^\r\n]+', r'\1=[已隐藏]', value, flags=re.I)
    value = re.sub(r'\b(token|passkey|api[_-]?key)\s*[:=]\s*[^\s,;，；]+', r'\1=[已隐藏]', value, flags=re.I)
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:limit] + ('…' if len(value) > limit else '')


def _section(lines, title, entries):
    if not entries:
        return
    lines.extend(['', f'{title} · {len(entries)} 项', *entries[:_LIMIT]])
    if len(entries) > _LIMIT:
        lines.append(f'另有 {len(entries) - _LIMIT} 项，请在插件页面查看。')


def build_refresh_notification(records, success_count, error_count, error_details=None,
                               site_names=None, targeted=False, diff=None, timestamp=None):
    """只汇总指定刷新范围；消息不包含成员身份，错误详情限制长度。"""
    names = list(dict.fromkeys(site_names if site_names is not None else records))
    errors = error_details or []
    failed_names = {item.get('site_name') for item in errors}
    rows = [_row(name, records[name]) for name in names if name in records]
    fresh = [row for row in rows if not row['cached_after_failure'] and row['name'] not in failed_names]
    now = time.time() if timestamp is None else timestamp
    invitable = sorted((row for row in fresh if row['category'] == 'invitable'),
                       key=lambda row: -(row['quota'] or 0))
    known = sum(row['quota'] or 0 for row in invitable)
    members = sum(row['members'] for row in fresh)
    attention = sum(row[key] for row in fresh for key in ('banned', 'low_ratio', 'no_data'))
    lines = [time.strftime('%Y-%m-%d %H:%M', time.localtime(now)) +
             f" · {'指定站点' if targeted else '全部站点'} {len(names)} 站",
             f'读取成功 {success_count} · 失败 {error_count}']
    if fresh:
        lines.append(f'可邀请 {len(invitable)} 站 · 已知可用名额 {known:g} 个')
        if members:
            lines.append(f'已记录成员 {members} 人' + (f' · 需关注 {attention} 人' if attention else ''))
    _section(lines, '⚠ 读取失败', [f"• {_text(item.get('site_name'), 32)}：{_text(item.get('msg'), 100)}" for item in errors])
    if error_count:
        lines.append('失败站点保留历史缓存，不计入本次名额与成员统计。')
    if fresh:
        pending = [row for row in fresh if row['needs_action']]
        _section(lines, '⚠ 邀请状态待处理',
                 [f"• {_text(row['name'], 32)}：{_text(row['reason'], 100)}" for row in pending])
        member_entries = []
        for row in fresh:
            details = [f'{label} {row[key]} 人' for key, label in
                       [('banned', '已禁用'), ('low_ratio', '低分享率'), ('no_data', '数据不全')]
                       if row[key]]
            if details:
                member_entries.append(f"• {_text(row['name'], 32)}：" + ' · '.join(details))
        _section(lines, '⚠ 成员需关注', member_entries)
        quota_entries = []
        for row in invitable:
            quota = '名额未返回' if row['quota'] is None else f"{row['quota']:g} 个"
            if row['temporary']:
                quota += f"（含临时 {row['temporary']:g}）"
            quota_entries.append(f"• {_text(row['name'], 32)}：{quota}")
        _section(lines, '✅ 可邀请站点', quota_entries)
        unknown = sum(row['quota'] is None for row in invitable)
        incomplete = sum(row['quota'] is not None and
                         (row['permanent'] is None or row['temporary'] is None) for row in invitable)
        if unknown:
            lines.append(f'{unknown} 站名额未返回，未计入可用名额。')
        if incomplete:
            lines.append(f'{incomplete} 站名额明细不全，合计仅包含已知数值。')
        counts = {}
        for row in fresh:
            if row['category'] != 'invitable' and not row['needs_action']:
                counts[row['label']] = counts.get(row['label'], 0) + 1
        if counts:
            lines.extend(['', '其他站点 · 受站点规则限制',
                          ' · '.join(f'{label} {count} 站' for label, count in counts.items())])
    else:
        lines.extend(['', '本次没有读取成功的站点，名额与成员数据未更新。'])
    changes = []
    for key, label in [('new_problem', '新增异常'), ('recovered', '状态恢复'), ('invitable', '恢复可邀请'), ('changed', '名额或成员变化')]:
        for item in (diff or {}).get(key, []):
            if item.get('name') in names:
                changes.append(f"• {label}：{_text(item['name'], 32)}")
    _section(lines, '本次变化', changes)
    lines.extend(['', '完整结果：MoviePilot → PT 邀请管家'])
    return 'PT 邀请管家 · 邀请数据更新', '\n'.join(lines)


def send_preview(host):
    """用现有缓存发送模板预览，不访问站点或改动定时任务。"""
    from app.schemas.types import NotificationType

    records = host.data_manager.get_site_data() or {}
    if not records:
        return {'success': False, 'message': '还没有可用于预览的邀请记录。'}
    rows = [_row(name, record) for name, record in records.items()]
    errors = [{'site_name': row['name'], 'msg': row['reason']} for row in rows if row['cached_after_failure']]
    _, text = build_refresh_notification(records, len(rows) - len(errors), len(errors), errors)
    note = '新版排版预览 · 使用已存缓存，本次未访问站点。\n各站缓存时间见插件页面。'
    host.post_message(mtype=NotificationType.SiteMessage, title='PT 邀请管家 · 排版预览', text=note + '\n\n' + text)
    return {'success': True, 'message': '邀请排版预览已提交到现有通知渠道。'}
