"""读取 YemaPT 邀请管理页面的本人名额与直属成员。"""
from urllib.parse import urlsplit

import requests

from . import _ISiteHandler


class YemaHandler(_ISiteHandler):
    site_schema = 'yema'
    base = 'https://www.yemapt.org'

    @classmethod
    def match(cls, url):
        return (urlsplit(str(url)).hostname or '').lower().rstrip('.') in {'yemapt.org', 'www.yemapt.org', 'web.yemapt.org'}

    @staticmethod
    def _data(response):
        if response.status_code in (401, 403):
            raise ValueError('邀请页面需要有效的网页登录会话，请更新 Cookie')
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise ValueError('站点未确认邀请页面请求')
        return payload.get('data')

    def parse_invite_page(self, site_info, session):
        headers = {'Accept': 'application/json'}
        if site_info.get('apikey'):
            headers['api-auth-key'] = site_info['apikey']
        options = {'headers': headers, 'timeout': (6, 20), 'allow_redirects': False}
        try:
            overview = self._data(session.get(self.base + '/api/invite/fetchInvitePageInfo', **options))
            if not isinstance(overview, dict):
                raise ValueError('站点未返回邀请概况')
            claim = overview.get('inviteClaimInfo') or {}
            permanent = claim.get('permanentInviteNum')
            temporary = claim.get('tempInviteNum')
            total = (overview.get('invitePromotionStats') or {}).get('totalInviteUserNum')
            if not all(type(n) is int and n >= 0 for n in (permanent, temporary, total)):
                raise ValueError('邀请配额或成员人数不完整')
            members = []
            for page in range(1, 101):
                batch = self._data(session.post(self.base + '/api/invite/fetchInviteUserList',
                                    json={'pageParam': {'current': page, 'pageSize': 50}}, **options))
                if not isinstance(batch, list):
                    raise ValueError('邀请成员列表格式有变')
                for member in batch:
                    name = (member.get('name') or member.get('userName')) if isinstance(member, dict) else None
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError('邀请成员资料不完整')
                    members.append({'username': name.strip()})
                if len(members) >= total or not batch:
                    break
            if len(members) != total or len({m['username'] for m in members}) != total:
                raise ValueError('邀请成员列表未完整读取')
            slots = permanent + temporary
            return {'invite_status': {'can_invite': False, 'permanent_count': permanent, 'temporary_count': temporary,
                                      'reason': '邀请名额不足' if slots == 0 else f'已读取 {slots} 枚邀请，发送资格待确认'},
                    'invitees': members}
        except (AttributeError, ValueError, requests.RequestException) as exc:
            reason = '邀请页面读取失败' if isinstance(exc, (AttributeError, requests.RequestException)) else str(exc)
            return {'error': reason, 'invite_status': {'can_invite': False, 'reason': reason}, 'invitees': []}
