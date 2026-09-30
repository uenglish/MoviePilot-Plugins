from typing import Optional, Tuple

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosigninplus.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class MTorrent(_ISiteSigninHandler):
    """
    馒头（M-Team）签到

    馒头站点没有签到功能，插件只需确认账号可用并顺带刷新访问时间：
    - 账号校验：POST /api/member/profile（请求头 x-api-key 携带站点配置中的 API 密钥）；
    - 刷新访问：POST /api/member/updateLastBrowse（该接口不接受 API 密钥，仅作尽力尝试）。
    接口一律返回 HTTP 200，是否成功要看响应体中的 code，不能只看状态码。
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "m-team"

    # API 密钥请求头
    _api_key_header = "x-api-key"

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return True if cls.site_url in url.split(".") else False

    @staticmethod
    def _api_result(res) -> Tuple[Optional[dict], str]:
        """
        解析馒头接口响应：接口返回 HTTP 200，业务结果在响应体 code/message 中
        :param res: 响应对象
        :return: (响应JSON, 错误信息)
        """
        if res is None:
            return None, "无法打开网站"
        text = (res.text or "").strip()
        if not text:
            return None, f"接口无响应内容，状态码：{res.status_code}"
        try:
            return res.json(), ""
        except Exception:
            return None, f"接口返回内容无法解析，状态码：{res.status_code}"

    @classmethod
    def _post(cls, url: str, site_info: CommentedMap, api_key: str):
        """
        携带 API 密钥请求馒头接口
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            cls._api_key_header: api_key,
        }
        if site_info.get("ua"):
            headers["User-Agent"] = site_info.get("ua")
        return RequestUtils(headers=headers,
                            timeout=site_info.get("timeout"),
                            proxies=settings.PROXY if site_info.get("proxy") else None,
                            referer=f"{site_info.get('url')}index").post_res(url=url, json={})

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作（馒头无签到功能，改为校验账号并刷新访问时间）
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA、API密钥等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        url = site_info.get("url")
        domain = StringUtils.get_url_domain(url)
        # 站点 API 密钥：MoviePilot 站点配置中的“API密钥”，历史配置可能放在 token 中
        api_key = site_info.get("apikey") or site_info.get("token")
        if not api_key:
            logger.error(f"{site} 站点无签到功能，但未配置 API 密钥")
            return False, "站点无签到功能；未配置 API 密钥，请在站点配置中填写！"

        # 校验账号是否可用
        result, error = self._api_result(self._post(url=f"https://api.{domain}/api/member/profile",
                                                   site_info=site_info,
                                                   api_key=api_key))
        if not result:
            logger.error(f"{site} 模拟登录失败，{error}")
            return False, f"模拟登录失败，{error}！"
        if str(result.get("code")) != "0":
            message = result.get("message") or "未知错误"
            logger.error(f"{site} 模拟登录失败，{message}（请检查站点 API 密钥）")
            return False, f"模拟登录失败，{message}！"

        # 顺带刷新访问时间：该接口不接受 API 密钥，失败不影响账号校验结果
        browse, browse_error = self._api_result(
            self._post(url=f"https://api.{domain}/api/member/updateLastBrowse",
                       site_info=site_info,
                       api_key=api_key))
        refreshed = bool(browse) and str(browse.get("code")) == "0"
        if not refreshed:
            logger.debug(f"{site} 刷新访问时间未生效：{browse_error or (browse or {}).get('message')}")

        logger.info(f"{site} 站点无签到功能，账号校验通过")
        return True, "站点无签到功能，账号校验通过"

    def login(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行登录操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 登录结果信息
        """
        return self.signin(site_info)
