"""Read-only homepage widget using MoviePilot's native JSON component schema."""
from . import ui


def _text(value, css='text-body-2'):
    return {'component': 'div', 'props': {'class': css}, 'text': str(value)}


def render(host):
    """Reuse the daily view model; opening the widget never visits a site."""
    view = ui.list_sites(host, sort='status', page_size=3)
    summary = view['summary']
    if not summary['total_sites']:
        return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
                 'text': '暂无启用站点的记录，请在插件设置中选择签到或登录站点。'}]
    metrics = [('签到成功', summary['success']), ('登录成功', summary['logged_in']),
               ('需处理', summary['failed']), ('待完成', summary['pending'])]
    nodes = [{'component': 'div', 'props': {'class': 'keeper-summary',
              'style': 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px'},
              'content': [{'component': 'div', 'content': [
                  {'component': 'strong', 'text': str(value)},
                  _text(label, 'text-caption text-medium-emphasis')]}
                  for label, value in metrics]}]
    if view['refreshing']:
        nodes.append(_text('任务执行中，以下为已保存的结果。', 'text-caption text-medium-emphasis'))
    for row in view['items']:
        nodes.extend([{'component': 'VDivider', 'props': {'class': 'my-3'}},
            {'component': 'div', 'props': {'class': 'd-flex align-center ga-2'}, 'content': [
                {'component': 'strong', 'text': row['name']}, {'component': 'VSpacer'},
                {'component': 'VChip', 'props': {'color': row['color'], 'size': 'small',
                 'variant': 'tonal'}, 'text': row['label']}]},
            _text(f"签到：{row['signin_status']['label']} · 登录：{row['login_status']['label']}")])
        if row['signin_selected'] or row['signin_success']:
            nodes.append(_text('奖励：' + row['bonus'], 'text-caption text-medium-emphasis'))
    nodes.append(_text(f"{view['date']} · {summary['total_sites']} 个站点 · "
                       f"展示 {len(view['items'])} 站，完整记录见插件页面",
                       'text-caption text-medium-emphasis mt-3'))
    return nodes
