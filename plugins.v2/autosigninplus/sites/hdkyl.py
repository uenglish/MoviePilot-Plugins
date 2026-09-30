from typing import Tuple

from ruamel.yaml import CommentedMap

from app.plugins.autosigninplus.sites import _ISiteSigninHandler
from app.plugins.autosigninplus.sites.nexusphp_attendance import NexusPhpAttendance
from app.utils.string import StringUtils


class HDKylin(_ISiteSigninHandler):
    """
    麒麟签到

    站点为 NexusPHP 架构，并部署了雷池（SafeLine）WAF：非浏览器请求会被要求执行 JS 挑战
    （返回 HTTP 468 的挑战页），因此普通请求被拦截时回退到浏览器仿真，由浏览器通过挑战后
    再按 NexusPHP 的签到流程处理。
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "hdkyl.in"

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
