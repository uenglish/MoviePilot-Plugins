"""站点状态归类：把各站点的邀请状态归到「可邀请 / 等级不足 / 邀请关闭 / 异常」等类别。

用于详情页分组展示、仪表盘统计、通知汇总与趋势对比，避免所有站点混在一张表里。
"""
from typing import Any, Dict, List, Optional, Tuple

# 类别定义：label 展示名，color 前端配色，icon 图标，advice 处理建议，action 是否需要用户处理
CATEGORY_META: Dict[str, Dict[str, Any]] = {
    "invitable": {
        "label": "可邀请", "color": "success", "icon": "mdi-account-plus",
        "advice": "有名额，可直接发送邀请", "action": False, "weight": 0,
    },
    "quota": {
        "label": "名额不足", "color": "info", "icon": "mdi-ticket-outline",
        "advice": "等级已够但没有名额，可等待发放或使用魔力兑换", "action": False, "weight": 1,
    },
    "level": {
        "label": "等级不足", "color": "grey", "icon": "mdi-arrow-up-bold-circle-outline",
        "advice": "等级未达标，提升分享率/做种量后自动可用", "action": False, "weight": 2,
    },
    "closed": {
        "label": "邀请关闭", "color": "grey", "icon": "mdi-lock-outline",
        "advice": "站点已关闭邀请，无需处理", "action": False, "weight": 3,
    },
    "limit": {
        "label": "已达上限", "color": "info", "icon": "mdi-account-multiple-check",
        "advice": "邀请数已达账号上限，等待被邀者确认或到期", "action": False, "weight": 3,
    },
    "unsupported": {
        "label": "无邀请功能", "color": "grey", "icon": "mdi-information-outline",
        "advice": "站点未提供邀请入口，可忽略", "action": False, "weight": 4,
    },
    "unknown": {
        "label": "待确认", "color": "grey", "icon": "mdi-help-circle-outline",
        "advice": "未能识别具体原因，建议手动查看站点页面", "action": True, "weight": 6,
    },
    "noconfig": {
        "label": "未配置", "color": "warning", "icon": "mdi-key-remove",
        "advice": "站点未配置 Cookie/API 密钥，请到站点设置中补充", "action": True, "weight": 9,
    },
    "cookie": {
        "label": "Cookie 失效", "color": "error", "icon": "mdi-cookie-off",
        "advice": "重新登录站点或同步 CookieCloud 后更新 Cookie", "action": True, "weight": 8,
    },
    "cf": {
        "label": "CF 验证", "color": "error", "icon": "mdi-shield-alert-outline",
        "advice": "浏览器打开站点完成验证，再同步新的 Cookie", "action": True, "weight": 7,
    },
    "site": {
        "label": "站点故障", "color": "error", "icon": "mdi-server-off",
        "advice": "站点侧异常（维护/500），恢复后重试", "action": True, "weight": 4,
    },
    "error": {
        "label": "请求失败", "color": "error", "icon": "mdi-alert-circle",
        "advice": "请求失败，可点击刷新重试", "action": True, "weight": 5,
    },
}

# 关键词 -> 类别（按顺序匹配，先命中先归类）
_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    ("cf", ("cloudflare", "cf 验证", "验证页")),
    ("cookie", ("cookie 已失效", "cookie已失效", "cookie 失效")),
    ("noconfig", ("站点信息不完整", "未配置", "缺少 cookie")),
    ("site", ("站点服务异常", "http 500", "http 502", "http 503", "维护")),
    ("unsupported", ("无邀请功能", "未提供邀请", "不支持邀请")),
    ("closed", ("邀请系统已关闭", "邀请已关闭", "关闭了邀请", "邀请功能已关闭")),
    ("limit", ("上限", "已达最大邀请数")),
    ("quota", ("数量不足", "名额不足", "没有足够的邀请", "没有剩余邀请", "剩余邀请0", "剩余邀请 0")),
    ("level", ("等级", "及以上", "权限不够", "最低等级", "才能发送邀请", "才可以发送邀请",
               "才能邀请", "权限为", "无法邀请", "等级不足", "貴賓", "贵宾")),
]


def normalize_record(record: Optional[dict]) -> Dict[str, Any]:
    """把站点数据统一成 {invite_status, invitees, error, last_update} 结构。"""
    record = record or {}
    data = record.get("data") if isinstance(record.get("data"), dict) else record
    inner = data.get("data") if isinstance(data.get("data"), dict) and "invite_status" not in data else data
    inner = inner or {}
    return {
        "invite_status": inner.get("invite_status") or {},
        "invitees": inner.get("invitees") or [],
        "error": inner.get("error") or data.get("error") or record.get("error"),
        "last_update": record.get("last_update") or data.get("last_update"),
    }


def classify(record: Optional[dict]) -> Dict[str, Any]:
    """
    归类单个站点状态。

    :param record: 站点数据（支持 data 包装或裸 invite_status 结构）
    :return: 类别信息字典，含 key/label/color/icon/advice/action/reason/counts
    """
    norm = normalize_record(record)
    status = norm["invite_status"] or {}
    reason = str(status.get("reason") or norm["error"] or "").strip()
    text = reason.lower()

    key = None
    if status.get("can_invite"):
        # 可邀请：有名额或明确说明名额不足
        key = "quota" if any(word in reason for word in ("不足", "没有剩余", "名额")) else "invitable"
    else:
        # 先按原因文本归类到具体类别，识别不出再退回通用「请求失败」
        for candidate, words in _PATTERNS:
            if any(word.lower() in text for word in words):
                key = candidate
                break
        if not key and norm["error"]:
            key = "error"
    if not key:
        key = "unknown"

    meta = dict(CATEGORY_META.get(key, CATEGORY_META["unknown"]))
    meta.update({
        "key": key,
        "reason": reason or meta["label"],
        "permanent_count": int(status.get("permanent_count") or 0),
        "temporary_count": int(status.get("temporary_count") or 0),
        "invitee_count": len(norm["invitees"] or []),
        "bonus": status.get("bonus") or 0,
        "permanent_invite_price": status.get("permanent_invite_price") or 0,
        "temporary_invite_price": status.get("temporary_invite_price") or 0,
        "last_update": norm["last_update"],
        "can_invite": bool(status.get("can_invite")),
    })
    return meta


def summarize(sites: Dict[str, dict]) -> Dict[str, Any]:
    """
    汇总所有站点的类别分布。

    :param sites: {站点名: 站点数据}
    :return: {"counts": {类别: 数量}, "action_sites": [...], "invitable_sites": [...]}
    """
    counts: Dict[str, int] = {}
    action_sites: List[Dict[str, Any]] = []
    invitable_sites: List[Dict[str, Any]] = []
    for name, record in (sites or {}).items():
        meta = classify(record)
        counts[meta["key"]] = counts.get(meta["key"], 0) + 1
        item = {"name": name, **meta}
        if meta["action"]:
            action_sites.append(item)
        if meta["key"] == "invitable":
            invitable_sites.append(item)
    action_sites.sort(key=lambda item: (-CATEGORY_META.get(item["key"], {}).get("weight", 0), item["name"]))
    invitable_sites.sort(key=lambda item: (-(item["permanent_count"] + item["temporary_count"]), item["name"]))
    return {"counts": counts, "action_sites": action_sites, "invitable_sites": invitable_sites}
