"""Bounded invitation summaries for MoviePilot's native JSON dashboard."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings
from . import ui


def _text(value, css='text-body-2'):
    return {'component': 'div', 'props': {'class': css}, 'text': str(value)}


def _updated(value):
    if value is None:
        return '尚无成功更新时间'
    try:
        return '最近成功记录：' + datetime.fromtimestamp(value, ZoneInfo(settings.TZ)).strftime('%m-%d %H:%M')
    except (ValueError, TypeError, OSError, OverflowError, KeyError):
        return '成功更新时间未知'


def render(host):
    """Read the local snapshot only, without member identities or account fields."""
    view = ui.list_sites(host, sort='status', page_size=3)
    summary = view['summary']
    if not summary['sites']:
        return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
                 'text': '暂无邀请数据，请在插件页面执行一次刷新。'}]
    metrics = [('站点记录', summary['sites']), ('可邀请', summary['invitable']),
               ('需处理', summary['problem']), ('已知持有名额', summary['quota'])]
    nodes = [{'component': 'div', 'props': {'class': 'keeper-summary',
              'style': 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px'},
              'content': [{'component': 'div', 'content': [
                  {'component': 'strong', 'text': str(value)},
                  _text(label, 'text-caption text-medium-emphasis')]}
                  for label, value in metrics]}]
    if view['refreshing']:
        nodes.append(_text('正在刷新，以下为已保存的结果。', 'text-caption text-medium-emphasis'))
    for row in view['items']:
        cached = row['cached_after_failure']
        quota = str(row['quota']) if row['quota'] is not None else '未知'
        nodes.extend([{'component': 'VDivider', 'props': {'class': 'my-3'}},
            {'component': 'div', 'props': {'class': 'd-flex align-center ga-2'}, 'content': [
                {'component': 'strong', 'text': row['name']}, {'component': 'VSpacer'},
                {'component': 'VChip', 'props': {'color': 'warning' if cached else row['color'],
                 'size': 'small', 'variant': 'tonal'},
                 'text': '刷新失败' if cached else row['label']}]},
            _text(f"{'上次记录名额' if cached else '剩余名额'}：{quota} · 成员记录：{row['members']}")])
        if cached or row['stale']:
            nodes.append(_text('保留上次成功数据，请重新刷新核验。' if cached else '数据较旧，请刷新核验。',
                               'text-caption text-medium-emphasis'))
    nodes.append(_text(f"成员记录 {summary['members']} · 分享率偏低 {summary['low_ratio']} · "
                       f"已封禁 {summary['banned']}", 'text-caption mt-3'))
    nodes.append(_text('名额仅汇总已读取数量；刷新失败的旧记录不计入汇总。',
                       'text-caption text-medium-emphasis'))
    nodes.append(_text(_updated(view['updated_at']), 'text-caption text-medium-emphasis'))
    nodes.append(_text(f"展示 {len(view['items'])} / {summary['sites']} 站，完整记录见插件页面",
                       'text-caption text-medium-emphasis'))
    return nodes
