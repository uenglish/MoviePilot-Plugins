"""通用签到流程的签到结果判定回归。

修复点：通用流程（未适配站点）过去只要页面处于登录态就上报“签到成功”，
即使站点改为需要提交验证码的表单、或页面根本没有签到结果。现在要求页面出现
明确的签到特征才算成功，需要验证码的表单报失败，其余情况明确标注“未检测到签到结果”。
站点文案跟随用户语言，因此判定同时兼容简体、繁体与英文。
"""
from unittest.mock import patch

import pytest

from app.plugins.autosigninplus import AutoSignInPlus, _is_signed_page, _need_captcha_page

SITE_INFO = {
    "name": "测试站",
    "url": "https://example.com/",
    "cookie": "c_secure_pass=abc",
    "ua": "Mozilla/5.0",
    "proxy": 0,
    "timeout": 10,
}

SIGNED_HTML = {
    "简体": "<a href='logout.php'>退出</a><h2>签到成功</h2>"
            "<p>这是您的第 <b>34</b> 次签到，已连续签到 <b>34</b> 天，本次签到获得 <b>175</b> 个魔力值。</p>",
    "繁体": "<a href='logout.php'>退出</a><h2>簽到成功</h2>"
            "<p>這是您的第 <b>42</b> 次簽到，已連續簽到 <b>42</b> 天，本次簽到獲得 <b>215</b> 個魔力值。</p>",
    "英文": "<a href='logout.php'>Logout</a><p>Already attendance</p>",
    "重复签到": "<a href='logout.php'>退出</a><div>已经签到过了，请勿重复刷新</div>",
}

CAPTCHA_HTML = (
    "<a href='logout.php'>退出</a><form method='post' action='attendance.php'>"
    "<img src='image.php?action=regimage&imagehash=hashA' />"
    "<input type='text' name='imagestring' value='' />"
    "<input type='hidden' name='imagehash' value='hashA' />"
    "<input type='submit' value='签到' /></form>"
)

UNKNOWN_HTML = "<a href='logout.php'>退出</a><p>欢迎回来</p><div>今日签到入口</div>"

LOGIN_HTML = "<form action='takelogin.php' method='post'><input type='password' name='password' /></form><a href='login.php'>登录</a>"


class FakeResponse:
    """模拟 requests 响应对象。"""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class FakeRequestUtils:
    """模拟 RequestUtils：按给定页面返回响应，不发起真实请求。"""

    def __init__(self, text: str):
        self._text = text

    def get_res(self, *args, **kwargs):
        """返回页面内容。"""
        return FakeResponse(self._text)

    def post_res(self, *args, **kwargs):
        """返回页面内容。"""
        return FakeResponse(self._text)


def _run_generic_signin(html: str, logged_in: bool = True):
    """以给定页面和登录态执行通用签到流程。"""
    with patch("app.plugins.autosigninplus.RequestUtils", lambda *args, **kwargs: FakeRequestUtils(html)), \
            patch("app.plugins.autosigninplus.SiteUtils.is_logged_in", staticmethod(lambda text: logged_in)):
        return AutoSignInPlus._AutoSignInPlus__signin_base(dict(SITE_INFO))


@pytest.mark.parametrize("case", list(SIGNED_HTML))
def test_is_signed_page_matches_all_languages(case):
    """简体、繁体、英文的已签到页面均能识别。"""
    assert _is_signed_page(SIGNED_HTML[case]) is True


@pytest.mark.parametrize("html", [CAPTCHA_HTML, UNKNOWN_HTML, LOGIN_HTML, ""])
def test_is_signed_page_ignores_pages_without_result(html):
    """没有签到结果的页面不能判定为已签到。"""
    assert _is_signed_page(html) is False


def test_need_captcha_page_detects_captcha_form():
    """验证码表单页面能被识别。"""
    assert _need_captcha_page(CAPTCHA_HTML) is True
    assert _need_captcha_page(SIGNED_HTML["简体"]) is False
    assert _need_captcha_page(UNKNOWN_HTML) is False


@pytest.mark.parametrize("case", list(SIGNED_HTML))
def test_generic_signin_reports_success_with_marker(case):
    """出现明确签到特征时上报签到成功。"""
    state, message = _run_generic_signin(SIGNED_HTML[case])
    assert state is True
    assert message == "签到成功"


def test_generic_signin_fails_on_captcha_page():
    """需要验证码的页面不再误报成功，而是明确失败。"""
    state, message = _run_generic_signin(CAPTCHA_HTML)
    assert state is False
    assert "验证码" in message


def test_generic_signin_marks_unknown_result():
    """无法确认签到结果时明确标注，不再直接上报签到成功。"""
    state, message = _run_generic_signin(UNKNOWN_HTML)
    assert state is True
    assert message == "模拟登录成功（未检测到签到结果）"
    assert "签到成功" not in message


def test_generic_signin_reports_invalid_cookie():
    """登录态失效时仍然上报 Cookie 失效，触发插件自动登录。"""
    state, message = _run_generic_signin(LOGIN_HTML, logged_in=False)
    assert state is False
    assert message == "签到失败，Cookie已失效！"


def test_signed_message_parses_traditional_chinese():
    """繁体签到页也能解析出签到详情。"""
    from app.plugins.autosigninplus.sites.nexusphp_attendance import NexusPhpAttendance

    message = NexusPhpAttendance.signed_message(SIGNED_HTML["繁体"])

    assert message == "签到成功，第42次签到，已连续签到42天，获得215魔力值"


def test_signed_message_detects_repeat_visit():
    """重复访问签到页按已签到处理。"""
    from app.plugins.autosigninplus.sites.nexusphp_attendance import NexusPhpAttendance

    assert NexusPhpAttendance.signed_message(SIGNED_HTML["重复签到"]) == "今日已签到"
