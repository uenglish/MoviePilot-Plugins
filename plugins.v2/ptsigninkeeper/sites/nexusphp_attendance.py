# -*- coding: utf-8 -*-
"""
NexusPHP 站点签到页（attendance.php）通用助手。

NexusPHP 的签到实现有两种形态：
1. 未开启签到验证码：访问 attendance.php 即完成签到，页面直接返回签到结果；
2. 开启签到验证码（SECURITY.iv 与 captcha.attendance.enabled 同时生效）：attendance.php
   返回 POST 表单（imagehash + imagestring），需要提交验证码后才会签到。

部分站点（如麒麟）还部署了雷池（SafeLine）WAF，非浏览器请求会返回 HTTP 468 的 JS 挑战页。
此时使用浏览器仿真通过挑战，并把浏览器中刷新后的 Cookie 回填给后续请求。

注意：
- 站点会跟随用户语言显示文案，因此匹配同时兼容简体、繁体与英文；
- MoviePilot 的 PlaywrightHelper 通过请求头下发 Cookie，雷池挑战页跳转后会丢失登录态，
  因此这里直接使用 CloakBrowser（MoviePilot 依赖的浏览器实现）写入浏览器 Cookie 罐。
"""
import re
import threading
import time
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urljoin, urlparse

from lxml import etree

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.helper.ocr import OcrHelper
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from ..ptengine.core import (auth_page_result, classify, extract_bonus, has_account_evidence,
                             plain_text, signin_rejection, visible_markup)
from ..ptengine.http import new_http_session


