"""躺平 NexusPHP 签到：读取已登录账号的正常回执。"""
from urllib.parse import urlparse

from . import _ISiteSigninHandler
from .nexusphp_attendance import NexusPhpAttendance


class TangPT(_ISiteSigninHandler):
    site_url = 'tangpt.top'
    ordinary_receipt_fallback = True

    @classmethod
    def match(cls, url: str) -> bool:
        host = (urlparse(str(url)).hostname or '').lower().rstrip('.')
        return host == cls.site_url or host.endswith('.' + cls.site_url)

    def signin(self, site_info):
        return NexusPhpAttendance.ordinary_receipt(site_info)

    def login(self, site_info):
        return NexusPhpAttendance.ordinary_receipt(site_info, '登录')
