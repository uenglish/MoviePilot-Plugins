"""思齐、Depth Studio、麒麟、YemaPT 签到适配回归。

麒麟使用雷池 WAF，需要浏览器仿真通过挑战并沿用刷新后的 Cookie；YemaPT 迁移到新平台后
签到需要 ALTCHA 人机验证（工作量证明）。这里用桩数据覆盖各分支，不向真实站点发请求。
"""
import base64
import hashlib
import json
import re
import struct

import pytest

from app.plugins.autosigninplus import AutoSignInPlus
from app.plugins.autosigninplus.sites import _ISiteSigninHandler
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

NEWLINE_COOKIE = "c_secure_pass=abc;\nc_secure_uid=MTIz;\r\nc_secure_ssl=bm9wZQ=="


def test_sites_helper_cleans_cookie_for_header():
    """CookieCloud 同步的换行 Cookie 需要规整后再放进请求头。"""
    assert _ISiteSigninHandler.clean_cookie(NEWLINE_COOKIE) == "c_secure_pass=abc;c_secure_uid=MTIz;c_secure_ssl=bm9wZQ=="
    assert _ISiteSigninHandler.clean_cookie("a=b; c=d") == "a=b; c=d"
    assert _ISiteSigninHandler.clean_cookie("") == ""
    assert _ISiteSigninHandler.clean_cookie(None) == ""


def test_get_page_source_uses_clean_cookie(monkeypatch):
    """get_page_source 请求头中的 Cookie 不含换行，UA 为空时不下发。"""
    captured = {}

    class FakeRequestUtils:
        def __init__(self, headers=None, **kwargs):
            captured["headers"] = headers or {}

        def get_res(self, url=None, **kwargs):
            return None

    monkeypatch.setattr("app.plugins.autosigninplus.sites.RequestUtils", FakeRequestUtils)
    _ISiteSigninHandler.get_page_source(url="https://example.com/", cookie=NEWLINE_COOKIE,
                                        ua="", proxy=0, render=0)
    headers = captured["headers"]
    assert "\n" not in headers["Cookie"] and "\r" not in headers["Cookie"]
    assert "User-Agent" not in headers


def test_plugin_normalizes_site_cookie():
    """插件入口会规整站点 Cookie，模块与通用流程都拿到干净 Cookie。"""
    normalized = AutoSignInPlus._normalize_site_info({"name": "站点", "cookie": NEWLINE_COOKIE})
    assert "\n" not in normalized["cookie"] and "\r" not in normalized["cookie"]
    clean = {"name": "站点", "cookie": "a=b; c=d"}
    assert AutoSignInPlus._normalize_site_info(clean) is clean

def test_pttime_treats_already_signed_as_success(monkeypatch):
    """PT时间 已签到时返回“拒绝访问：已签到，无需再签”，应视为成功而不是失败。"""
    from app.plugins.autosigninplus.sites.pttime import PTTime

    monkeypatch.setattr(PTTime, "get_page_source",
                        staticmethod(lambda **kwargs: "拒绝访问：已签到，无需再签"))
    state, message = PTTime().signin({"name": "PT时间", "url": "https://pttime.org/", "cookie": "a=b"})
    assert state is True
    assert message == "今日已签到"


def test_pttime_reports_real_failure(monkeypatch):
    """PT时间 返回无法识别的页面时仍上报失败。"""
    from app.plugins.autosigninplus.sites.pttime import PTTime

    monkeypatch.setattr(PTTime, "get_page_source", staticmethod(lambda **kwargs: "<html>异常页面</html>"))
    state, message = PTTime().signin({"name": "PT时间", "url": "https://pttime.org/", "cookie": "a=b"})
    assert state is False
    assert message == "签到失败"

def _mteam_site():
    """馒头站点信息桩（只配置 API 密钥，站点没有 Cookie）。"""
    return {"name": "馒头", "url": "https://kp.m-team.cc/", "ua": "Mozilla/5.0",
            "apikey": "APIKEY", "cookie": "", "proxy": 0, "timeout": 15}


def test_mteam_verifies_account_with_api_key(monkeypatch):
    """馒头没有签到功能：用 API 密钥校验账号，成功即视为完成。"""
    from app.plugins.autosigninplus.sites.mteam import MTorrent

    calls = []

    class FakeRes:
        def __init__(self, payload):
            self.text = json.dumps(payload)
            self.status_code = 200

        def json(self):
            return json.loads(self.text)

    class FakeRequestUtils:
        def __init__(self, headers=None, **kwargs):
            self.headers = headers or {}

        def post_res(self, url=None, json=None, **kwargs):
            calls.append({"url": url, "headers": self.headers, "json": json})
            if url.endswith("/api/member/profile"):
                return FakeRes({"code": "0", "message": "SUCCESS", "data": {"id": "1"}})
            return FakeRes({"code": 401, "message": "Full authentication is required"})

    monkeypatch.setattr("app.plugins.autosigninplus.sites.mteam.RequestUtils", FakeRequestUtils)

    state, message = MTorrent().signin(_mteam_site())

    assert state is True
    assert message == "站点无签到功能，账号校验通过"
    assert calls[0]["url"].endswith("/api/member/profile")
    assert calls[0]["headers"]["x-api-key"] == "APIKEY"
    assert "Authorization" not in calls[0]["headers"]


