"""海胆的签到入口是有副作用的 GET。只调用一次，再用同一站点会话读取首页按钮。"""
import re
from urllib.parse import urljoin, urlsplit

from requests import Session, RequestException

from .core import (DEFAULT_USER_AGENT, SignResult, auth_page_result, cloudflare_pending,
                   has_account_evidence, login_page, signin_rejection, visible_markup)
from .http import SiteSessionCookies


def sign_in(site, rule, timeout, proxies):
    base = site.get("url") or ""
    cookie = site.get("cookie") or ""
    origin = urlsplit(base)
    if not origin.hostname or not cookie:
        return SignResult("failed", "未配置站点地址或 Cookie")
    home = urljoin(base, rule["path"])
    url = urljoin(base, rule["signin_path"])
    # 宿主持久会话会在连接中断后自动重试 GET，签到入口必须由调用方控制次数。
    with Session() as session:
        session.headers.update({"User-Agent": site.get("ua") or DEFAULT_USER_AGENT, "Accept": "text/html,*/*"})
        # 将初始 Cookie 放进 CookieJar；后续请求使用站点通过 Set-Cookie 更新的值。
        SiteSessionCookies(session).seed(base, cookie)

        def read_home():
            response = session.get(home, timeout=timeout, proxies=proxies, allow_redirects=False)
            return page_result(response, rule, homepage=True)

        try:
            before = read_home()
        except RequestException as error:
            return SignResult("failed", f"无法读取海胆首页（{type(error).__name__}）")
        if before:
            return before
        try:
            response = session.get(url, headers={"Referer": home, **rule["signin_headers"]},
                                   timeout=timeout, proxies=proxies, allow_redirects=False)
            submitted = page_result(response, rule, homepage=False)
            if submitted and submitted.auth_required:
                return submitted
            detail = submitted.message if submitted else ""
        except RequestException as error:
            detail = f"签到入口连接失败（{type(error).__name__}）"
        try:
            confirmed = read_home()
        except RequestException as error:
            confirmed = SignResult("failed", f"首页回读失败（{type(error).__name__}）")
        if confirmed and confirmed.status == "already":
            return SignResult("success", "签到成功（已回读海胆首页打卡按钮）")
        reason = confirmed.message if confirmed else "首页未显示当日打卡回执"
        return SignResult("failed", "；".join(filter(None, (detail, reason))))


def page_result(response, rule, *, homepage):
    code = response.status_code
    where = "海胆首页" if homepage else "海胆签到入口"
    if response.is_redirect:
        target = urlsplit(urljoin(response.url, response.headers.get("Location", "")))
        if target.path.rsplit("/", 1)[-1] == "login.php":
            return SignResult("failed", f"{where}要求重新登录，请检查站点 Cookie 和登录会话", auth_required=True)
        if not homepage and target.netloc == urlsplit(response.url).netloc and target.path in {"/", rule["path"]}:
            return None
    if code != 200:
        return SignResult("failed", f"{where}返回 HTTP {code}")
    html = response.text
    auth = auth_page_result(html)
    if auth:
        return auth
    if login_page(html):
        return SignResult("failed", f"{where}要求重新登录，请检查站点 Cookie 和登录会话", auth_required=True)
    if cloudflare_pending(html):
        return SignResult("failed", f"{where}要求人工验证，请在站点内完成")
    if homepage:
        if not has_account_evidence(html):
            return SignResult("failed", "海胆首页未确认登录状态")
        if not signin_rejection(html) and re.search(rule["already_html"], visible_markup(html), re.I | re.S):
            return SignResult("already", "今日已签到（海胆首页显示已打卡）")
    # AJAX 可能返回空响应或普通文本，最终以同一会话的首页按钮为准。
    return None
