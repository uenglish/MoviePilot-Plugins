"""Read-only, paginated view models for the invitation dashboard.

Summary responses never include member lists. A site's members and historical
points are requested only when its detail panel opens. No function visits a site.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
from urllib.parse import urlsplit, urlunsplit

from .categories import CATEGORY_META, classify, normalize_record
from .parsing import sanitize_invitees
from .trend import diff_snapshots

_CACHE_LOCK = RLock()
_PAGE_SIZES = (10, 20, 50, 100)
_STALE_SECONDS = 12 * 3600
_MEMBER_FIELDS = (
    "username", "email", "uploaded", "downloaded", "ratio", "seeding", "seeding_size",
    "seed_magic", "seed_bonus", "last_seed_report", "status", "data_status",
)


def _number(value, default=None):
    """Keep unavailable amounts distinct from a genuine zero and reject NaN."""
    if value is None or isinstance(value, bool) or str(value).strip() in ("", "-", "—"):
        return default
    try:
        number = float(str(value).replace(",", ""))
        if not math.isfinite(number):
            return default
        return int(number) if number.is_integer() else number
    except (ValueError, TypeError, OverflowError):
        return default


def _text(value, limit=1200):
    """Bound upstream text while retaining enough of a failure for diagnosis."""
    return str(value if value is not None else "").strip()[:limit]


def _safe_url(value):
    """Expose an ordinary profile link without credentials or query secrets."""
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return ""
        # Profile IDs are legitimate query parameters; strip any credential-like key.
        from urllib.parse import parse_qsl, urlencode
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in {"apikey", "api_key", "key", "passkey", "token", "auth", "authorization"}]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except (ValueError, TypeError):
        return ""


def _fingerprint(manager):
    """Invalidate snapshots when either cache file changes, without a TTL lag."""
    values = []
    for attribute in ("data_file", "history_file"):
        path = getattr(manager, attribute, None)
        if not path:
            return None
        path = Path(path)
        try:
            stat = path.stat()
            values.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            values.append((str(path), None, None))
    return tuple(values)


def _read_file(path, expected):
    """Read one cache file directly so a partial write cannot masquerade as empty."""
    path = Path(path)
    if not path.exists():
        return expected()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, expected):
        raise ValueError("Invalid invitation cache shape")
    return value


def _members(record):
    """Use the established cleanup rules without modifying the stored snapshot."""
    value = normalize_record(record).get("invitees")
    return sanitize_invitees(deepcopy(value if isinstance(value, list) else []))


def _member_state(member):
    """Classify banned members separately from sharing-ratio and missing-data flags."""
    enabled = str(member.get("enabled", "")).strip().lower()
    if member.get("enabled") is False or enabled in ("no", "false", "0", "disabled"):
        return "banned"
    if member.get("ratio_health") in ("warning", "danger"):
        return "low_ratio"
    if member.get("ratio_health") == "neutral":
        return "no_data"
    return "healthy"


def _row(name, record):
    """Flatten a cache record into a bounded table row, omitting member details."""
    normalized = normalize_record(record)
    status = normalized.get("invite_status") or {}
    if not isinstance(status, dict):
        status = {}
    safe_record = {"data": {"invite_status": dict(status), "invitees": []},
                   "last_update": normalized.get("last_update")}
    if normalized.get("error"):
        safe_record["data"]["error"] = normalized["error"]
    for key in ("permanent_count", "temporary_count"):
        safe_record["data"]["invite_status"][key] = _number(status.get(key), 0)
    meta = classify(safe_record)
    members = _members(record)
    states = [_member_state(member) for member in members]
    permanent = _number(status.get("permanent_count"))
    temporary = _number(status.get("temporary_count"))
    bonus = _number(status.get("bonus"))
    permanent_price = _number(status.get("permanent_invite_price"))
    temporary_price = _number(status.get("temporary_invite_price"))
    reason = _text(meta["reason"])
    reported_bonus = re.search(r"魔力值[（(]([\d,.]+)[)）]", reason)
    if bonus is None and reported_bonus:
        bonus = _number(reported_bonus.group(1))
    reported_buyable = re.search(r"可购买(\d+)个临时邀请", reason)
    ordinary = record.get("data", record) if isinstance(record, dict) else {}
    ordinary = ordinary if isinstance(ordinary, dict) else {}
    return {
        "id": str(name), "name": _text(name, 200),
        "category": meta["key"], "label": meta["label"], "color": meta["color"],
        "needs_action": bool(meta["action"]), "reason": reason,
        "advice": _text(meta["advice"]), "can_invite": bool(status.get("can_invite")),
        "permanent": permanent, "temporary": temporary,
        "quota": _number(status.get("remaining_count")) if permanent is None and temporary is None else (permanent or 0) + (temporary or 0),
        "bonus": bonus, "permanent_price": permanent_price, "temporary_price": temporary_price,
        "purchasable_permanent": int(bonus // permanent_price) if bonus is not None and permanent_price and permanent_price > 0 else None,
        "purchasable_temporary": int(bonus // temporary_price) if bonus is not None and temporary_price and temporary_price > 0 else None,
        "reported_purchasable_temporary": int(reported_buyable.group(1)) if reported_buyable else None,
        "members": len(members), "low_ratio": states.count("low_ratio"),
        "banned": states.count("banned"), "no_data": states.count("no_data"),
        "updated_at": _number(ordinary.get("last_success_at") if ordinary.get("fetch_failed") else normalized.get("last_update")),
        "attempted_at": _number(normalized.get("last_update")),
        "cached_after_failure": bool(ordinary.get("fetch_failed")),
    }


def _snapshot(host):
    """Cache parsed local files and summaries, retaining the prior view during a write."""
    manager = host.data_manager
    with _CACHE_LOCK:
        signature = _fingerprint(manager)
        cached = getattr(host, "_invitation_ui_snapshot", None)
        if cached and signature is not None and cached["signature"] == signature:
            return cached
        try:
            if signature is None:
                records = manager.get_site_data() or {}
                history = manager.load_history() or []
            else:
                records = _read_file(manager.data_file, dict)
                history = _read_file(manager.history_file, list)
            records = {str(name): record for name, record in records.items() if isinstance(record, dict)}
            rows = [_row(name, record) for name, record in records.items()]
            history = [row for row in history if isinstance(row, dict)][-30:]
        except (OSError, ValueError, TypeError, AttributeError):
            if cached:
                return cached
            records, rows, history = {}, [], []
            signature = None  # Retry on the next request after an interrupted write.
        snapshot = {"signature": signature, "records": records, "rows": rows, "history": history}
        host._invitation_ui_snapshot = snapshot
        return snapshot


def _pagination(page, page_size, total):
    """Clamp page controls to stable bounds after a search or filter changes."""
    try:
        size = int(page_size)
    except (ValueError, TypeError, OverflowError):
        size = 20
    size = size if size in _PAGE_SIZES else min(100, max(1, size))
    pages = max(1, math.ceil(total / size))
    try:
        current = min(pages, max(1, int(page)))
    except (ValueError, TypeError, OverflowError):
        current = 1
    return current, size, pages


def _priorities(host):
    """Resolve the user's site ordering without returning its connection fields."""
    try:
        return {str(site.get("name")): index for index, site in enumerate(host.sites.get_indexers())}
    except (AttributeError, TypeError):
        return {}


