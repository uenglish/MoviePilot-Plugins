from typing import Tuple

from ruamel.yaml import CommentedMap

from app.plugins.autosigninplus.sites import _ISiteSigninHandler
from app.plugins.autosigninplus.sites.nexusphp_attendance import NexusPhpAttendance
from app.utils.string import StringUtils


class Audiences(_ISiteSigninHandler):
    """
    观众签到

    站点为 NexusPHP 架构并部署了 Cloudflare：普通请求依赖浏览器产生的 cf_clearance，
    该 Cookie 过期后站点会返回 Cloudflare 挑战页（Just a moment...），此时签到会被拦截。
    这里在命中挑战时回退到浏览器仿真通过挑战，并用浏览器中刷新后的 Cookie 继续签到。
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "audiences.me"

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        if StringUtils.url_equal(url, cls.site_url):
            return True
        return cls.site_url in str(url)

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        return NexusPhpAttendance.sign_in(site_info, allow_browser=True)

    def login(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行登录操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 登录结果信息
        """
        return NexusPhpAttendance.login(site_info, allow_browser=True)
