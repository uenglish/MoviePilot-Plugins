from typing import Tuple

from ruamel.yaml import CommentedMap

from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.plugins.autosignin.sites.nexusphp_attendance import NexusPhpAttendance
from app.utils.string import StringUtils


class DStudio(_ISiteSigninHandler):
    """
    Depth Studio签到

    站点为 NexusPHP 架构，签到页 attendance.php：
    - 未开启签到验证码时访问即完成签到；
    - 开启签到验证码时页面返回带验证码的表单，需要提交表单完成签到。
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "dstudio.me"

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
        return NexusPhpAttendance.sign_in(site_info)

    def login(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行登录操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 登录结果信息
        """
        return NexusPhpAttendance.login(site_info)
