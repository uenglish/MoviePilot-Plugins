"""思齐、Depth Studio、麒麟、YemaPT 签到适配回归。

麒麟使用雷池 WAF，需要浏览器仿真通过挑战并沿用刷新后的 Cookie；YemaPT 迁移到新平台后
签到需要 ALTCHA 人机验证（工作量证明）。这里用桩数据覆盖各分支，不向真实站点发请求。
"""
import base64
import hashlib
import json
import struct

import pytest

from app.plugins.autosigninplus.sites.audiences import Audiences
from app.plugins.autosigninplus.sites.dstudio import DStudio
from app.plugins.autosigninplus.sites.hdkyl import HDKylin
from app.plugins.autosigninplus.sites.nexusphp_attendance import NexusPhpAttendance
from app.plugins.autosigninplus.sites.siqi import SiQi
from app.plugins.autosigninplus.sites.yema import YemaPT

SITE_URL = "https://si-qi.xyz/"

SIGNED_HTML = (
    '<table><tr><td><h2>签到成功</h2></td></tr>'
    "<tr><td>这是您的第 <b>12</b> 次签到，已连续签到 <b>12</b> 天，"
    "本次签到获得 <b>50</b> 个魔力值。</td></tr></table>"
    '<a href="logout.php">退出</a>'
)

REPEAT_HTML = '<a href="logout.php">退出</a><div>已经签到过了，请勿重复刷新</div>'

FORM_HTML = (
    '<a href="logout.php">退出</a><table><form method="post" action="attendance.php">'
    '<img src="image.php?action=regimage&imagehash=hashA" />'
    '<input type="text" name="imagestring" value="9527" />'
    '<input type="hidden" name="imagehash" value="hashA" />'
    '<input type="submit" value="签到" /></form></table>'
    '<input type="submit" value="签到" /></form></table>'
)

EMPTY_FORM_HTML = FORM_HTML.replace('value="9527"', 'value=""')

LOGIN_HTML = (
    '<form action="takelogin.php" method="post"><input type="password" name="password" /></form>'
    '<a href="login.php">登录</a>'
)

CHALLENGE_HTML = (
    '<html><head><title id="slg-title"></title></head><body><div id="slg-box"></div>'
    '<script src="/.safeline/challenge/v2/challenge.js"></script></body></html>'
)


class FakeResponse:
    """模拟 requests 响应对象，仅提供站点模块用到的属性。"""

    def __init__(self, payload: dict):
        self.text = json.dumps(payload)
        self.status_code = 200
        self.payload = payload

    def json(self):
        """返回接口JSON。"""
        return self.payload

@pytest.fixture
def site_info():
    """站点信息桩，字段与插件运行时传入的一致。"""
    return {
        "id": 21,
        "name": "思齐",
        "url": SITE_URL,
        "cookie": "c_secure_pass=abc",
        "ua": "Mozilla/5.0",
        "proxy": 0,
        "render": 0,
        "timeout": 15,
        "public": 0,
    }


def _stub_requests(monkeypatch, responses, calls=None):
    """按顺序返回 (状态码, 页面源码)，并记录请求参数。"""

    def fake_request_html(url, site_info, method="get", data=None):
        if calls is not None:
            calls.append({"url": url, "method": method, "data": dict(data or {}), "cookie": site_info.get("cookie")})
        return responses[min(len(calls or []) - 1, len(responses) - 1)] if calls is not None else responses.pop(0)

    monkeypatch.setattr(NexusPhpAttendance, "request_html", staticmethod(fake_request_html))


@pytest.mark.parametrize("handler, url", [
    (SiQi, SITE_URL),
    (DStudio, "https://dstudio.me/"),
    (HDKylin, "https://www.hdkyl.in/"),
    (YemaPT, "https://yemapt.org/"),
])
def test_site_modules_match(handler, url):
    """四个站点的签到模块均能匹配各自的站点地址。"""
    assert handler.match(url) is True


def test_nexusphp_signed_page_reports_detail(monkeypatch, site_info):
    """未开启验证码的站点，访问签到页即完成签到，并回传签到详情。"""
    calls = []
    _stub_requests(monkeypatch, [(200, SIGNED_HTML)], calls)

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is True
    assert message == "签到成功，第12次签到，已连续签到12天，获得50魔力值"
    assert calls[0]["url"] == SITE_URL + "attendance.php"
    assert calls[0]["method"] == "get"


def test_nexusphp_repeat_page_reports_already_signed(monkeypatch, site_info):
    """重复访问签到页时按已签到处理。"""
    _stub_requests(monkeypatch, [(200, REPEAT_HTML)], [])

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is True
    assert message == "今日已签到"


