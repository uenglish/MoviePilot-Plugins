"""后宫管理系统（增强版）核心逻辑测试：状态归类、变化趋势、Cookie/域名规整。

这些模块只依赖标准库与 requests，可直接导入，不需要 MoviePilot 运行环境。
"""
import importlib.util
import pathlib
import sys

import pytest

def _find_plugin_dir() -> pathlib.Path:
    """向上查找插件目录，兼容在仓库任意层级运行测试。"""
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "plugins.v2" / "nexusinviteeplus"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("未找到插件目录 plugins.v2/nexusinviteeplus")


PLUGIN_DIR = _find_plugin_dir()


def _load(name):
    """按文件路径加载插件内的纯逻辑模块。"""
    path = PLUGIN_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"harem_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


categories = _load("categories")
parsing = _load("parsing")
trend = _load("trend")
site_access = _load("site_access")


def test_normalize_cookie_removes_newlines():
    """CookieCloud 同步的换行 Cookie 必须规整后才能放进请求头。"""
    raw = "c_secure_pass=abc;\nc_secure_uid=MTIz;\r\nc_secure_ssl=bm9wZQ=="
    assert site_access.normalize_cookie(raw) == "c_secure_pass=abc;c_secure_uid=MTIz;c_secure_ssl=bm9wZQ=="
    assert site_access.normalize_cookie("a=b; c=d") == "a=b; c=d"
    assert site_access.normalize_cookie(None) == ""


def test_normalize_ua_falls_back_to_default():
    """站点未配置 UA 时使用默认 UA，避免请求被拒绝。"""
    assert site_access.normalize_ua("") == site_access.DEFAULT_UA
    assert site_access.normalize_ua("  ") == site_access.DEFAULT_UA
    assert site_access.normalize_ua("MyUA/1.0") == "MyUA/1.0"


def test_swap_www():
    """www 与非 www 主机名互换，用于纠正站点跳转导致误判。"""
    assert site_access.swap_www("https://pttime.org/") == "https://www.pttime.org/"
    assert site_access.swap_www("https://www.pttime.org/index.php") == "https://pttime.org/index.php"
    assert site_access.swap_www("") is None


@pytest.mark.parametrize("record, expected", [
    ({"data": {"invite_status": {"can_invite": True, "permanent_count": 3,
                                 "reason": "存在可用邀请表单"}}}, "invitable"),
    ({"data": {"invite_status": {"can_invite": True,
                                 "reason": "可以发送邀请，但当前邀请数量不足"}}}, "quota"),
    ({"data": {"invite_status": {"can_invite": False,
                                 "reason": "当前邀请权限为 Crazy User 及以上，您的权限不够，无法邀请。"}}}, "level"),
    ({"data": {"invite_status": {"can_invite": False,
                                 "reason": "发送邀请的最低等级是： 宅护法"}}}, "level"),
    ({"data": {"invite_status": {"can_invite": False, "reason": "邀请系统已关闭"}}}, "closed"),
    ({"data": {"invite_status": {"can_invite": False,
                                 "reason": "Cookie 已失效，请重新登录站点更新 Cookie"}}}, "cookie"),
    ({"data": {"invite_status": {"can_invite": False,
                                 "reason": "检测到 Cloudflare 验证页，请在站点中正常完成验证后更新 Cookie"}}}, "cf"),
    ({"data": {"error": "站点信息不完整: Cookie"}}, "noconfig"),
    ({"data": {"error": "站点服务异常（HTTP 500）"}}, "site"),
    ({"data": {"invite_status": {"can_invite": False, "reason": "当前账户上限数已到"}}}, "limit"),
    ({"data": {"invite_status": {"can_invite": False, "reason": "邀請數量不足"}}}, "quota"),
    ({"data": {"invite_status": {"can_invite": False,
                                 "reason": "Elite User(筑基) 或以上等級才可以發送邀請"}}}, "level"),
    ({"data": {"invite_status": {"can_invite": False,
                                 "reason": "新平台账号已校验（user），邀请信息请在「用户成长 → Invite」页面查看"}}}, "platform"),
])
def test_classify_site_category(record, expected):
    """各类站点返回原因应归入对应类别，而不是一律当成失败。"""
    assert categories.classify(record)["key"] == expected


