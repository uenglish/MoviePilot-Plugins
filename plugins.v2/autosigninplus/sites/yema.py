import base64
import hashlib
import json
import struct
import threading
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosigninplus.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class YemaPT(_ISiteSigninHandler):
    """
    YemaPT（野马）签到

    站点已从 NexusPHP 旧站（api/consumer/checkIn）迁移到新平台，接口变化为：
    - 站点前台：https://www.yemapt.org（旧域名 yemapt.org 会跳转到文档站）；
    - 签到状态：GET /api/consumer/fetchCheckInPageInfo；
    - 签到接口：POST /api/consumer/checkIn，需要携带 ALTCHA 人机验证（工作量证明）结果；
    - 人机验证：POST /api/captcha/generateAltchaChallenge {"feature": "checkIn"} 取题，
      本地求解（PBKDF2/SHA-256 逐次递增 counter，直到派生密钥以 keyPrefix 开头）后回传。
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "yemapt.org"

    # 新平台接口地址（站点配置里的地址可能仍是旧域名）
    _api_host = "https://www.yemapt.org"
    _checkin_info_api = "/api/consumer/fetchCheckInPageInfo"
    _checkin_api = "/api/consumer/checkIn"
    _challenge_api = "/api/captcha/generateAltchaChallenge"
    _profile_api = "/api/user/profile"
    # 人机验证求解并发数与最长耗时（秒）
    _solve_workers = 4
    _solve_timeout = 90

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

    @classmethod
    def _api_base(cls, site_info: CommentedMap) -> str:
        """
        获取接口地址前缀，兼容站点配置为旧域名或新域名
        """
        host = urlparse(str(site_info.get("url") or "")).hostname or ""
        if host.lower().startswith("www.") and cls.site_url in host:
            return f"https://{host}"
        return cls._api_host

    @classmethod
    def _auth_cookie(cls, site_info: CommentedMap) -> str:
        """
        站点鉴权 Cookie 值

        优先使用站点配置中的 API Auth Key（站点安全中心生成，有效期 180 天），
        没有配置时才退回浏览器 Cookie（会话有效期很短，容易过期导致签到失败）。
        """
        api_key = str(site_info.get("apikey") or "").strip()
        if api_key:
            return f"auth={api_key}"
        return str(site_info.get("cookie") or "")

    @classmethod
    def _request(cls, site_info: CommentedMap, api: str, method: str = "get", data: dict = None):
        """
        请求站点接口

        :return: (状态码, 响应对象)
        """
        base_url = cls._api_base(site_info)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": base_url,
        }
        request = RequestUtils(headers=headers,
                               ua=site_info.get("ua"),
                               cookies=cls._auth_cookie(site_info),
                               proxies=settings.PROXY if site_info.get("proxy") else None,
                               timeout=site_info.get("timeout"),
                               referer=base_url + "/")
        if method == "post":
            res = request.post_res(url=base_url + api, json=data or {})
        else:
            res = request.get_res(url=base_url + api)
        if res is None:
            return None, None
        return res.status_code, res

    @staticmethod
    def _json_result(res) -> Optional[dict]:
        """
        解析接口返回的JSON

        非JSON（如被 Cloudflare 拦截的页面）或非字典结果（如裸数字）一律返回 None，
        避免调用方 .get 时抛异常导致签到被判失败。
        """
        if res is None or not res.text:
            return None
        try:
            result = res.json()
        except Exception:
            logger.debug(f"YemaPT 接口返回非JSON内容：{res.text[:200]}")
            return None
        if not isinstance(result, dict):
            logger.debug(f"YemaPT 接口返回非预期结构：{str(result)[:200]}")
            return None
        return result

    @classmethod
    def _checkin_info(cls, site_info: CommentedMap) -> Optional[dict]:
        """
        查询今日签到状态
        """
        status, res = cls._request(site_info=site_info, api=cls._checkin_info_api)
        result = cls._json_result(res)
        if not result or not result.get("success"):
            return None
        return result.get("data") or {}

    @classmethod
    def _get_challenge(cls, site_info: CommentedMap) -> Optional[dict]:
        """
        获取 ALTCHA 人机验证题目
        """
        status, res = cls._request(site_info=site_info,
                                   api=cls._challenge_api,
                                   method="post",
                                   data={"feature": "checkIn"})
        result = cls._json_result(res)
        if not result or not result.get("success"):
            return None
        return result.get("data") or {}

    @staticmethod
    def _derive_key(parameters: dict, counter: int) -> bytes:
        """
        按 ALTCHA 规则计算指定 counter 的派生密钥
        """
        algorithm = str(parameters.get("algorithm") or "SHA-256").upper()
        key_length = int(parameters.get("keyLength") or 32)
        cost = max(1, int(parameters.get("cost") or 1))
        nonce = bytes.fromhex(str(parameters.get("nonce") or ""))
        salt = bytes.fromhex(str(parameters.get("salt") or ""))
        password = nonce + struct.pack(">I", counter)
        if algorithm.startswith("PBKDF2/"):
            hash_name = {
                "PBKDF2/SHA-512": "sha512",
                "PBKDF2/SHA-384": "sha384",
            }.get(algorithm, "sha256")
            return hashlib.pbkdf2_hmac(hash_name, password, salt, cost, dklen=key_length)
        hash_name = algorithm.replace("-", "").lower()
        digest = hashlib.new(hash_name, salt + password).digest()[:key_length]
        for _ in range(cost - 1):
            digest = hashlib.new(hash_name, digest).digest()[:key_length]
        return digest

    @classmethod
    def _solve_challenge(cls, parameters: dict) -> Optional[Tuple[int, str]]:
        """
        求解 ALTCHA 人机验证题目（多线程递增 counter）

        :return: (counter, derivedKey)，求解失败返回 None
        """
        key_prefix = str(parameters.get("keyPrefix") or "")
        if not key_prefix:
            return None
        workers = max(1, min(cls._solve_workers, int(parameters.get("cost") or 1) and cls._solve_workers))
        deadline = time.monotonic() + cls._solve_timeout
        found: dict = {}
        lock = threading.Event()
        start = time.monotonic()

        def worker(offset: int):
            counter = offset
            while not lock.is_set():
                if time.monotonic() > deadline:
                    return
                derived = cls._derive_key(parameters, counter)
                if derived.hex().startswith(key_prefix):
                    found["counter"] = counter
                    found["derivedKey"] = derived.hex()
                    lock.set()
                    return
                counter += workers

        threads = [threading.Thread(target=worker, args=(index,), daemon=True) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=max(1.0, deadline - time.monotonic()))

        if not found:
            return None
        logger.info(f"YemaPT 人机验证求解完成，counter={found['counter']}，"
                    f"耗时 {time.monotonic() - start:.1f} 秒")
        return found["counter"], found["derivedKey"]

    @classmethod
    def _altcha_payload(cls, challenge: dict) -> Optional[str]:
        """
        生成回传给站点的 ALTCHA 验证载荷（base64 后的 challenge + solution）
        """
        parameters = challenge.get("parameters") or {}
        solution = cls._solve_challenge(parameters)
        if not solution:
            return None
        payload = {
            "challenge": challenge,
            "solution": {
                "counter": solution[0],
                "derivedKey": solution[1],
                "time": 1000,
            },
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        if not self._auth_cookie(site_info):
            logger.warn(f"未配置 {site} 的 API Auth Key 或 Cookie，无法签到")
            return False, "签到失败：请在站点配置的 API Key 中填写 API Auth Key（站点安全中心生成，有效期 180 天）！"

        # 已签到则直接返回，避免重复签到
        info = self._checkin_info(site_info)
        if info is None:
            if str(site_info.get("apikey") or "").strip():
                logger.error(f"{site} 签到失败，无法获取签到信息，API Auth Key 可能无效或已过期")
                return False, "签到失败，API Auth Key 无效或已过期，请在站点安全中心重新生成！"
            logger.error(f"{site} 签到失败，无法获取签到信息，Cookie可能已失效")
            return False, "签到失败，Cookie已失效！"
        if info.get("checkedInToday"):
            days = info.get("continuousCheckInDays")
            logger.info(f"{site} 今日已签到")
            return True, f"今日已签到，已连续签到 {days} 天"

        # 取人机验证题目并求解
        challenge = self._get_challenge(site_info)
        if not challenge or not challenge.get("parameters"):
            logger.error(f"{site} 签到失败，获取人机验证题目失败")
            return False, "签到失败，获取人机验证失败！"
        payload = self._altcha_payload(challenge)
        if not payload:
            logger.error(f"{site} 签到失败，人机验证求解超时")
            return False, "签到失败，人机验证求解超时！"

        # 提交签到
        status, res = self._request(site_info=site_info,
                                    api=self._checkin_api,
                                    method="post",
                                    data={"altchaPayload": payload})
        result = self._json_result(res)
        if result is None:
            logger.error(f"{site} 签到失败，签到接口返回异常，状态码：{status}")
            return False, f"签到失败，签到接口返回异常，状态码：{status}！"

        if result.get("success"):
            data = result.get("data") or {}
            point = data.get("point") or data.get("checkInPoint") or data.get("bonus")
            message = f"签到成功，获得 {point} 积分" if point else "签到成功"
            logger.info(f"{site} {message}")
            return True, message

        error_message = str(result.get("errorMessage") or "签到失败")
        if "已签" in error_message:
            logger.info(f"{site} 今日已签到")
            return True, "今日已签到"
        logger.error(f"{site} 签到失败，{error_message}")
        return False, f"签到失败，{error_message}！"

    def login(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行登录操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 登录结果信息
        """
        site = site_info.get("name")
        status, res = self._request(site_info=site_info, api=self._profile_api)
        result = self._json_result(res)
        if result and result.get("success"):
            logger.info(f"{site} 模拟登录成功")
            return True, "模拟登录成功"
        if result is None:
            logger.error(f"{site} 模拟登录失败，站点返回异常，状态码：{status}")
            return False, f"模拟登录失败，站点返回异常，状态码：{status}！"
        if str(site_info.get("apikey") or "").strip():
            logger.error(f"{site} 模拟登录失败，API Auth Key 无效或已过期")
            return False, "模拟登录失败，API Auth Key 无效或已过期，请在站点安全中心重新生成！"
        logger.error(f"{site} 模拟登录失败，Cookie已失效")
        return False, "模拟登录失败，Cookie已失效！"
