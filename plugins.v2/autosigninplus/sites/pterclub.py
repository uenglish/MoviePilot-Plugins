import json
from typing import Tuple

from ruamel.yaml import CommentedMap

from app.log import logger
from app.plugins.autosigninplus.sites import _ISiteSigninHandler
from app.utils.string import StringUtils


class PTerClub(_ISiteSigninHandler):
    """
    猫签到
    """
    # 匹配的站点Url，站点同时存在 .com 与 .net 域名
    site_url = "pterclub.com"
    _domains = ("pterclub.com", "pterclub.net")

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类

        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return any(StringUtils.url_equal(url, domain) for domain in cls._domains)

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxy = site_info.get("proxy")
        render = site_info.get("render")
        timeout = site_info.get("timeout")

        # 签到（按站点配置的域名请求，.com/.net 均可）
        base_url = str(site_info.get("url") or "").rstrip("/")
        if not base_url:
            logger.warn(f"未配置 {site} 的站点地址，无法签到")
            return False, ""
        html_text = self.get_page_source(url=f"{base_url}/attendance-ajax.php",
                                         cookie=site_cookie,
                                         ua=ua,
                                         proxy=proxy,
                                         render=render,
                                         timeout=timeout)
        if not html_text:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        if "login.php" in html_text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'
        try:
            sign_dict = json.loads(html_text)
        except Exception as e:
            logger.error(f"{site} 签到失败，签到接口返回数据异常，错误信息：{str(e)}")
            return False, '签到失败，签到接口返回数据异常'
        if sign_dict['status'] == '1':
            # {"status":"1","data":" (签到已成功300)","message":"<p>这是您的第<b>237</b>次签到，
            # 已连续签到<b>237</b>天。</p><p>本次签到获得<b>300</b>克猫粮。</p>"}
            logger.info(f"{site} 签到成功")
            return True, '签到成功'
        else:
            # {"status":"0","data":"抱歉","message":"您今天已经签到过了，请勿重复刷新。"}
            logger.info(f"{site} 今日已签到")
            return True, '今日已签到'
