"""后宫成员列表的跨站点清洗与分享率归一化。"""
import re
from typing import Any, Dict, List, Optional, Tuple


_HEADER_WORDS = {
    "用户名", "用户", "邮箱", "邮件", "分享率", "上传", "下载", "状态",
    "username", "user", "email", "ratio", "uploaded", "downloaded", "status",
}
_SIZE_RE = re.compile(r"([\d.,]+)\s*([KMGTPE]?i?B)\b", re.IGNORECASE)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
          "TB": 1024 ** 4, "PB": 1024 ** 5, "EB": 1024 ** 6}


def _size_to_bytes(value: Any) -> Optional[float]:
    match = _SIZE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
        unit = match.group(2).upper().replace("I", "")
        return number * _UNITS.get(unit, 1)
    except ValueError:
        return None


def _ratio(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text or text.lower() in _HEADER_WORDS or _SIZE_RE.search(text):
        return None
    if text.lower() in {"∞", "inf", "inf.", "infinite", "无限"}:
        return float("inf")
    match = re.search(r"-?\d+(?:[.,]\d+)?", text.replace(",", "."))
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None


def _ratio_label(value: Optional[float]) -> Tuple[str, List[str]]:
    if value is None:
        return "neutral", ["无数据", "text-grey"]
    if value == float("inf"):
        return "excellent", ["分享率无限", "text-success"]
    if value >= 1:
        return "good", ["正常", "text-success"]
    if value >= 0.4:
        return "warning", ["较低", "text-warning"]
    if value > 0:
        return "danger", ["危险", "text-error"]
    return "neutral", ["无数据", "text-grey"]


def sanitize_invitees(invitees: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """丢弃误抓表头、修正错列分享率，并为界面补齐健康状态。"""
    cleaned = []
    for invitee in invitees or []:
        if not isinstance(invitee, dict):
            continue
        username = str(invitee.get("username") or "").strip()
        if not username or username.lower() in _HEADER_WORDS:
            continue

        ratio = _ratio(invitee.get("ratio"))
        if ratio is None:
            uploaded = _size_to_bytes(invitee.get("uploaded"))
            downloaded = _size_to_bytes(invitee.get("downloaded"))
            if uploaded is not None and downloaded is not None:
                ratio = float("inf") if downloaded == 0 and uploaded > 0 else (
                    uploaded / downloaded if downloaded > 0 else None
                )
                if ratio is not None:
                    invitee["ratio"] = "∞" if ratio == float("inf") else f"{ratio:.3f}"
            elif _SIZE_RE.search(str(invitee.get("ratio") or "")):
                invitee["ratio"] = ""

        health, label = _ratio_label(ratio)
        invitee["ratio_health"] = health
        invitee["ratio_label"] = label
        cleaned.append(invitee)
    return cleaned


# 各站点「不可邀请」提示的常见文案（含简体/繁体/英文措辞差异）
_INVITE_REASON_PATTERNS = (
    r"当前邀请权限为[^。;<]{0,40}无法邀请[。.]?",
    r"您的权限不够[，,]?无法邀请[。.]?",
    r"只有[^，。;<]{0,24}才能(?:发送|發送)(?:邀请|邀請)[。.]?",
    r"[^，。;<]{0,30}(?:及以上|或以上)[^，。;<]{0,12}(?:等级|等級)?[^，。;<]{0,6}"
    r"(?:才可以|才能)[^，。;<]{0,4}(?:发送|發送)(?:邀请|邀請)[。.]?",
    r"[^，。;<]{0,24}才可以(?:发送|發送)(?:邀请|邀請)[。.]?",
    r"(?:发送|發送)(?:邀请|邀請)的最低(?:等级|等級)是[:：]\s*\S+",
    r"(?:购买|購買)(?:邀请|邀請)的最低(?:等级|等級)是[:：]\s*\S+",
    r"(?:邀请|邀請)系统已关闭",
    r"(?:邀请|邀請)已?关闭",
    r"当前账户上限数已到",
    r"已达到最大(?:邀请|邀請)数",
    r"没有剩余(?:邀请|邀請)",
    r"(?:邀请|邀請)(?:数量|數量|名额|名額)不足",
)

_CONTROL_REASON_WORDS = ("不足", "及以上", "等級", "等级", "权限", "權限", "上限")


def _clean_reason(reason: str) -> str:
    """清理原因文本里的“这里返回”等页面拼接残留。"""
    reason = re.sub(r"\s*这[里裏].{0,4}返回。?", "", reason)
    reason = re.sub(r"\s+", " ", reason or "").strip()
    return reason.strip("。. ")


def extract_invite_reason(html: str) -> str:
    """
    从页面文本中提取不可邀请的具体原因。

    兼容中英文/简繁措辞，并会读取 disabled 按钮/输入框的文案
    （如 NicePT 的 “邀請數量不足”，这类文案不在可见文本里）。
    """
    if not html:
        return ""
    plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"\s+", " ", plain)
    for pattern in _INVITE_REASON_PATTERNS:
        match = re.search(pattern, plain)
        if match:
            reason = _clean_reason(match.group(0))
            if reason:
                return reason
    for control in re.finditer(r'(?:value|title|aria-label)="([^"]{2,60})"', html):
        value = _clean_reason(control.group(1))
        if value and any(word in value for word in _CONTROL_REASON_WORDS):
            return value
    return ""
