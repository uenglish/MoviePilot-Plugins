from typing import Tuple
from urllib.parse import urljoin

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosigninplus.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils


class HDDolby(_ISiteSigninHandler):
    """
    高清杜比（HDDolby）

    站点只开放 API 访问：请求头 x-api-key 取值站点配置中的“API Key”，该值应为站点 RSS 里的
    passkey。站点明确拒绝浏览器访问（带浏览器 UA 时接口返回 Browser access is blocked!），
    因此这里全程不使用浏览器仿真，也不使用 Cookie。

    接口中没有签到相关字段，站点也未开放签到接口，因此签到以 API 校验账号代替。
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "hddolby.com"

    # 账号数据接口
    _user_api = "api/v1/user/data"

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类
        """
        return cls.site_url in str(url)

    @classmethod
    def _headers(cls, api_key: str) -> dict:
        """
        构造接口请求头

        注意：不能带浏览器 UA，站点会以“Browser access is blocked!”拒绝浏览器访问。
        """
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "x-api-key": api_key,
        }

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作（站点未开放签到接口，改为 API 校验账号）
        :param site_info: 站点信息，含有站点Url、API Key 等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_url = str(site_info.get("url") or "")
        # 站点 API Key，即 RSS Key（passkey）
        api_key = str(site_info.get("apikey") or site_info.get("token") or "").strip()
        if not site_url or not api_key:
            logger.error(f"{site} 未配置 API Key（RSS Key），无法校验账号")
            return False, "签到失败：请在站点配置的 API Key 中填写 RSS Key！"

        res = RequestUtils(headers=self._headers(api_key),
                           timeout=site_info.get("timeout"),
                           proxies=settings.PROXY if site_info.get("proxy") else None).get_res(
            url=urljoin(site_url.rstrip("/") + "/", self._user_api))
        if res is None:
            logger.error(f"{site} 模拟登录失败，无法打开网站")
            return False, "模拟登录失败，无法打开网站！"
        try:
            result = res.json()
        except Exception:
            logger.error(f"{site} 模拟登录失败，接口返回内容异常，状态码：{res.status_code}")
            return False, f"模拟登录失败，接口返回内容异常，状态码：{res.status_code}！"

        if str(result.get("status")) != "0":
            message = ((result.get("error") or {}).get("message")
                       if isinstance(result.get("error"), dict) else None) or "API Key 无效"
            logger.error(f"{site} 模拟登录失败，{message}（请检查站点 API Key 是否为 RSS Key）")
            return False, f"模拟登录失败，{message}！"

        user = result.get("data")
        if isinstance(user, list):
            user = user[0] if user else {}
        username = (user or {}).get("username") or ""
        logger.info(f"{site} 站点无签到 API，API Key 校验通过（{username}）")
        return True, "站点无签到 API，API Key 校验通过"

    def login(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行登录操作
        :param site_info: 站点信息，含有站点Url、API Key 等信息
        :return: 登录结果信息
        """
        state, message = self.signin(site_info)
        if state:
            return True, "模拟登录成功（API Key 有效）"
        return state, message