def _site_identity(host):
    """Only expose the configured homepage and native ID used by the host icon API."""
    try:
        return {str(site.get("name")): {"url": _safe_url(site.get("url")),
                "site_id": str(site['id']) if str(site.get('id', '')).isdigit() else None}
                for site in host.sites.get_indexers()}
    except (AttributeError, TypeError):
        return {}


def _filtered(rows, query, category, now):
    query = _text(query, 200).casefold()
    selected = []
    for raw in rows:
        row = dict(raw)
        updated = row["updated_at"]
        row["stale"] = updated is None or now - updated > _STALE_SECONDS
        if query and query not in (row["name"] + " " + row["reason"]).casefold():
            continue
        if category == "problem" and not row["needs_action"]:
            continue
        if category == "stale" and not row["stale"]:
            continue
        if category not in ("", "all", "problem", "stale") and row["category"] != category:
            continue
        selected.append(row)
    return selected


def _sorted(rows, sort, priorities):
    default = lambda row: (priorities.get(row["name"], len(priorities) + 1), row["name"].casefold())
    if sort in ("quota", "quota_desc"):
        return sorted(rows, key=lambda row: (-(row["quota"] or 0), *default(row)))
    if sort in ("members", "members_desc"):
        return sorted(rows, key=lambda row: (-row["members"], *default(row)))
    if sort in ("updated", "updated_desc"):
        return sorted(rows, key=lambda row: (-(row["updated_at"] or 0), *default(row)))
    if sort in ("status", "problem"):
        return sorted(rows, key=lambda row: (not row["needs_action"], -CATEGORY_META[row["category"]]["weight"], *default(row)))
    if sort == "name":
        return sorted(rows, key=lambda row: row["name"].casefold())
    return sorted(rows, key=default)


def _trend(history, site=None):
    """Return compact points and bounded change lists, not full historical snapshots."""
    points = []
    for snapshot in history:
        sites = snapshot.get("sites") or {}
        if not isinstance(sites, dict):
            continue
        if site is not None:
            record = sites.get(site)
            if not isinstance(record, dict):
                continue
            points.append({"time": _number(snapshot.get("time")), "category": _text(record.get("category"), 30),
                           "quota": (_number(record.get("permanent"), 0) + _number(record.get("temporary"), 0)),
                           "members": _number(record.get("invitees"), 0)})
        else:
            records = [row for row in sites.values() if isinstance(row, dict)]
            points.append({"time": _number(snapshot.get("time")), "sites": len(records),
                           "invitable": sum(row.get("category") == "invitable" for row in records),
                           "problem": sum(bool(CATEGORY_META.get(row.get("category"), {}).get("action")) for row in records),
                           "quota": sum(_number(row.get("permanent"), 0) + _number(row.get("temporary"), 0) for row in records),
                           "members": sum(_number(row.get("invitees"), 0) for row in records)})
    return points