def test_mteam_reports_invalid_api_key(monkeypatch):
    """API 密钥无效时不再误报成功。"""
    from app.plugins.autosigninplus.sites.mteam import MTorrent

    class FakeRes:
        text = json.dumps({"code": 1, "message": "key無效"})
        status_code = 200

        def json(self):
            return json.loads(self.text)

    class FakeRequestUtils:
        def __init__(self, **kwargs):
            pass

        def post_res(self, **kwargs):
            return FakeRes()

    monkeypatch.setattr("app.plugins.autosigninplus.sites.mteam.RequestUtils", FakeRequestUtils)

    state, message = MTorrent().signin(_mteam_site())

    assert state is False
    assert "key無效" in message


def test_mteam_requires_api_key():
    """未配置 API 密钥时给出明确提示。"""
    from app.plugins.autosigninplus.sites.mteam import MTorrent

    site = _mteam_site()
    site.pop("apikey")
    state, message = MTorrent().signin(site)
    assert state is False
    assert "API 密钥" in message

@pytest.mark.parametrize("status, level, icon", [
    ("签到成功", "success", "mdi-check-circle"),
    ("今日已签到", "success", "mdi-check-circle"),
    ("模拟登录成功（未检测到签到结果）", "warning", "mdi-help-circle-outline"),
    ("站点无签到功能，账号校验通过", "none", "mdi-information-outline"),
    ("签到失败，请检查站点连通性", "error", "mdi-alert-circle"),
    ("签到失败，Cookie已失效！", "error", "mdi-cookie-off"),
])
def test_status_meta_levels(status, level, icon):
    """详情页状态配色：新结果类型（未确认/无签到功能）不再混进成功或失败。"""
    meta = AutoSignInPlus._status_meta(status)
    assert meta["level"] == level
    assert meta["icon"] == icon

def test_legacy_history_migration():
    """插件改名后能从旧插件继承历史记录（无 key 查询返回的是数据对象列表）。"""
    class FakeRecord:
        def __init__(self, key, value):
            self.key = key
            self.value = value

    class FakePlugin(AutoSignInPlus):
        def __init__(self):
            self.saved = {}

        def get_data(self, key=None, plugin_id=None):
            if plugin_id == self._legacy_plugin_id:
                return [FakeRecord("9月30日", [{"site": "测试站", "status": "签到成功"}]),
                        FakeRecord("签到-2026-09-30", {"do": [1], "retry": []})]
            return []

        def save_data(self, key, value, plugin_id=None):
            self.saved[key] = value

    plugin = FakePlugin()
    plugin._AutoSignInPlus__migrate_legacy_data()

    assert set(plugin.saved) == {"9月30日", "签到-2026-09-30"}
    assert plugin.saved["签到-2026-09-30"] == {"do": [1], "retry": []}


def test_legacy_migration_skips_when_data_exists():
    """当前插件已有历史时不再重复迁移。"""
    class FakePlugin(AutoSignInPlus):
        def __init__(self):
            self.saved = {}

        def get_data(self, key=None, plugin_id=None):
            if plugin_id:
                raise AssertionError("已有数据时不应读取旧插件")
            return [object()]

        def save_data(self, key, value, plugin_id=None):
            self.saved[key] = value

    plugin = FakePlugin()
    plugin._AutoSignInPlus__migrate_legacy_data()
    assert plugin.saved == {}

def test_run_sites_times_out_without_blocking_others():
    """单站点卡住时按失败记录并跳过，其余站点照常汇总（不再拖死整轮、不再影响落库）。"""
    import time as _time

    plugin = AutoSignInPlus.__new__(AutoSignInPlus)
    plugin._queue_cnt = 3
    plugin._site_timeout = 1

    def fake_signin(site):
        if site["name"] == "卡住":
            _time.sleep(30)
        return site["name"], "签到成功"

    sites = [{"name": "卡住"}, {"name": "正常1"}, {"name": "正常2"}]
    status = plugin._AutoSignInPlus__run_sites(func=fake_signin, sites=sites, type_str="签到")

    assert ("正常1", "签到成功") in status
    assert ("正常2", "签到成功") in status
    hung = [item for item in status if item[0] == "卡住"][0]
    # 超时结果里带“失败”，命中默认重试关键词，下次执行会重试
    assert "失败" in hung[1]
    assert re.search("错误|失败", hung[1])


