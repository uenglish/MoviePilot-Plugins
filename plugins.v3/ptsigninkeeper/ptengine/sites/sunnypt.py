"""Sunny 新版 API 签到；参考 AutoSignIn 3.0.15，登录只验证会话。"""
from decimal import Decimal, InvalidOperation
from typing import Mapping
from urllib.parse import urlsplit


class SunnyPT:
    site_url = 'sunnypt.top'
    authoritative = True
    _session_url = 'https://sunnypt.top/api/auth/session'
    _api = 'https://api.sunnypt.top/api/v1/attendance'

    @classmethod
    def match(cls, url):
        return urlsplit(str(url or '')).hostname == cls.site_url

    @staticmethod
    def _request(ctx, url, token='', post=False):
        """限制到固定端点，不转发 Cookie 到 API，也不跟随认证重定向。"""
        session = None
        try:
            session = ctx._session()
            if token:
                session.headers.pop('Cookie', None)
                session.headers['Authorization'] = 'Bearer ' + token
            session.headers['Accept'] = 'application/json'
            sender = session.post if post else session.get
            response = sender(url, timeout=ctx.timeout, proxies=ctx.proxies,
                              allow_redirects=False, **({'json': {}} if post else {}))
            if response is None:
                return None, '接口未返回响应'
            status = response.status_code
            if status in (401, 403):
                return None, '会话已失效，请更新站点 Cookie'
            if status != 200:
                return None, f'接口请求异常（HTTP {status}）'
            try:
                payload = response.json()
            except (ValueError, TypeError):
                return None, '接口返回的内容无法解析'
            if not isinstance(payload, dict):
                return None, '接口返回的数据格式异常'
            return payload, ''
        except Exception:
            return None, '接口连接失败，请检查网络后重试'
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    @staticmethod
    def _code(payload):
        value = (payload or {}).get('code')
        if isinstance(value, bool):
            return None
        try:
            return int(str(value))
        except (ValueError, TypeError):
            return None

    @classmethod
    def _session(cls, site, ctx):
        if not str(site.get('cookie') or '').strip():
            return '', '未配置站点 Cookie'
        payload, error = cls._request(ctx, cls._session_url)
        data = (payload or {}).get('data')
        token = data.get('accessToken') if isinstance(data, Mapping) else None
        if not isinstance(token, str) or token.count('.') != 2 or any(c.isspace() for c in token):
            return '', error or '未取得有效会话令牌，请更新站点 Cookie'
        return token, ''

    @classmethod
    def _status(cls, ctx, token):
        payload, error = cls._request(ctx, cls._api + '/status', token)
        if cls._code(payload) == 401000:
            return None, '会话令牌已失效，请更新站点 Cookie'
        data = (payload or {}).get('data')
        if cls._code(payload) != 0 or not isinstance(data, Mapping):
            return None, error or '签到状态接口未返回有效结果'
        if type(data.get('checked_in')) is not bool:
            return None, '签到状态尚未明确，未提交'
        return data, ''

    @staticmethod
    def _reward(data):
        value = data.get('points') if isinstance(data, Mapping) else None
        if value is None or isinstance(value, bool):
            return ''
        try:
            amount = Decimal(str(value))
            if amount.is_finite() and amount >= 0:
                return f'（获得 {format(amount, "f")} 魔力值）'
        except (InvalidOperation, ValueError):
            pass
        return ''

    def login(self, site, ctx):
        token, error = self._session(site, ctx)
        if not token:
            return False, '登录失败：' + error
        data, error = self._status(ctx, token)
        return (True, '登录成功（API 会话已验证）') if data is not None else (False, '登录失败：' + error)

    def signin(self, site, ctx):
        token, error = self._session(site, ctx)
        if not token:
            return False, '签到失败：' + error
        before, error = self._status(ctx, token)
        if before is None:
            return False, '签到失败：' + error
        if before['checked_in']:
            return True, '今日已签到'
        payload, error = self._request(ctx, self._api + '/check-in', token, post=True)
        code = self._code(payload)
        if code == 0:
            return True, '签到成功' + self._reward(payload.get('data'))
        if code == 401000:
            return False, '签到失败：会话令牌已失效，请更新站点 Cookie'
        # 重复提交或网络结果未知时只回读，绝不再 POST 或切换浏览器提交。
        if code == 400001 or payload is None:
            after, _ = self._status(ctx, token)
            if after is not None and after['checked_in']:
                return True, '今日已签到（API 回读确认）'
        return False, '签到未确认：' + (error or '接口未返回可确认的签到结果')