def _change_value(value):
    """Limit historical changes to the compact fields emitted by this plugin."""
    if isinstance(value, dict):
        return {"category": _text(value.get("category"), 30),
                **{field: _number(value.get(field)) for field in ("invitees", "permanent", "temporary")}}
    return _text(value, 30)


def list_sites(host, query="", category="all", sort="priority", page=1, page_size=20, now=None):
    """Return dashboard totals plus one filtered page of invitation summaries."""
    now = time.time() if now is None else now
    snapshot = _snapshot(host)
    all_rows = _filtered(snapshot["rows"], "", "all", now)
    selected = _sorted(_filtered(snapshot["rows"], query, category, now), sort, _priorities(host))
    current, size, pages = _pagination(page, page_size, len(selected))
    fresh = [row for row in all_rows if not row["cached_after_failure"]]
    counts = {key: sum(row["category"] == key for row in all_rows) for key in CATEGORY_META}
    history = snapshot["history"]
    changes = diff_snapshots(history[-2], history[-1]) if len(history) > 1 else {}
    changes = {key: [{"name": _text(row.get("name"), 200),
                     "from": _change_value(row.get("from")), "to": _change_value(row.get("to"))} for row in value[:8]]
               for key, value in changes.items()}
    identities = _site_identity(host)
    items = selected[(current - 1) * size:current * size]
    for item in items:
        item.update(identities.get(item["id"], {"url": "", "site_id": None}))
    return {
        "summary": {"sites": len(all_rows), "invitable": counts["invitable"],
                    "problem": sum(row["needs_action"] for row in all_rows),
                    "stale": sum(row["stale"] for row in all_rows),
                    "quota": sum(row["quota"] or 0 for row in fresh),
                    "permanent": sum(row["permanent"] or 0 for row in fresh),
                    "temporary": sum(row["temporary"] or 0 for row in fresh),
                    "members": sum(row["members"] for row in fresh),
                    "low_ratio": sum(row["low_ratio"] for row in fresh),
                    "banned": sum(row["banned"] for row in fresh),
                    "no_data": sum(row["no_data"] for row in fresh)},
        "categories": [{"key": key, "label": meta["label"], "color": meta["color"], "count": counts[key]}
                       for key, meta in CATEGORY_META.items()],
        "items": items,
        "total": len(selected), "page": current, "page_size": size, "pages": pages,
        "updated_at": max((row["updated_at"] or 0 for row in all_rows), default=0) or None,
        "refreshing": bool(getattr(host, "_refreshing", False)),
        "trend": {"points": _trend(history), "changes": changes},
    }


def site_detail(host, site, member_query="", member_status="all", page=1, page_size=20):
    """Return one site's member page and history, loaded on demand by its drawer."""
    snapshot = _snapshot(host)
    record = snapshot["records"].get(str(site))
    if record is None:
        return {"found": False, "site": None, "items": [], "total": 0, "page": 1,
                "page_size": 20, "pages": 1, "history": []}
    summary = next(row for row in snapshot["rows"] if row["id"] == str(site))
    query = _text(member_query, 200).casefold()
    members = []
    for member in _members(record):
        state = _member_state(member)
        if member_status not in ("all", "") and state != member_status:
            continue
        if query and query not in (str(member.get("username", "")) + " " + str(member.get("email", ""))).casefold():
            continue
        item = {key: _text(member.get(key)) for key in _MEMBER_FIELDS}
        item.update({"profile_url": _safe_url(member.get("profile_url")), "health": state,
                     "ratio_health": _text(member.get("ratio_health"), 30),
                     "ratio_label": _text((member.get("ratio_label") or ["无数据"])[0], 50),
                     "banned": state == "banned"})
        item["seed_magic"] = item["seed_magic"] or _text(member.get("magic") or member.get("seed_time"))
        item["seed_bonus"] = item["seed_bonus"] or _text(member.get("invitee_bonus") or member.get("bonus"))
        item["last_seed_report"] = item["last_seed_report"] or _text(member.get("last_seen"))
        members.append(item)
    order = {"banned": 0, "low_ratio": 1, "no_data": 2, "healthy": 3}
    members.sort(key=lambda row: (order[row["health"]], row["username"].casefold()))
    current, size, pages = _pagination(page, page_size, len(members))
    return {"found": True, "site": {**summary, **_site_identity(host).get(str(site), {"url": "", "site_id": None})},
            "items": members[(current - 1) * size:current * size],
            "total": len(members), "page": current, "page_size": size, "pages": pages,
            "history": _trend(snapshot["history"], str(site))}


def export_rows(host, query="", category="all"):
    """Export the selected site summaries without member email or connection secrets."""
    rows = _sorted(_filtered(_snapshot(host)["rows"], query, category, time.time()), "priority", _priorities(host))
    return [{"站点": row["name"], "状态": row["label"], "原因": row["reason"],
             "永久名额": row["permanent"], "临时名额": row["temporary"], "魔力": row["bonus"],
             "成员": row["members"], "低分享率": row["low_ratio"], "已禁用": row["banned"],
             "更新时间": row["updated_at"]} for row in rows]
