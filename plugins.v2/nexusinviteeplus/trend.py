"""刷新历史对比：找出新失效、新恢复与邀请数量变化，供详情页与通知展示。"""
from typing import Any, Dict, List, Optional

# 需要用户处理的异常类别
PROBLEM_CATEGORIES = ("cookie", "cf", "site", "noconfig", "error", "unknown")


def build_snapshot(sites: Dict[str, dict], classify, timestamp: int) -> Dict[str, Any]:
    """
    生成一次刷新的紧凑快照。

    :param sites: {站点名: 站点数据}
    :param classify: 归类函数
    :param timestamp: 刷新时间戳
    """
    compact = {}
    for name, record in (sites or {}).items():
        meta = classify(record)
        compact[name] = {
            "category": meta["key"],
            "invitees": meta["invitee_count"],
            "permanent": meta["permanent_count"],
            "temporary": meta["temporary_count"],
        }
    return {"time": int(timestamp), "sites": compact}


def diff_snapshots(previous: Optional[dict], current: dict) -> Dict[str, List[dict]]:
    """
    对比两次快照。

    :return: {"new_problem": [...], "recovered": [...], "invitable": [...], "changed": [...]}
    """
    result: Dict[str, List[dict]] = {"new_problem": [], "recovered": [], "invitable": [], "changed": []}
    prev_sites = (previous or {}).get("sites") or {}
    current_sites = (current or {}).get("sites") or {}
    for name, info in current_sites.items():
        old = prev_sites.get(name)
        if old is None:
            continue
        old_cat, new_cat = old.get("category"), info.get("category")
        if old_cat == new_cat:
            if old.get("invitees") != info.get("invitees") or old.get("permanent") != info.get("permanent"):
                result["changed"].append({"name": name, "from": old, "to": info})
            continue
        if new_cat in PROBLEM_CATEGORIES and old_cat not in PROBLEM_CATEGORIES:
            result["new_problem"].append({"name": name, "from": old_cat, "to": new_cat})
        elif old_cat in PROBLEM_CATEGORIES and new_cat not in PROBLEM_CATEGORIES:
            result["recovered"].append({"name": name, "from": old_cat, "to": new_cat})
        if new_cat == "invitable" and old_cat != "invitable":
            result["invitable"].append({"name": name, "to": new_cat})
    return result


def has_changes(diff: Dict[str, List[dict]]) -> bool:
    """是否存在需要提示的变化。"""
    return any(diff.get(key) for key in ("new_problem", "recovered", "invitable", "changed"))