def test_browser_simulation_is_serialized(monkeypatch):
    """浏览器仿真串行执行：并发站点不会同时进入 Playwright（同步接口非线程安全）。"""
    import threading
    import time as _time

    from app.plugins.autosigninplus.sites.nexusphp_attendance import NexusPhpAttendance

    active = []
    peak = []

    def fake_browser_once(cls, url=None, site_info=None):
        active.append(1)
        peak.append(len(active))
        _time.sleep(0.15)
        active.pop()
        return "<html>签到成功</html>", "cookie=1"

    monkeypatch.setattr(NexusPhpAttendance, "_browser_once", classmethod(fake_browser_once))
    monkeypatch.setattr(NexusPhpAttendance, "is_challenge",
                        classmethod(lambda cls, res, html: False))

    results = []

    def worker():
        results.append(NexusPhpAttendance.browser_page_source(
            url="https://example.com/", site_info={"name": "测试"}))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 3
    assert max(peak) == 1, "浏览器仿真必须串行执行"

def test_yema_prefers_api_auth_key():
    """YemaPT 优先用 API Auth Key（有效期 180 天）作为鉴权，其次才是浏览器 Cookie。"""
    from app.plugins.autosigninplus.sites.yema import YemaPT

    assert YemaPT._auth_cookie({"apikey": "KEY123", "cookie": "auth=session"}) == "auth=KEY123"
    assert YemaPT._auth_cookie({"cookie": "auth=session"}) == "auth=session"
    assert YemaPT._auth_cookie({"apikey": "  ", "cookie": "auth=session"}) == "auth=session"


def test_yema_uses_api_key_in_requests(monkeypatch):
    """YemaPT 请求会带上 API Auth Key，且未配置时不走 Cookie 失效分支。"""
    from app.plugins.autosigninplus.sites import yema as yema_module
    from app.plugins.autosigninplus.sites.yema import YemaPT

    captured = {}

    class FakeRequestUtils:
        def __init__(self, headers=None, cookies=None, **kwargs):
            captured["cookies"] = cookies

        def get_res(self, url=None, **kwargs):
            return None

        def post_res(self, url=None, **kwargs):
            return None

    monkeypatch.setattr(yema_module, "RequestUtils", FakeRequestUtils)
    YemaPT._request(site_info={"url": "https://www.yemapt.org/", "apikey": "KEY123", "cookie": "auth=old"}, api="/x")
    assert captured["cookies"] == "auth=KEY123"

    state, message = YemaPT().signin({"name": "YemaPT", "url": "https://www.yemapt.org/", "apikey": "", "cookie": ""})
    assert state is False
    assert "API Auth Key" in message


def test_hddolby_uses_api_key_without_browser(monkeypatch):
    """高清杜比：只用 API Key 调用接口（不带浏览器 UA），绝不使用浏览器仿真。"""
    from app.plugins.autosigninplus.sites import hddolby as hddolby_module
    from app.plugins.autosigninplus.sites.hddolby import HDDolby

    captured = {}

    class FakeRes:
        status_code = 200
        text = json.dumps({"status": 0, "data": [{"id": "1", "username": "serendipity"}]}, ensure_ascii=False)

        def json(self):
            return json.loads(self.text)

    class FakeRequestUtils:
        def __init__(self, headers=None, **kwargs):
            captured["headers"] = headers or {}
            captured["cookies"] = kwargs.get("cookies")

        def get_res(self, url=None, **kwargs):
            captured["url"] = url
            return FakeRes()

    monkeypatch.setattr(hddolby_module, "RequestUtils", FakeRequestUtils)

    site = {"name": "高清杜比", "url": "https://www.hddolby.com/", "apikey": "RSSKEY", "cookie": ""}
    state, message = HDDolby().signin(site)

    assert state is True
    assert message == "站点无签到 API，API Key 校验通过"
    assert captured["headers"]["x-api-key"] == "RSSKEY"
    assert "User-Agent" not in captured["headers"], "站点拒绝浏览器访问，不能带浏览器 UA"
    assert captured["url"] == "https://www.hddolby.com/api/v1/user/data"


def test_hddolby_reports_invalid_key(monkeypatch):
    """高清杜比 API Key 无效时如实报错。"""
    from app.plugins.autosigninplus.sites import hddolby as hddolby_module
    from app.plugins.autosigninplus.sites.hddolby import HDDolby

    class FakeRes:
        status_code = 200
        text = json.dumps({"status": 10401, "error": {"message": "Invalid API Key"}})

        def json(self):
            return json.loads(self.text)

    class FakeRequestUtils:
        def __init__(self, **kwargs):
            pass

        def get_res(self, **kwargs):
            return FakeRes()

    monkeypatch.setattr(hddolby_module, "RequestUtils", FakeRequestUtils)

    state, message = HDDolby().signin({"name": "高清杜比", "url": "https://www.hddolby.com/", "apikey": "BAD"})
    assert state is False
    assert "Invalid API Key" in message