def test_nexusphp_submits_prefilled_captcha(monkeypatch, site_info):
    """开启验证码的站点提交页面中默认填入的验证码。"""
    calls = []
    _stub_requests(monkeypatch, [(200, FORM_HTML), (200, SIGNED_HTML)], calls)

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is True
    assert message.startswith("签到成功")
    assert calls[1]["method"] == "post"
    assert calls[1]["url"] == SITE_URL + "attendance.php"
    assert calls[1]["data"] == {"imagestring": "9527", "imagehash": "hashA"}


def test_nexusphp_uses_ocr_when_captcha_empty(monkeypatch, site_info):
    """验证码为空时调用 OCR 识别后再提交。"""
    calls = []
    _stub_requests(monkeypatch, [(200, EMPTY_FORM_HTML), (200, SIGNED_HTML)], calls)
    monkeypatch.setattr(NexusPhpAttendance, "solve_captcha", staticmethod(lambda html, info: "8888"))

    state, _ = NexusPhpAttendance.sign_in(site_info)

    assert state is True
    assert calls[1]["data"]["imagestring"] == "8888"


def test_nexusphp_fails_without_captcha_answer(monkeypatch, site_info):
    """验证码无法识别时明确失败，不误报成功。"""
    _stub_requests(monkeypatch, [(200, EMPTY_FORM_HTML), (200, SIGNED_HTML)], [])
    monkeypatch.setattr(NexusPhpAttendance, "solve_captcha", staticmethod(lambda html, info: ""))

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is False
    assert "验证码" in message


def test_nexusphp_reports_invalid_cookie(monkeypatch, site_info):
    """登录态失效时提示 Cookie 失效，触发插件自动登录。"""
    _stub_requests(monkeypatch, [(200, LOGIN_HTML)], [])

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is False
    assert message == "签到失败，Cookie已失效！"


def test_waf_challenge_requires_browser_when_disabled(monkeypatch, site_info):
    """未允许浏览器仿真时，WAF 拦截直接失败，不误报成功。"""
    _stub_requests(monkeypatch, [(468, CHALLENGE_HTML)], [])

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is False
    assert message == "签到失败，站点被 WAF 拦截（Cloudflare/雷池）！"


def test_safeline_challenge_passes_with_browser(monkeypatch, site_info):
    """允许浏览器仿真时通过挑战并沿用刷新后的 Cookie。"""
    calls = []
    _stub_requests(monkeypatch, [(468, CHALLENGE_HTML), (200, REPEAT_HTML)], calls)
    monkeypatch.setattr(NexusPhpAttendance, "browser_page_source",
                        classmethod(lambda cls, url, site_info: (SIGNED_HTML, "c_secure_pass=refresh")))

    state, message = NexusPhpAttendance.sign_in(dict(site_info), allow_browser=True)

    assert state is True
    assert message.startswith("签到成功")
    # 页面已在浏览器中完成签到，无需再发普通请求
    assert len(calls) == 1
    assert calls[0]["url"] == SITE_URL + "attendance.php"


def test_safeline_challenge_keeps_refreshed_cookie_for_form_post(monkeypatch, site_info):
    """浏览器只用于过盾时，后续表单提交沿用刷新后的 Cookie。"""
    calls = []
    _stub_requests(monkeypatch, [(468, CHALLENGE_HTML), (200, SIGNED_HTML)], calls)
    monkeypatch.setattr(NexusPhpAttendance, "browser_page_source",
                        classmethod(lambda cls, url, site_info: (FORM_HTML, "c_secure_pass=refresh")))

    state, _ = NexusPhpAttendance.sign_in(dict(site_info), allow_browser=True)

    assert state is True
    assert calls[1]["method"] == "post"
    assert calls[1]["cookie"] == "c_secure_pass=refresh"


def test_safeline_challenge_not_passed(monkeypatch, site_info):
    """挑战未通过时给出明确失败提示。"""
    _stub_requests(monkeypatch, [(468, CHALLENGE_HTML)], [])
    monkeypatch.setattr(NexusPhpAttendance, "browser_page_source",
                        classmethod(lambda cls, url, site_info: (CHALLENGE_HTML, "")))

    state, message = NexusPhpAttendance.sign_in(dict(site_info), allow_browser=True)

    assert state is False
    assert message == "签到失败，WAF 挑战未通过！"


