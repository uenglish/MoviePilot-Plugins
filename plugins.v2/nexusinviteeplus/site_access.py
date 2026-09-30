"""站点访问、Cookie/UA 规整与响应分类。

只识别 Cloudflare 挑战，不尝试绕过；同时把「换行 Cookie」「空 UA」这类
环境问题在发请求前规整掉，避免 requests 直接抛 InvalidHeader 导致站点静默失效。
"""
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")

CF_CHALLENGE_REASON = "检测到 Cloudflare 验证页，请在站点中正常完成验证后更新 Cookie"
COOKIE_EXPIRED_REASON = "Cookie 已失效，请重新登录站点更新 Cookie"
CF_MARKERS = ("just a moment", "checking your browser", "cf-browser-verification",
              "challenge-platform", "cf_chl_opt", "attention required! | cloudflare")
LOGIN_MARKERS = ("takelogin.php", "action=login", "name=\"password\"", "name='password'",
                 "name=password")
LOGGED_IN_MARKERS = ("logout.php", "退出", "登出", "usercp.php", "viewmessages.php",
                     "mybonus.php", "邀請", "邀请 [ 发送 ]")


def normalize_cookie(cookie: Optional[str]) -> str:
    """
    规整 Cookie 字符串。

    CookieCloud 同步过来的 Cookie 常带换行/制表符，直接写进请求头会被 requests
    判为非法（InvalidHeader），请求根本不会发出，站点表现为「静默失效」。
    """
    return re.sub(r"[\r\n\t]+", "", cookie or "")


def normalize_ua(ua: Optional[str]) -> str:
    """站点未配置 User-Agent 时使用默认 UA，避免请求被站点直接拒绝。"""
    return (ua or "").strip() or DEFAULT_UA


def has_user_id(html: str) -> bool:
    """页面是否包含带数字 id 的用户中心链接（判断登录态最可靠的信号）。"""
    return bool(re.search(r"userdetails\.php\?id=\d+", html or ""))


def logout_marked(html: str) -> bool:
    """页面是否存在退出/控制面板等已登录专属入口。"""
    body = (html or "")[:200000]
    return any(marker in body for marker in ("logout.php", "退出", "登出", "usercp.php"))


def swap_www(url: str) -> Optional[str]:
    """在带 www. 与不带 www. 的主机名之间切换，用于纠正站点规范化跳转。"""
    if not url:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return None
    new_host = host[4:] if host.lower().startswith("www.") else "www." + host
    netloc = new_host
    if parts.port:
        netloc = "%s:%s" % (new_host, parts.port)
    return urlunsplit((parts.scheme or "https", netloc, parts.path or "/", parts.query, parts.fragment))


def is_logged_in(html: str) -> bool:
    """判断页面是否是已登录状态（出现退出/控制面板等入口）。"""
    body = (html or "")[:200000].lower()
    return any(marker.lower() in body for marker in LOGGED_IN_MARKERS)


def get(session: requests.Session, base_url: str, path: str, **kwargs) -> requests.Response:
    """所有专用处理器统一使用的相对路径请求。"""
    response = session.get(urljoin(base_url, path.lstrip("/")), timeout=(10, 30), **kwargs)
    _fix_encoding(response)
    return response


def _fix_encoding(response: requests.Response) -> None:
    """修正未声明编码的旧 PT 页面，避免中文表头解析失败。"""
    if (response.encoding or "").lower() not in ("", "iso-8859-1", "ascii"):
        return
    match = re.search(rb'charset=["\']?\s*([\w-]+)', response.content[:4096], re.IGNORECASE)
    if match:
        try:
            response.encoding = match.group(1).decode("ascii")
            return
        except UnicodeDecodeError:
            pass
    response.encoding = response.apparent_encoding or "utf-8"


def classify(response: requests.Response) -> Optional[str]:
    """
    将防护页、Cookie 失效和 HTTP 错误转换为用户可处理的原因。

    只有「看起来像登录页且没有任何已登录线索」时才判定 Cookie 失效，
    避免部分站点（如 PT时间）跳转到登录页导致误判。
    """
    response_url = (response.url or "").lower()
    body = (response.text or "")[:200000]
    body_lower = body.lower()

    if response.status_code in (403, 429, 503) and any(mark in body_lower for mark in CF_MARKERS):
        return CF_CHALLENGE_REASON
    if response.status_code >= 500:
        return "站点服务异常（HTTP %s）" % response.status_code
    if response.status_code == 403:
        if any(mark in body_lower for mark in CF_MARKERS):
            return CF_CHALLENGE_REASON
        return "站点返回 403，可能是 Cookie 已失效或触发访问限制"
    if response.status_code >= 400:
        return "请求失败（HTTP %s）" % response.status_code

    looks_login = any(mark in body_lower for mark in LOGIN_MARKERS) or any(
        marker in response_url for marker in ("login.php", "takelogin.php"))
    if looks_login and not is_logged_in(body):
        return COOKIE_EXPIRED_REASON
    return None


def detect_schema(html: str, site_url: str) -> Optional[str]:
    """根据域名与页面特征识别非 NexusPHP 站点体系。"""
    host = (site_url or "").lower()
    body = (html or "")[:200000].lower()
    if "totheglory.im" in host:
        return "ttg"
    if "yemapt" in host:
        return "yema"
    if "rousi.pro" in host:
        return "rousipro"
    if "unit3d" in body or ("livewire" in body and "/users/" in body):
        return "unit3d"
    if "gazelle" in body or ("user.php?action=" in body and "torrents.php" in body):
        return "gazelle"
    return None