def test_summarize_groups_and_orders():
    """汇总统计要给出计数、可邀请列表与需处理列表（按处理优先级排序）。"""
    sites = {
        "A站": {"data": {"invite_status": {"can_invite": True, "permanent_count": 2, "reason": "存在可用邀请表单"}}},
        "B站": {"data": {"invite_status": {"can_invite": False, "reason": "等级不足，需要 Power User"}}},
        "C站": {"data": {"error": "Cookie 已失效"}},
        "D站": {"data": {"error": "站点服务异常（HTTP 500）"}},
    }
    summary = categories.summarize(sites)
    assert summary["counts"]["invitable"] == 1
    assert [item["name"] for item in summary["invitable_sites"]] == ["A站"]
    assert [item["name"] for item in summary["action_sites"]] == ["C站", "D站"]


def test_trend_detects_new_problem_and_recovery():
    """趋势对比要能识别新异常、恢复与名额变化。"""
    before = {"time": 1, "sites": {"A站": {"category": "invitable", "invitees": 0, "permanent": 1},
                                   "B站": {"category": "cookie", "invitees": 0, "permanent": 0},
                                   "C站": {"category": "level", "invitees": 0, "permanent": 0}}}
    after = {"time": 2, "sites": {"A站": {"category": "cookie", "invitees": 0, "permanent": 0},
                                  "B站": {"category": "invitable", "invitees": 2, "permanent": 1},
                                  "C站": {"category": "invitable", "invitees": 1, "permanent": 1}}}
    diff = trend.diff_snapshots(before, after)
    # 可邀请 -> Cookie 失效：新异常
    assert [item["name"] for item in diff["new_problem"]] == ["A站"]
    # Cookie 失效 -> 可邀请：已恢复
    assert [item["name"] for item in diff["recovered"]] == ["B站"]
    # 新出现可邀请名额的站点
    assert {item["name"] for item in diff["invitable"]} == {"B站", "C站"}
    assert trend.has_changes(diff)


def test_build_snapshot_shape():
    """快照结构应包含类别与名额，便于后续对比。"""
    snapshot = trend.build_snapshot(
        {"A站": {"data": {"invite_status": {"can_invite": True, "permanent_count": 1,
                                            "reason": "存在可用邀请表单"}}}},
        categories.classify, 123)
    assert snapshot["time"] == 123
    assert snapshot["sites"]["A站"] == {"category": "invitable", "invitees": 0, "permanent": 1, "temporary": 0}


@pytest.mark.parametrize("html, expected", [
    ("<td>对不起，只有 能天使 及以上的用户才能发送邀请。</td>",
     "只有 能天使 及以上的用户才能发送邀请"),
    ("<b>发送邀请的最低等级是： </b><span>宅护法</span>", "发送邀请的最低等级是： 宅护法"),
    ('<input disabled type="submit" value="邀請數量不足"/>', "邀請數量不足"),
    ("<div>Elite User(筑基) 或以上等級才可以發送邀請这里返回。</div>",
     "Elite User(筑基) 或以上等級才可以發送邀請"),
    ("<div>当前邀请权限为 Crazy User 及以上，您的权限不够，无法邀请。</div>",
     "当前邀请权限为 Crazy User 及以上，您的权限不够，无法邀请"),
    ("<div>邀请系统已关闭</div>", "邀请系统已关闭"),
    ("<div>没有相关内容</div>", ""),
])
def test_extract_invite_reason(html, expected):
    """各站点「不可邀请」文案（含繁体与控件文案）都能提取出具体原因。"""
    assert parsing.extract_invite_reason(html) == expected