def test_nexusphp_parse_form_reads_script_filled_captcha():
    """站点通过脚本填入验证码时从脚本中取值。"""
    html = ('<form action="attendance.php"><input type="hidden" name="imagehash" value="hashB" />'
            '<input type="text" name="imagestring" value="" /></form>'
            '<script>$("#imagestring").val("2468");</script>')

    form = NexusPhpAttendance.parse_form(html, SITE_URL)

    assert form["imagestring"] == "2468"
    assert form["imagehash"] == "hashB"
    assert form["__action__"] == SITE_URL + "attendance.php"


def _altcha_parameters(counter: int, cost: int = 1, key_length: int = 32) -> dict:
    """按 ALTCHA 规则构造一道可求解的题（keyPrefix 对应指定 counter）。"""
    nonce = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    salt = "102030405060708090a0b0c0d0e0f001"
    derived = hashlib.pbkdf2_hmac("sha256",
                                  bytes.fromhex(nonce) + struct.pack(">I", counter),
                                  bytes.fromhex(salt), cost, dklen=key_length)
    return {
        "algorithm": "PBKDF2/SHA-256",
        "nonce": nonce,
        "salt": salt,
        "cost": cost,
        "keyLength": key_length,
        "keyPrefix": derived.hex(),
        "expiresAt": 4102444800,
        "data": {"feature": "checkIn"},
    }


def test_yema_solves_altcha_pow(monkeypatch):
    """ALTCHA 工作量证明求解命中服务端指定的 counter。"""
    monkeypatch.setattr(YemaPT, "_solve_workers", 1)

    counter, derived_key = YemaPT._solve_challenge(_altcha_parameters(counter=7))

    assert counter == 7
    assert derived_key == _altcha_parameters(counter=7)["keyPrefix"]


def test_yema_altcha_payload_shape():
    """回传的载荷为 challenge + solution 的 base64 编码。"""
    parameters = _altcha_parameters(counter=2)
    challenge = {"parameters": parameters, "signature": "sign"}

    payload = YemaPT._altcha_payload(challenge)

    decoded = json.loads(base64.b64decode(payload))
    assert decoded["challenge"] == challenge
    assert decoded["solution"]["counter"] == 2
    assert decoded["solution"]["derivedKey"] == parameters["keyPrefix"]


def test_yema_skips_when_already_checked_in(monkeypatch, site_info):
    """今日已签到时不重复签到。"""
    monkeypatch.setattr(YemaPT, "_checkin_info",
                        classmethod(lambda cls, info: {"checkedInToday": True, "continuousCheckInDays": 6}))
    monkeypatch.setattr(YemaPT, "_request",
                        classmethod(lambda cls, **kwargs: pytest.fail("已签到不应再请求接口")))

    state, message = YemaPT().signin(dict(site_info, url="https://yemapt.org/"))

    assert state is True
    assert message == "今日已签到，已连续签到 6 天"


def test_yema_posts_checkin_with_altcha(monkeypatch, site_info):
    """未签到站点取题求解后携带 altchaPayload 提交签到。"""
    parameters = _altcha_parameters(counter=1)
    challenge = {"parameters": parameters, "signature": "sign"}
    responses = []

    monkeypatch.setattr(YemaPT, "_checkin_info", classmethod(lambda cls, info: {"checkedInToday": False}))
    monkeypatch.setattr(YemaPT, "_get_challenge", classmethod(lambda cls, info: challenge))

    def fake_request(cls, site_info, api, method="get", data=None):
        responses.append({"api": api, "method": method, "data": data})
        return 200, FakeResponse({"success": True, "data": {"point": 88}})
    monkeypatch.setattr(YemaPT, "_request", classmethod(fake_request))

    state, message = YemaPT().signin(dict(site_info, url="https://yemapt.org/"))

    assert state is True
    assert message == "签到成功，获得 88 积分"
    assert responses[0]["api"] == "/api/consumer/checkIn"
    payload = json.loads(base64.b64decode(responses[0]["data"]["altchaPayload"]))
    assert payload["solution"]["counter"] == 1


def test_yema_reports_checkin_error(monkeypatch, site_info):
    """签到接口返回业务错误时回传错误信息。"""
    challenge = {"parameters": _altcha_parameters(counter=0), "signature": "sign"}
    monkeypatch.setattr(YemaPT, "_checkin_info", classmethod(lambda cls, info: {"checkedInToday": False}))
    monkeypatch.setattr(YemaPT, "_get_challenge", classmethod(lambda cls, info: challenge))
    monkeypatch.setattr(YemaPT, "_request", classmethod(lambda cls, **kwargs: (
        200, FakeResponse({"success": False, "errorMessage": "验证失败, 请刷新页面重试"}))))

    state, message = YemaPT().signin(dict(site_info, url="https://yemapt.org/"))

    assert state is False
    assert message == "签到失败，验证失败, 请刷新页面重试！"


