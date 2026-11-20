"""Nonblocking UI actions using the same plugin runner and daily deduplication."""
from datetime import datetime
from threading import Lock, Thread
import time

_ACTION_LOCK = Lock()


def start_run(host, kind, scope='pending', site=''):
    """Start one explicit UI action; page reads never initiate remote requests."""
    if scope not in ('pending', 'all', 'site'):
        return {'success': False, 'message': '无效的执行范围'}
    if not host.get_state():
        return {'success': False, 'message': '请先启用插件'}
    from . import ui
    if kind != 'invitee':
        return {'success': False, 'message': '无效的任务类型'}
    options = {'category': 'problem' if scope == 'pending' else 'all'}
    first = ui.list_sites(host, page_size=100, **options)
    rows = list(first['items'])
    for page in range(2, first.get('pages', 1) + 1):
        rows.extend(ui.list_sites(host, page=page, page_size=100, **options)['items'])
    if scope == 'site':
        rows = [row for row in rows if str(row['id']) == str(site)]
    if not rows:
        return {'success': True, 'message': '没有需要处理的站点'}
    with _ACTION_LOCK:
        if getattr(host, '_ui_task', {}).get('running') or getattr(host, '_refreshing', False):
            return {'success': True, 'message': '已有任务进行中，请稍候'}
        host._ui_task = {'running': True, 'started_at': time.time(), 'scope': scope, 'error': ''}
    generation = getattr(host, '_generation', None)

    def work():
        try:
            if generation != getattr(host, '_generation', None) or not host.get_state():
                return
            host.refresh_all_sites([row['name'] for row in rows])
        except Exception:
            from app.log import logger
            logger.exception('PT 邀请管家手动任务失败')
            host._ui_task['error'] = '任务执行异常，请查看插件日志'
        finally:
            host._ui_task.update(running=False, finished_at=time.time())

    Thread(target=work, name=f'{kind}-ui-task', daemon=True).start()
    return {'success': True, 'message': '已开始重新获取，结果会自动更新。'}