def test_hddolby_requires_api_key():
    """高清杜比未配置 API Key 时给出明确提示。"""
    from app.plugins.autosigninplus.sites.hddolby import HDDolby

    state, message = HDDolby().signin({"name": "高清杜比", "url": "https://www.hddolby.com/"})
    assert state is False
    assert "RSS Key" in message

class _RousiFakeRes:
    def __init__(self, status, payload):
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)


def test_rousipro_retries_transient_failure(monkeypatch):
    """Rousi Pro 走 Cloudflare，首次请求可能无响应：应自动重试而不是直接判失败。"""
    from app.plugins.autosigninplus.sites import rousipro as rousi_module
    from app.plugins.autosigninplus.sites.rousipro import RousiPro

    seen = []

    class FakeRequestUtils:
        def __init__(self, headers=None, **kwargs):
            self.headers = headers or {}

        def post_res(self, url=None, json=None, **kwargs):
            seen.append(self.headers)
            if len(seen) == 1:
                return None  # 首次无响应（实测会偶发出现）
            return _RousiFakeRes(400, {"code": 1, "message": "今日已签到"})

    monkeypatch.setattr(rousi_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(rousi_module.time, "sleep", lambda *_: None)

    state, message = RousiPro().signin({"name": "Rousi Pro", "url": "https://rousi.pro/",
                                        "apikey": "pgk_key", "ua": "UA"})

    assert state is True
    assert message == "今日已签到"
    assert len(seen) == 2
    assert seen[0]["api-token"] == "pgk_key"


def test_rousipro_retries_cloudflare_5xx(monkeypatch):
    """Cloudflare 520 时应重试，重试成功即视为签到成功。"""
    from app.plugins.autosigninplus.sites import rousipro as rousi_module
    from app.plugins.autosigninplus.sites.rousipro import RousiPro

    seen = []

    class FakeRequestUtils:
        def __init__(self, headers=None, **kwargs):
            self.headers = headers or {}

        def post_res(self, url=None, json=None, **kwargs):
            seen.append(1)
            if len(seen) == 1:
                return _RousiFakeRes(520, {"title": "Error 520"})
            return _RousiFakeRes(200, {"code": 0, "message": "success"})

    monkeypatch.setattr(rousi_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(rousi_module.time, "sleep", lambda *_: None)

    state, message = RousiPro().signin({"name": "Rousi Pro", "url": "https://rousi.pro/",
                                        "apikey": "pgk_key", "ua": "UA"})
    assert state is True
    assert message == "签到成功"
    assert len(seen) == 2


def test_rousipro_login_uses_api_token(monkeypatch):
    """模拟登录也使用 PeerGo 个人 API Key（api-token 请求头）。"""
    from app.plugins.autosigninplus.sites import rousipro as rousi_module
    from app.plugins.autosigninplus.sites.rousipro import RousiPro

    captured = {}

    class FakeRequestUtils:
        def __init__(self, headers=None, **kwargs):
            captured["headers"] = headers or {}

        def get_res(self, url=None, **kwargs):
            captured["url"] = url
            return _RousiFakeRes(200, {"code": 0, "message": "success"})

    monkeypatch.setattr(rousi_module, "RequestUtils", FakeRequestUtils)

    state, message = RousiPro().login({"name": "Rousi Pro", "url": "https://rousi.pro/",
                                       "apikey": "pgk_key", "ua": "UA"})
    assert state is True
    assert message == "模拟登录成功"
    assert captured["headers"]["api-token"] == "pgk_key"
    assert captured["url"].endswith("/api/v1/profile")


def test_rousipro_reports_invalid_key(monkeypatch):
    """API Key 失效时如实报错，不再当成网络问题。"""
    from app.plugins.autosigninplus.sites import rousipro as rousi_module
    from app.plugins.autosigninplus.sites.rousipro import RousiPro

    class FakeRequestUtils:
        def __init__(self, **kwargs):
            pass

        def post_res(self, **kwargs):
            return _RousiFakeRes(401, {"code": 401, "message": "unauthorized"})

    monkeypatch.setattr(rousi_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(rousi_module.time, "sleep", lambda *_: None)

    state, message = RousiPro().signin({"name": "Rousi Pro", "url": "https://rousi.pro/",
                                        "apikey": "pgk_bad", "ua": "UA"})
    assert state is False
    assert "权限不足" in message or "失效" in message
