"""Rousi Pro 当前账号的邀请资格和直属成员。"""
from urllib.parse import urlsplit

import requests

from . import _ISiteHandler


class RousiProHandler(_ISiteHandler):
    site_schema = 'rousipro'

    @classmethod
    def match(cls, url):
        return (urlsplit(str(url)).hostname or '').lower().rstrip('.') in ('rousi.pro', 'www.rousi.pro')

    def parse_invite_page(self, site_info, session):
        headers = {'Accept': 'application/json'}
        if site_info.get('apikey'):
            headers['api-token'] = site_info['apikey']
        try:
            response = session.get('https://rousi.pro/api/v1/me/invitations', params={'limit': 20, 'offset': 0},
                                   headers=headers, timeout=(6, 20), allow_redirects=False)
            if response.status_code in (401, 403):
                raise ValueError('邀请页面需要有效的网页登录会话，请更新 Cookie')
            response.raise_for_status()
            payload = response.json()
            eligibility = payload.get('eligibility') if isinstance(payload, dict) else None
            network = payload.get('network') if isinstance(payload, dict) else None
            if not isinstance(eligibility, dict) or not isinstance(network, dict):
                raise ValueError('未识别账号邀请信息')
            slots = eligibility.get('remaining_invites')
            members = network.get('direct_members')
            if (type(slots) is not int or slots < 0 or not isinstance(members, list)
                    or len(members) != network.get('direct_count')
                    or not all(isinstance(m, dict) and m.get('username') for m in members)):
                raise ValueError('邀请配额或成员数据不完整')
            # The quota is meaningful even when further account eligibility is unknown.
            permitted = eligibility.get('can_invite')
            reason = ('名额不足' if slots == 0 else ('账号可发送邀请' if permitted is True else
                      f'已读取 {slots} 枚邀请，发送资格待确认'))
            return {'invite_status': {'can_invite': bool(slots and permitted is True),
                                      'reason': reason, 'remaining_count': slots},
                    'invitees': [{'username': m['username']} for m in members]}
        except (requests.RequestException, ValueError) as exc:
            reason = '邀请页面读取失败' if isinstance(exc, requests.RequestException) else str(exc)
            return {'error': reason, 'invite_status': {'can_invite': False, 'reason': reason}, 'invitees': []}
