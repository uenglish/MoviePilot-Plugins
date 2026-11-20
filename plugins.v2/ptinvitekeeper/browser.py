"""Cloudflare 挑战处理：用浏览器仿真过盾并把刷新后的 Cookie 交回请求流程。

站点可通过配置项加入「不使用浏览器仿真」名单（例如站点明确禁止仿真登录），
此时只提示原因，不做任何仿真动作。
"""
import time
from typing import Optional, Tuple

from app.core.config import settings
from app.log import logger

CHALLENGE_MARKERS = ("just a moment", "checking your browser", "cf-browser-verification",
                     "challenge-platform", "cf_chl_opt", "attention required! | cloudflare",
                     "雷池", "waf", "safeline")


def is_challenge(html: Optional[str]) -> bool:
    """页面是否是 Cloudflare/雷池类挑战页。"""
    body = (html or "")[:200000].lower()
    return any(marker in body for marker in CHALLENGE_MARKERS)


def _cookie_list(cookie: str, url: str) -> list:
    """把 Cookie 字符串转换为浏览器可用的 cookie 列表。"""
    items = []
    for pair in (cookie or "").split(";"):
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            items.append({"name": name, "value": value, "url": url})
    return items


def _cookie_string(cookies: list) -> str:
    """把浏览器 Cookie 列表转回请求可用的字符串。"""
    return "; ".join(f"{item.get('name')}={item.get('value')}"
                     for item in cookies or [] if item.get("name"))


def pass_challenge(site_info: dict, url: str, wait_seconds: int = 45) -> Tuple[str, str]:
    """
    打开站点页面等待挑战通过。

    :param site_info: 站点信息（cookie/ua/proxy/timeout）
    :param url: 需要访问的地址
    :return: (页面源码, 刷新后的 Cookie)；失败时 Cookie 为空字符串
    """
    site = site_info.get("name", "")
    proxies = settings.PROXY_SERVER if site_info.get("proxy") else None
    ua = site_info.get("ua")
    timeout = max(30, int(site_info.get("timeout") or 60))
    try:
        from cloakbrowser import launch_context
    except Exception as err:
        logger.warn(f"站点 {site} 未安装 CloakBrowser（{str(err)}），改用 PlaywrightHelper 兜底")
        try:
            from app.helper.browser import PlaywrightHelper
            html = PlaywrightHelper().get_page_source(url=url, cookies=site_info.get("cookie"),
                                                      ua=ua, proxies=proxies, headless=True,
                                                      timeout=timeout)
            return html or "", ""
        except Exception as playwright_err:
            logger.error(f"站点 {site} 浏览器仿真失败：{str(playwright_err)}")
            return "", ""

    context = None
    try:
        context = launch_context(headless=True, proxy=proxies, user_agent=ua)
        context.add_cookies(_cookie_list(site_info.get("cookie"), url))
        page = context.new_page()
        page.set_default_timeout(timeout * 1000)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        deadline = time.monotonic() + wait_seconds
        html = ""
        while time.monotonic() < deadline:
            time.sleep(2)
            html = page.content() or ""
            if not is_challenge(html):
                break
        cookie = _cookie_string(context.cookies())
        if is_challenge(html):
            logger.error(f"站点 {site} 浏览器仿真未通过挑战")
            return html, cookie
        logger.info(f"站点 {site} 浏览器仿真已通过挑战")
        return html, cookie
    except Exception as err:
        logger.error(f"站点 {site} 浏览器仿真异常：{str(err)}")
        return "", ""
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
