"""Read Browserless account units without creating a browser or changing billing.

Uses the same accountSummary query as Browserless's official MCP account tool.
The browser API token travels in the POST body, never a URL or error message.
"""
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
import math
from threading import Lock
import time
from urllib.parse import urlsplit

import requests

from .ptengine.core import DEFAULT_BROWSERLESS_URL

ACCOUNT_URL = 'https://api.browserless.io/graphql'
ACCOUNT_QUERY = '''query PluginQuota($apiToken: String!) {
  accountSummary(apiToken: $apiToken) {
    plan currentPeriodEnd
    cloudUnits { unitsLeft used available }
  }
}'''
CACHE_SECONDS = 60
_CACHE = OrderedDict()
_CACHE_LOCK = Lock()


def _error(message, status='error'):
    return {'status': status, 'message': message}


def _official_cloud(value):
    if value is not None and not isinstance(value, str):
        return False
    try:
        parsed = urlsplit((value or DEFAULT_BROWSERLESS_URL).strip())
        host = (parsed.hostname or '').lower().rstrip('.')
        return (parsed.scheme in ('https', 'wss') and parsed.port in (None, 443)
                and not parsed.username and not parsed.password
                and (host == 'browserless.io' or host.endswith('.browserless.io')))
    except (TypeError, ValueError):
        return False


def _number(value, *, negative=False):
    # Missing/invalid data must stay unknown, never turn into zero or unlimited.
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or abs(value) > 10 ** 15 or not math.isfinite(value)
            or (not negative and value < 0)):
        return None
    return value


def lookup_quota(payload):
    """Explicit, authenticated UI lookup; unsaved token edits are supported."""
    if not isinstance(payload, dict):
        return _error('查询参数无效')
    token = payload.get('token')
    if not isinstance(token, str) or not token.strip():
        return _error('请先填写 Token')
    token = token.strip()
    if len(token) > 4096 or any(char.isspace() for char in token):
        return _error('Token 格式无效，请检查后重试')
    if not _official_cloud(payload.get('browserless_url')):
        return _error('额度查询仅支持 Browserless 官方云服务；自建或第三方服务请查看其控制台。', 'unsupported')

    key = sha256(token.encode()).digest()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            _CACHE.move_to_end(key)
            return {**cached[1], 'cached': True}

    try:
        response = requests.post(ACCOUNT_URL,
                                 json={'query': ACCOUNT_QUERY, 'variables': {'apiToken': token}},
                                 timeout=(5, 20), allow_redirects=False)
        try:
            if response.status_code in (401, 403):
                return _error('Token 无效、已撤销或无账户查询权限，请检查后重试')
            if response.status_code == 429:
                return _error('Browserless 查询过于频繁，请稍后重试')
            if response.status_code != 200:
                return _error('Browserless 账户接口暂时不可用，请稍后重试')
            body = response.json()
        finally:
            response.close()
    except requests.Timeout:
        return _error('额度查询超时，请检查网络后重试')
    except (requests.RequestException, ValueError):
        return _error('额度查询失败，请检查网络或稍后重试')

    if not isinstance(body, dict):
        return _error('Browserless 返回的数据格式无效，请稍后重试')
    if body.get('errors'):
        # Do not echo remote GraphQL errors: they can contain credentials.
        return _error('Browserless 未能读取账户，请检查 Token 是否有效及是否有查询权限')
    data = body.get('data')
    summary = data.get('accountSummary') if isinstance(data, dict) else None
    if not isinstance(summary, dict) or not summary:
        return _error('未查询到该 Token 对应的账户')
    units = summary.get('cloudUnits')
    unit_based = isinstance(units, dict)
    units = units if unit_based else {}
    plan = summary.get('plan')
    period_end = _number(summary.get('currentPeriodEnd'))
    # Expose only the fields needed by the form, not the account or API-key list.
    result = {'status': 'ok', 'scope': 'account',
              'plan': plan.replace(token, '[已隐藏]')[:80] if isinstance(plan, str) else '',
              'remaining': _number(units.get('unitsLeft'), negative=True),
              'used': _number(units.get('used')),
              'limit': _number(units.get('available')),
              'unit_based': unit_based,
              'period_end': period_end if period_end and period_end < 253402300800 else None,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'cached': False}
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), dict(result))
        _CACHE.move_to_end(key)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)
    return result