class NexusPhpAttendance:
    """NexusPHP attendance.php 签到助手（不匹配站点，仅被各站点模块调用）。"""

    # 只认可见的当天回执；累计天数、隐藏脚本和表单说明不构成签到证据。
    _receipt_rule = {
        'strict_status_keywords': True,
        'success_keywords': ['签到成功', '簽到成功', '成功签到', 'Attendance success'],
        'success': r'(?:本次|此次)(?:签到|簽到)(?:您)?(?:获得|獲得)\s*[\d,]+',
        'already_keywords': ['已签到', '已簽到', '已经签到', '已經簽到',
                             'Already attendance', 'You have already attend'],
    }
    _personal_record = re.compile(
        r'[这這]是您的第\s*\d+\s*次[签簽]到[，,\s]+已[连連][续續][签簽]到\s*\d+\s*天'
        r'[，,\s]+本次[签簽]到[获獲]得\s*[\d,.]+\s*(?:[个個点點]\s*)?(?:憨豆|魔力值)|'
        r'(?P<already>(?:您|你)(?:今天|今日)(?:已[经經]?)?[签簽](?:到[过過]|[过過]到)了|'
        r'You have already attend, no refresh please\.)|'
        r'You have already attended \d+ days, Continuous \d+ days, this time you will get (?P<bonus>\d+) bonus\.',
        re.I)

    # Cookie 已失效
    _login_patterns = (
        r"login\.php",
        r"takelogin\.php",
        r"该页面必须在登录后才能访问",
        r"該頁面必須在登錄後才能訪問",
        r"must be logged in",
    )

    # 雷池（SafeLine）WAF 挑战页特征
    _safeline_patterns = (
        r"id=[\"']slg-title[\"']",
        r"SafeLineChallenge",
        r"/\.safeline/",
        r"id=[\"']slg-box[\"']",
        r"safeline",
    )

    # Cloudflare 挑战/拦截页特征
    _cloudflare_patterns = (
        r"Just a moment",
        r"cf-chl",
        r"challenge-platform",
        r"__cf_chl",
        r"cdn-cgi/challenge",
        r"cf-error-details",
        r"Attention Required",
        r"Verify you are human",
        r"正在进行安全验证",
    )

    # 站点页面特征（用于判断 WAF 挑战已通过、页面已正常渲染）
    _site_page_patterns = (
        r"Powered by NexusPHP",
        r"logout",
        r"mainmenu",
    )

    # 验证码相关提示（用于识别验证码错误）
    _captcha_error_patterns = (
        r"[验驗][证證][码碼]",
        r"[验驗][证證]图片",
        r"[验驗][证證]圖片",
        r"security code",
    )

    # 浏览器仿真全局互斥：Playwright 同步接口不是线程安全的，多站点并发过盾会互相阻塞
    _browser_lock = threading.Lock()

    # CloakBrowser 的指纹为 Windows，遇到 Cloudflare 挑战时用与指纹一致的 UA 再试一次
    _windows_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

    # 浏览器等待 WAF 挑战通过的最长时间（秒）
    _browser_challenge_timeout = 120
    # 浏览器仿真的最多尝试次数
    _browser_attempts = 3

    @classmethod
    def is_safeline_challenge(cls, status_code: Optional[int], html: str) -> bool:
        """
        判断响应是否为雷池 WAF 的 JS 挑战页
        """
        if status_code == 468:
            return True
        if not html:
            return False
        return any(re.search(pattern, html, re.IGNORECASE) for pattern in cls._safeline_patterns)

    @classmethod
    def is_cloudflare_challenge(cls, status_code: Optional[int], html: str) -> bool:
        """
        判断响应是否为 Cloudflare 挑战/拦截页
        """
        if status_code in (403, 429, 503) and html and "cloudflare" in html.lower():
            return True
        if not html:
            return False
        return any(re.search(pattern, html, re.IGNORECASE) for pattern in cls._cloudflare_patterns)

    @classmethod
    def is_challenge(cls, status_code: Optional[int], html: str) -> bool:
        """
        判断响应是否为 WAF 挑战页（雷池或 Cloudflare）
        """
        return (cls.is_safeline_challenge(status_code, html)
                or cls.is_cloudflare_challenge(status_code, html))

    @classmethod
    def is_turnstile(cls, html: str) -> bool:
        """
        判断页面是否包含 Cloudflare Turnstile 人机验证

        这类页面的签到表单只有隐藏字段（cf-token），由验证通过后的 JS 自动提交，
        普通请求无法完成，需要浏览器仿真。
        """
        if not html:
            return False
        return bool(re.search(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", html, re.IGNORECASE))

    @classmethod
    def confirm_signed(cls, site: str, checkin_url: str, site_info,
                       attempts: int = 3, delay: int = 4) -> str:
        """
        提交表单后回查签到页确认结果

        部分站点提交后会跳转或返回中间态页面，直接判定失败会误报；
        回查只要能确认签到状态就返回对应提示，否则返回空字符串。
        """
        for _ in range(1, max(1, attempts) + 1):
            time.sleep(delay)
            _, html = cls.request_html(url=checkin_url, site_info=site_info)
            if html and cls.is_signed(html):
                return cls.signed_message(html)
        return ""

    @classmethod
    def is_signed(cls, html: str) -> bool:
        """
        判断页面是否已是签到完成的状态
        """
        return cls._receipt(html).status in {'success', 'already'}

    @classmethod
    def _receipt(cls, html: str):
        """共用 PT 引擎的可见内容、否定提示及未提交按钮校验。"""
        content = visible_markup(html or '')
        # 部分说明文本没有表单按钮，同样不能把“签到成功即可获得”当作完成。
        if re.search(r'(?:签到|簽到)成功(?:后|後|即|才|可)|(?:如果|若)(?:签到|簽到)成功',
                     re.sub(r'\s+', '', plain_text(content))):
            from ..ptengine.core import SignResult
            return SignResult('failed', '页面仅显示签到说明，未确认本次签到成功')
        return classify(content, cls._receipt_rule)

    @classmethod
    def is_login_page(cls, html: str) -> bool:
        """
        判断页面是否明确是登录页（不使用“缺少登出链接”这类弱特征，避免页面中间态误判）
        """
        if not html:
            return False
        return any(re.search(pattern, html, re.IGNORECASE) for pattern in cls._login_patterns)

    @classmethod
    def is_not_login(cls, html: str) -> bool:
        """
        判断页面是否已经掉登录
        """
        if not html:
            return True
        if SiteUtils.is_logged_in(html):
            return False
        return cls.is_login_page(html)

    @classmethod
    def is_site_page(cls, html: str) -> bool:
        """
        判断页面是否已正常渲染为站点页面（而非 WAF 挑战的中间态）
        """
        if not html:
            return False
        return any(re.search(pattern, html, re.IGNORECASE) for pattern in cls._site_page_patterns)

    @staticmethod
    def attendance_url(site_info) -> str:
        """
        获取签到页地址
        """
        site_url = str(site_info.get("url") or "").strip()
        if "attendance.php" in site_url:
            return site_url
        return urljoin(site_url.rstrip("/") + "/", "attendance.php")

    @staticmethod
    def parse_form(html: str, base_url: str) -> Optional[Dict[str, str]]:
        """
        解析签到页中需要提交的表单（验证码表单或普通签到提交表单）

        :param html: 页面源码
        :param base_url: 站点地址，用于补齐表单 action
        :return: 表单提交参数字典（含 __action__），页面无需提交表单时返回 None
        """
        if not html or "attendance" not in html.lower():
            return None
        try:
            tree = etree.HTML(html)
            if tree is None:
                return None
            # 优先匹配带验证码的签到表单，其次匹配签到页上的普通提交表单
            forms = tree.xpath("//form[.//input[@name='imagehash']]")
            if not forms:
                forms = tree.xpath(
                    "//form[contains(@action, 'attendance')]"
                    "[.//input[@type='submit' or @type='button' or @type='image']]"
                )
            if not forms:
                return None
            form = forms[0]
            action = form.xpath("./@action")
            data = {}
            for input_node in form.xpath(".//input"):
                name = input_node.get("name")
                if not name:
                    continue
                input_type = (input_node.get("type") or "text").lower()
                if input_type in ("submit", "button", "image"):
                    # 普通签到表单可能需要提交按钮名，验证码表单则不需要
                    if input_type == "submit" and "imagehash" not in html:
                        data[name] = input_node.get("value") or ""
                    continue
                data[name] = input_node.get("value") or ""
            # 站点通常会把验证码默认填入 input，个别站点通过脚本赋值
            if not data.get("imagestring"):
                script_value = re.search(
                    r"imagestring[\"']?\s*\)?\s*\.\s*(?:val|value)\s*\(\s*[\"']([^\"']+)[\"']",
                    html
                ) or re.search(
                    r"name=[\"']imagestring[\"'][^>]*value=[\"']([^\"']+)[\"']",
                    html
                )
                if script_value:
                    data["imagestring"] = script_value.group(1)
            data["__action__"] = urljoin(base_url.rstrip("/") + "/",
                                         (action[0] if action else "attendance.php"))
            return data
        except Exception as err:
            logger.error(f"解析签到表单失败：{str(err)}")
            return None

    @staticmethod
    def solve_captcha(html: str, site_info) -> str:
        """
        识别验证码：页面中取验证码图片交给 OCR 服务识别

        :param html: 页面源码
        :param site_info: 站点信息
        :return: 验证码答案，无法识别时返回空字符串
        """
        if not html:
            return ""
        img_url = None
        try:
            tree = etree.HTML(html)
            if tree is not None:
                images = tree.xpath("//img[contains(@src, 'regimage') or contains(@src, 'image.php')]/@src")
                if images:
                    img_url = urljoin(str(site_info.get("url")), images[0])
        except Exception as err:
            logger.error(f"解析验证码图片失败：{str(err)}")
        if not img_url:
            return ""
        try:
            answer = OcrHelper().get_captcha_text(image_url=img_url,
                                                  cookie=site_info.get("cookie"),
                                                  ua=site_info.get("ua"))
            if answer:
                logger.info(f"{site_info.get('name')} 验证码识别结果：{answer}")
                return str(answer).strip()
            logger.warn(f"{site_info.get('name')} 验证码无法自动识别，需要手动签到")
        except Exception as err:
            logger.error(f"验证码识别失败：{str(err)}")
        return ""

    @staticmethod
    def request_html(url: str, site_info, method: str = "get", data: dict = None) -> Tuple[Optional[int], str]:
        """
        请求页面

        :return: (状态码, 页面源码)
        """
        cookie = re.sub(r"[\r\n\t]+", "", site_info.get("cookie") or "")
        request = RequestUtils(cookies=cookie,
                               ua=site_info.get("ua"),
                               proxies=settings.PROXY if site_info.get("proxy") else None,
                               timeout=site_info.get("timeout") or 20)
        res = request.post_res(url=url, data=data,
                               allow_redirects=True) if method == "post" else request.get_res(url=url)
        if res is None:
            return None, ""
        return res.status_code, res.text or ""

    @staticmethod
    def _parse_cookie(cookie: str) -> Iterable[Tuple[str, str]]:
        """
        解析 Cookie 字符串
        """
        for item in (cookie or "").split(";"):
            if "=" not in item:
                continue
            name, value = item.strip().split("=", 1)
            if name:
                yield name, value

    @classmethod
    def _cookie_list(cls, cookie: str, url: str) -> list:
        """
        将 Cookie 字符串转换为浏览器 Cookie 列表（主机域名 + 顶级域名）
        """
        host = urlparse(url).hostname or ""
        domains = {host}
        parts = host.split(".")
        if len(parts) > 2:
            domains.add("." + ".".join(parts[-2:]))
        else:
            domains.add("." + host)
        return [{"name": name, "value": value, "domain": domain, "path": "/"}
                for domain in sorted(domains)
                for name, value in cls._parse_cookie(cookie)]

    @staticmethod
    def _cookie_string(cookies: list) -> str:
        """
        将浏览器中的 Cookie 列表转换为 Cookie 字符串
        """
        return "; ".join(f"{cookie.get('name')}={cookie.get('value')}"
                         for cookie in cookies or [] if cookie.get("name"))

    @classmethod
    def browser_page_source(cls, url: str, site_info, attempts: int = None) -> Tuple[str, str]:
        """
        使用浏览器仿真打开页面，等待雷池等 WAF 的 JS 挑战通过

        CloakBrowser 偶发启动失败，这里失败自动重试，避免单次异常导致签到失败。

        :return: (页面源码, 浏览器中刷新后的Cookie字符串)
        """
        site = site_info.get("name")
        attempts = max(1, attempts or cls._browser_attempts)
        result = ("", "")
        # 同一时刻只允许一个站点使用浏览器仿真，避免 Playwright 同步接口被并发使用而卡死
        with cls._browser_lock:
            candidates = [site_info.get("ua"), cls._windows_ua]
            for index, browser_ua in enumerate(candidates):
                if not browser_ua or (index and browser_ua == candidates[0]):
                    continue
                # 换 UA 只作为 Cloudflare 指纹不一致时的兜底，试一次即可
                tries = attempts if index == 0 else 1
                for attempt in range(1, tries + 1):
                    html, cookie = cls._browser_once(url=url, site_info=site_info, ua=browser_ua)
                    result = (html, cookie)
                    if html and not cls.is_challenge(None, html):
                        return html, cookie
                    logger.warn(f"{site} 浏览器仿真第 {attempt} 次未通过 WAF 挑战")
                    if attempt < tries:
                        time.sleep(3)
                if index == 0:
                    logger.info(f"{site} 改用与浏览器指纹一致的 UA 重试")
        return result

    @classmethod
    def _browser_once(cls, url: str, site_info, ua: str = None) -> Tuple[str, str]:
        """
        单次浏览器仿真：启动浏览器、写入 Cookie、打开页面并等待挑战完成

        :param ua: 浏览器 UA，默认使用站点配置的 UA
        :return: (页面源码, 浏览器中刷新后的Cookie字符串)
        """
        proxies = settings.PROXY_SERVER if site_info.get("proxy") else None
        ua = ua or site_info.get("ua")
        timeout = max(30, int(site_info.get("timeout") or 60))
        try:
            from app.sdk.browser import launch_browser_context as launch_context
        except Exception as err:
            logger.warn(f"{site_info.get('name')} 未安装 CloakBrowser（{str(err)}），使用 PlaywrightHelper 兜底")
            html = PlaywrightHelper().get_page_source(url=url,
                                                      cookies=site_info.get("cookie"),
                                                      ua=ua,
                                                      proxies=proxies,
                                                      headless=True,
                                                      timeout=timeout)
            return html or "", ""

        context = None
        try:
            context = launch_context(headless=True, proxy=proxies, user_agent=ua)
            context.add_cookies(cls._cookie_list(site_info.get("cookie"), url))
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

            html = ""
            stable = ""
            deadline = time.monotonic() + cls._browser_challenge_timeout
            while time.monotonic() < deadline:
                html = page.content() or ""
                if cls.is_challenge(None, html):
                    stable = ""
                    time.sleep(2)
                    continue
                # 挑战通过后页面仍会继续渲染，需等页面稳定或已能识别出签到状态
                if cls.is_signed(html) or cls.is_login_page(html) or cls.parse_form(html, url):
                    break
                if cls.is_turnstile(html):
                    # Turnstile 通过后由 JS 自动提交签到表单，需继续等待签到结果
                    stable = html
                    time.sleep(2)
                    continue
                if html == stable and cls.is_site_page(html):
                    break
                stable = html
                time.sleep(2)
            return html, cls._cookie_string(context.cookies())
        except Exception as err:
            logger.error(f"{site_info.get('name')} 浏览器仿真失败：{str(err)}")
            return "", ""
        finally:
            try:
                if context:
                    context.close()
            except Exception as err:
                logger.debug(f"{site_info.get('name')} 关闭浏览器失败：{str(err)}")

    @staticmethod
    def _read_receipt_page(url, site_info):
        session = new_http_session()
        try:
            cookie = re.sub(r"[\r\n\t]+", "", site_info.get("cookie") or "")
            session.headers.update({'Cookie': cookie, 'Accept': 'text/html'})
            if site_info.get('ua'):
                session.headers['User-Agent'] = site_info['ua']
            response = session.get(url, timeout=site_info.get('timeout') or 20,
                                   proxies=settings.PROXY if site_info.get('proxy') else None)
            return (response.status_code, response.text or '') if response is not None else (None, '')
        finally:
            session.close()

    @classmethod
    def _personal_receipt(cls, html, url):
        """只从当前用户的回执段落取奖励，排除日历历史、说明文字和待提交表单。"""
        if not has_account_evidence(html):
            return False, '签到页未确认登录状态'
        if cls.parse_form(html, url):
            return False, '站点要求手动提交签到表单，请在站点内完成'
        reason = signin_rejection(html)
        if reason:
            return False, reason
        tree = etree.HTML(visible_markup(html))
        for node in tree.xpath('//*[self::p or (self::td or self::div) and not(*)][not(ancestor::blockquote)]'):
            text = ' '.join(' '.join(node.itertext()).split())
            match = cls._personal_record.match(text)
            if match:
                if match.group('bonus'):
                    return True, f"今日已签到，获得 {match.group('bonus')} bonus"
                if match.group('already'):
                    return True, '今日已签到'
                return True, cls.signed_message(match.group())
        return False, '站点未返回可确认的当日签到回执'

    @classmethod
    def ordinary_receipt(cls, site_info, action='签到', *, personal_receipt=False):
        """Use the site's normal GET with the configured session. Stop on human verification."""
        if not site_info.get('url') or not site_info.get('cookie'):
            return False, '未配置站点地址或 Cookie'
        url = cls.attendance_url(site_info) if action == '签到' else site_info['url']
        try:
            read = cls._read_receipt_page if personal_receipt else cls.request_html
            status, html = read(url, site_info)
        except Exception as error:
            return False, f'读取站点页面失败（{type(error).__name__}）'
        if cls.is_challenge(status, html) or cls.is_turnstile(html):
            return False, '站点需要人工验证，请在站点内完成'
        if status == 404 and action == '签到':
            return False, '签到入口暂不可用（HTTP 404），可能暂时关闭或地址变更，后续任务会重新检查'
        if status != 200 or not html:
            return False, f'站点未返回正常页面（HTTP {status or "无响应"}）'
        if action == '签到' and (auth := auth_page_result(html)):
            return False, auth.message
        if cls.is_login_page(html) and not SiteUtils.is_logged_in(html):
            return False, '请重新登录站点并更新 Cookie'
        if action == '登录':
            from ..result import has_login_evidence
            return (True, '登录成功（首页已验证）') if has_login_evidence(html) else (False, '未确认登录状态')
        if personal_receipt:
            return cls._personal_receipt(html, url)
        receipt = cls.signed_message(html)
        if receipt:
            return True, receipt
        if cls.parse_form(html, url):
            return False, '站点要求手动提交签到表单，请在站点内完成'
        return False, '站点未返回可确认的当日签到回执'

    @classmethod
    def sign_in(cls, site_info, allow_browser: bool = False) -> Tuple[bool, str]:
        """
        执行签到

        :param site_info: 站点信息
        :param allow_browser: 被 WAF 拦截时是否允许使用浏览器仿真
        """
        site = site_info.get("name")
        if not site_info.get("url") or not site_info.get("cookie"):
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return False, ""

        checkin_url = cls.attendance_url(site_info)
        logger.info(f"开始站点签到：{site}，地址：{checkin_url}...")

        status, html = cls.request_html(url=checkin_url, site_info=site_info)

        if cls.is_challenge(status, html):
            if not allow_browser:
                logger.error(f"{site} 签到失败，站点被 WAF 拦截")
                return False, "签到失败，站点被 WAF 拦截（Cloudflare/雷池）！"
            logger.info(f"{site} 命中 WAF 挑战，切换浏览器仿真")
            html, browser_cookie = cls.browser_page_source(url=checkin_url, site_info=site_info)
            if browser_cookie:
                # 浏览器已刷新 WAF Cookie，后续请求沿用
                site_info = dict(site_info)
                site_info["cookie"] = browser_cookie
            if not html or cls.is_challenge(None, html):
                logger.error(f"{site} 签到失败，WAF 挑战未通过")
                return False, "签到失败，WAF 挑战未通过！"

        if not html:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, "签到失败，请检查站点连通性"

        if cls.is_not_login(html):
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, "签到失败，Cookie已失效！"

        if cls.is_signed(html):
            logger.info(f"{site} 签到成功")
            return True, cls.signed_message(html)

        # Cloudflare Turnstile 人机验证：表单由 JS 自动提交，改用浏览器仿真
        if cls.is_turnstile(html):
            if not allow_browser:
                logger.error(f"{site} 签到失败，站点需要 Cloudflare Turnstile 人机验证")
                return False, "签到失败，站点需要 Cloudflare Turnstile 人机验证！"
            logger.info(f"{site} 命中 Cloudflare Turnstile 人机验证，切换浏览器仿真")
            html, browser_cookie = cls.browser_page_source(url=checkin_url, site_info=site_info)
            if browser_cookie:
                # 浏览器可能刷新了 Cookie，后续请求沿用
                site_info = dict(site_info)
                site_info["cookie"] = browser_cookie
            if cls.is_signed(html):
                logger.info(f"{site} 签到成功")
                return True, cls.signed_message(html)
            message = cls.confirm_signed(site=site, checkin_url=checkin_url, site_info=site_info)
            if message:
                logger.info(f"{site} 签到成功")
                return True, message
            logger.error(f"{site} 签到失败，Cloudflare Turnstile 人机验证未通过（需浏览器手动签到）")
            return False, "签到失败，需 Cloudflare Turnstile 人机验证，自动签到无法完成（建议手动签到）！"

        # 需要提交验证码表单
        form = cls.parse_form(html, str(site_info.get("url")))
        if not form:
            logger.error(f"{site} 签到失败，签到页面无法识别")
            return False, "签到失败，签到页面无法识别！"

        if "imagestring" in form and not form.get("imagestring"):
            answer = cls.solve_captcha(html, site_info)
            if not answer:
                return False, "签到失败，需要验证码，自动识别失败！"
            form["imagestring"] = answer

        action = form.pop("__action__")
        status, html = cls.request_html(url=action, site_info=site_info, method="post", data=form)
        if not html:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, "签到失败，请检查站点连通性"

        if cls.is_not_login(html):
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, "签到失败，Cookie已失效！"

        if cls.is_signed(html):
            logger.info(f"{site} 签到成功")
            return True, cls.signed_message(html)

        # 提交后可能是跳转或中间态页面，先回查签到页确认结果，避免误报失败
        message = cls.confirm_signed(site=site, checkin_url=checkin_url, site_info=site_info)
        if message:
            logger.info(f"{site} 签到成功")
            return True, message

        if any(re.search(pattern, html, re.IGNORECASE) for pattern in cls._captcha_error_patterns):
            logger.error(f"{site} 签到失败，验证码错误")
            return False, "签到失败，验证码错误！"

        logger.error(f"{site} 签到失败，签到结果无法识别")
        return False, "签到失败，签到结果无法识别！"

    @classmethod
    def signed_message(cls, html: str) -> str:
        """
        从签到结果页面提取信息，拼装友好提示（兼容简体、繁体）
        """
        result = cls._receipt(html)
        if result.status not in {'success', 'already'}:
            return ''
        text = plain_text(html)
        days = re.search(r"[这這]是您的第\s*(\d+)\s*次[签簽]到", text)
        keep_days = re.search(r"已[连連][续續][签簽]到\s*(\d+)\s*天", text)
        reward = extract_bonus(html, {})
        message = '今日已签到' if result.status == 'already' else '签到成功'
        if days:
            message += f"，第{days.group(1)}次签到"
        if keep_days:
            message += f"，已连续签到{keep_days.group(1)}天"
        if reward:
            message += '，' + (reward if re.match(r'获得|獲得|獲取', reward) else '获得' + reward)
        return message

    @classmethod
    def login(cls, site_info, allow_browser: bool = False) -> Tuple[bool, str]:
        """
        模拟登录（访问站点首页确认登录态）
        """
        site = site_info.get("name")
        site_url = str(site_info.get("url") or "").replace("attendance.php", "")
        if not site_url or not site_info.get("cookie"):
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法模拟登录")
            return False, ""

        status, html = cls.request_html(url=site_url, site_info=site_info)
        if cls.is_challenge(status, html):
            if not allow_browser:
                logger.error(f"{site} 模拟登录失败，站点被 WAF 拦截")
                return False, "模拟登录失败，站点被 WAF 拦截（Cloudflare/雷池）！"
            html, _ = cls.browser_page_source(url=site_url, site_info=site_info)
            if not html or cls.is_challenge(None, html):
                logger.error(f"{site} 模拟登录失败，WAF 挑战未通过")
                return False, "模拟登录失败，WAF 挑战未通过！"

        if not html:
            logger.error(f"{site} 模拟登录失败，无法打开网站")
            return False, "模拟登录失败，无法打开网站！"

        if cls.is_not_login(html):
            logger.error(f"{site} 模拟登录失败，Cookie已失效")
            return False, "模拟登录失败，Cookie已失效！"

        logger.info(f"{site} 模拟登录成功")
        return True, "模拟登录成功"
