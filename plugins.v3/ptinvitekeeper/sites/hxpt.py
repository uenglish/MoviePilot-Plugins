"""好学账号在邀请管理页使用的配额和下级列表。"""
from urllib.parse import urljoin, urlsplit

import requests

from . import _ISiteHandler


class HxptHandler(_ISiteHandler):
    site_schema = 'hxpt'

    @classmethod
    def match(cls, url):
        host = (urlsplit(str(url)).hostname or '').lower().rstrip('.')
        return host in {'hxpt.org', 'www.hxpt.org'}

    @staticmethod
    def _read(session, base, path, **params):
        response = session.get(urljoin(base, path), params=params, headers={'Accept': 'application/json'},
                               timeout=(6, 20), allow_redirects=False)
        if response.status_code in (401, 403):
            raise ValueError('邀请页面需要重新登录或授权')
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get('code') != 0 or not isinstance(result.get('data'), dict):
            raise ValueError('站点未返回账号邀请数据')
        return result['data']

    def parse_invite_page(self, site_info, session):
        base = site_info['url']
        try:
            status = self._read(session, base, '/api/v1/invites/status')
            required = ('class_id', 'quota_limit', 'quota_used', 'quota_extra')
            if not all(type(status.get(k)) is int and status[k] >= 0 for k in required):
                raise ValueError('站点未返回完整配额')
            members = []
            total = None
            for page in range(1, 101):
                batch = self._read(session, base, '/api/v1/invites/invitees', page=page, per=50)
                items = batch.get('items')
                if (not isinstance(items, list) or type(batch.get('total')) is not int
                        or batch['total'] < 0 or batch.get('page') != page):
                    raise ValueError('邀请成员页数据不完整')
                total = batch['total']
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get('username'), str):
                        raise ValueError('邀请成员资料格式有变，请重新核对')
                    members.append({'username': item['username']})
                if len(members) >= total or not items:
                    break
            if len(members) != total:
                raise ValueError('邀请成员列表未完整读取')
            available = max(0, status['quota_limit'] + status['quota_extra'] - status['quota_used'])
            can_invite = status['class_id'] >= 3 and available > 0
            reason = (f"当前账号等级 LV{status['class_id']}，LV3 及以上可邀请" if status['class_id'] < 3 else
                      (f'本周还可发出 {available} 枚邀请' if available else '本周邀请名额不足'))
            return {'invite_status': {'can_invite': can_invite, 'reason': reason,
                                      'temporary_count': available, 'permanent_count': 0}, 'invitees': members}
        except (requests.RequestException, ValueError) as exc:
            reason = '邀请页面读取失败' if isinstance(exc, requests.RequestException) else str(exc)
            return {'error': reason, 'invite_status': {'can_invite': False, 'reason': reason}, 'invitees': []}