def test_yema_treats_already_signed_error_as_success(monkeypatch, site_info):
    """接口提示已签到时按成功处理。"""
    challenge = {"parameters": _altcha_parameters(counter=0), "signature": "sign"}
    monkeypatch.setattr(YemaPT, "_checkin_info", classmethod(lambda cls, info: {"checkedInToday": False}))
    monkeypatch.setattr(YemaPT, "_get_challenge", classmethod(lambda cls, info: challenge))
    monkeypatch.setattr(YemaPT, "_request", classmethod(lambda cls, **kwargs: (
        200, FakeResponse({"success": False, "errorMessage": "你已签过到, 请勿重新尝试"}))))

    state, message = YemaPT().signin(dict(site_info, url="https://yemapt.org/"))

    assert state is True
    assert message == "今日已签到"


def test_yema_login_uses_profile_api(monkeypatch, site_info):
    """模拟登录使用个人资料接口校验登录态。"""
    called = {}

    def fake_request(cls, site_info, api, method="get", data=None):
        called["api"] = api
        return 200, FakeResponse({"success": True})

    monkeypatch.setattr(YemaPT, "_request", classmethod(fake_request))

    state, message = YemaPT().login(dict(site_info, url="https://yemapt.org/"))

    assert state is True
    assert message == "模拟登录成功"
    assert called["api"] == "/api/user/profile"

AUDIENCES_SIGNED_HTML = (
    '<a href="logout.php">退出</a>'
    '<div class="attendance-hero__text"><h1 class="attendance-hero__title">每日签到</h1>'
    '<p class="attendance-hero__sub">今日已签到，明天再来吧</p></div>'
    '<div>您今天已经签到过了，请勿重复刷新。</div>'
)

AUDIENCES_FORM_HTML = (
    '<a href="logout.php">退出</a><h1>每日签到</h1>'
    '<form method="post" action="attendance.php"><input type="hidden" name="action" value="signin" />'
    '<input type="submit" name="submit" value="签到" /></form>'
)

CLOUDFLARE_CHALLENGE_HTML = (
    '<html><head><title>Just a moment...</title></head><body>'
    '<div id="cf-challenge-running"></div>'
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    '<noscript>Please enable JavaScript</noscript></body></html>'
)


@pytest.mark.parametrize("html, expected", [
    (CLOUDFLARE_CHALLENGE_HTML, True),
    (CHALLENGE_HTML, True),
    (SIGNED_HTML, False),
    ("", False),
])
def test_is_challenge_detects_waf_pages(html, expected):
    """雷池与 Cloudflare 的挑战页都能识别，正常页面不误判。"""
    assert NexusPhpAttendance.is_challenge(None, html) is expected


def test_cloudflare_status_hint():
    """Cloudflare 的 403/503 拦截页可识别。"""
    assert NexusPhpAttendance.is_cloudflare_challenge(503, "<html>cloudflare</html>") is True
    assert NexusPhpAttendance.is_cloudflare_challenge(200, "<html>cloudflare</html>") is False


def test_audiences_signed_page_detected():
    """观众签到页（现代版式）的已签到文案能被识别。"""
    assert NexusPhpAttendance.is_signed(AUDIENCES_SIGNED_HTML) is True
    assert NexusPhpAttendance.signed_message(AUDIENCES_SIGNED_HTML) == "今日已签到"


def test_audiences_module_uses_browser(monkeypatch):
    """观众模块在命中 WAF 时允许浏览器仿真。"""
    called = {}

    def fake_sign_in(site_info, allow_browser=False):
        called["allow_browser"] = allow_browser
        return True, "签到成功"

    monkeypatch.setattr(NexusPhpAttendance, "sign_in", staticmethod(fake_sign_in))
    assert Audiences.match("https://audiences.me/") is True
    state, _ = Audiences().signin({"name": "观众", "url": "https://audiences.me/", "cookie": "a=b"})
    assert state is True
    assert called["allow_browser"] is True


def test_nexusphp_submits_generic_attendance_form(monkeypatch, site_info):
    """没有验证码的普通签到表单也能提交。"""
    calls = []
    _stub_requests(monkeypatch, [(200, AUDIENCES_FORM_HTML), (200, AUDIENCES_SIGNED_HTML)], calls)

    state, message = NexusPhpAttendance.sign_in(site_info)

    assert state is True
    assert message == "今日已签到"
    assert calls[1]["method"] == "post"
    assert calls[1]["data"] == {"action": "signin", "submit": "签到"}
