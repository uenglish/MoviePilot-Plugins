"""增强版详情页组件：操作入口、原因归类、可邀请聚合、需处理清单、变化趋势与分组明细。

所有组件遵循 MoviePilot 插件页面的 component/props/content 结构；
交互（刷新/导出）通过 events.click.api 调用插件自身 API。
"""
import time
from typing import Any, Dict, List, Optional

from . import categories as site_categories
from .categories import CATEGORY_META

PROBLEM_ORDER = ["cookie", "cf", "site", "noconfig", "error", "unknown"]


def _stat_chip(label: str, value: Any, color: str, icon: Optional[str] = None) -> dict:
    """紧凑统计块（数字 + 说明）。"""
    return {
        "component": "VCol",
        "props": {"cols": "auto"},
        "content": [{
            "component": "VChip",
            "props": {"color": color, "variant": "tonal", "size": "small",
                      "prepend-icon": icon} if icon else {"color": color, "variant": "tonal", "size": "small"},
            "text": f"{label} {value}",
        }],
    }


def build_actions(plugin_id: str, apikey: str, problem_count: int, stale_count: int = 0) -> dict:
    """顶部操作卡片：一键刷新（全部/仅异常）与数据导出。"""
    def button(text: str, api: str, icon: str, color: str, variant: str = "tonal") -> dict:
        return {
            "component": "VBtn",
            "props": {"color": color, "variant": variant, "size": "small", "prepend-icon": icon},
            "text": text,
            "events": {"click": {"api": api, "method": "get"}},
        }

    base = f"plugin/{plugin_id}"
    buttons = [
        button("刷新全部站点", f"{base}/refresh_now?apikey={apikey}&scope=all", "mdi-refresh", "primary"),
        button(f"仅刷新异常站点（{problem_count}）",
               f"{base}/refresh_now?apikey={apikey}&scope=problem", "mdi-wrench", "warning"),
        button("导出 CSV", f"{base}/export_data?apikey={apikey}&format=csv", "mdi-download", "info"),
        button("导出 JSON", f"{base}/export_data?apikey={apikey}&format=json", "mdi-code-json", "info"),
    ]
    if stale_count:
        buttons.append(button(f"刷新超期站点（{stale_count}）",
                              f"{base}/refresh_now?apikey={apikey}&scope=stale", "mdi-clock-alert", "secondary"))
    return {
        "component": "VCard",
        "props": {"class": "mb-4", "variant": "flat"},
        "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-2 d-flex align-center"},
             "content": [{"component": "VIcon", "props": {"class": "mr-2"}, "text": "mdi-lightning-bolt"},
                         {"component": "span", "text": "快捷操作"}]},
            {"component": "VCardText", "props": {"class": "pt-0"},
             "content": [{"component": "div", "props": {"class": "d-flex flex-wrap ga-2"},
                          "content": buttons}]},
        ],
    }


def build_status_overview(sites: Dict[str, dict], last_update: str) -> dict:
    """原因归类统计：每类站点数量 + 占用比例，并给出需要处理的建议。"""
    summary = site_categories.summarize(sites)
    counts = summary["counts"]
    total = sum(counts.values())
    chips = []
    for key, meta in sorted(CATEGORY_META.items(), key=lambda item: item[1]["weight"]):
        count = counts.get(key, 0)
        if not count:
            continue
        chips.append({
            "component": "VChip",
            "props": {"color": meta["color"], "variant": "tonal", "size": "small",
                      "prepend-icon": meta["icon"]},
            "text": f"{meta['label']} {count}",
        })
    if not chips:
        chips.append({"component": "VChip",
                      "props": {"color": "grey", "variant": "tonal", "size": "small"},
                      "text": "暂无数据"})

    content: List[dict] = [
        {"component": "div", "props": {"class": "d-flex flex-wrap ga-2"}, "content": chips},
        {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
         "text": f"共 {total} 个站点 · 数据最后更新：{last_update} · 有邀请名额的站点会自动排在「可邀请」类别"},
    ]
    problems = summary["action_sites"]
    if problems:
        content.append({
            "component": "VAlert",
            "props": {"type": "warning", "variant": "tonal", "class": "mt-3",
                      "text": "需要处理：" + "、".join(
                          f"{item['name']}（{item['label']}：{item['reason'][:24]}）"
                          for item in problems[:8]) + ("…" if len(problems) > 8 else "")},
        })
    return {
        "component": "VCard",
        "props": {"class": "mb-4", "variant": "flat"},
        "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-2 d-flex align-center"},
             "content": [{"component": "VIcon", "props": {"class": "mr-2"}, "text": "mdi-chart-donut"},
                         {"component": "span", "text": "状态归类"}]},
            {"component": "VCardText", "props": {"class": "pt-0"}, "content": content},
        ],
    }


def build_invitable_card(sites: Dict[str, dict]) -> Optional[dict]:
    """可邀请聚合：集中展示有名额的站点，方便马上发邀请。"""
    items = site_categories.summarize(sites)["invitable_sites"]
    if not items:
        return None
    rows = []
    for item in items:
        total_invite = item["permanent_count"] + item["temporary_count"]
        price = item["permanent_invite_price"] or item["temporary_invite_price"] or 0
        rows.append({
            "component": "tr",
            "content": [
                {"component": "td", "text": item["name"]},
                {"component": "td", "text": f"{item['permanent_count']} / {item['temporary_count']}"},
                {"component": "td", "text": str(total_invite)},
                {"component": "td", "text": f"{price:,.0f}" if price else "-"},
                {"component": "td", "text": f"{item['bonus']:,.0f}" if item["bonus"] else "-"},
                {"component": "td", "text": item["reason"][:40]},
            ],
        })
    return {
        "component": "VCard",
        "props": {"class": "mb-4", "variant": "flat"},
        "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-2 d-flex align-center"},
             "content": [{"component": "VIcon", "props": {"class": "mr-2"}, "text": "mdi-account-multiple-plus"},
                         {"component": "span", "text": f"可邀请站点（{len(items)}）"}]},
            {"component": "VCardText", "props": {"class": "pt-0"},
             "content": [{
                 "component": "table",
                 "props": {"class": "site-invitees-table"},
                 "content": [
                     {"component": "thead", "content": [{"component": "tr", "content": [
                         {"component": "th", "text": "站点"},
                         {"component": "th", "text": "永久 / 临时名额"},
                         {"component": "th", "text": "合计"},
                         {"component": "th", "text": "单个所需魔力"},
                         {"component": "th", "text": "当前魔力"},
                         {"component": "th", "text": "说明"},
                     ]}]},
                     {"component": "tbody", "content": rows},
                 ],
             }]},
        ],
    }


def build_problem_card(sites: Dict[str, dict]) -> Optional[dict]:
    """需处理清单：按类别与建议列出异常站点。"""
    problems = site_categories.summarize(sites)["action_sites"]
    if not problems:
        return None
    rows = [{
        "component": "tr",
        "content": [
            {"component": "td", "text": item["name"]},
            {"component": "td", "content": [{
                "component": "VChip",
                "props": {"color": item["color"], "variant": "tonal", "size": "x-small"},
                "text": item["label"]}]},
            {"component": "td", "text": item["reason"][:60]},
            {"component": "td", "text": item["advice"]},
        ],
    } for item in problems]
    return {
        "component": "VCard",
        "props": {"class": "mb-4", "variant": "flat"},
        "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-2 d-flex align-center"},
             "content": [{"component": "VIcon", "props": {"class": "mr-2"}, "text": "mdi-alert-decagram"},
                         {"component": "span", "text": f"需要处理（{len(problems)}）"}]},
            {"component": "VCardText", "props": {"class": "pt-0"},
             "content": [{
                 "component": "table",
                 "props": {"class": "site-invitees-table"},
                 "content": [
                     {"component": "thead", "content": [{"component": "tr", "content": [
                         {"component": "th", "text": "站点"},
                         {"component": "th", "text": "类别"},
                         {"component": "th", "text": "站点返回"},
                         {"component": "th", "text": "建议"},
                     ]}]},
                     {"component": "tbody", "content": rows},
                 ],
             }]},
        ],
    }


def build_trend_card(diff: Optional[dict], history: List[dict]) -> dict:
    """最近变化：新失效/新恢复/名额变化，以及最近几次刷新时间。"""
    diff = diff or {}
    lines: List[str] = []
    for item in diff.get("new_problem", [])[:6]:
        lines.append(f"⚠️ {item['name']} 变为异常（{CATEGORY_META.get(item['to'], {}).get('label', item['to'])}）")
    for item in diff.get("recovered", [])[:6]:
        lines.append(f"✅ {item['name']} 已恢复（{CATEGORY_META.get(item['to'], {}).get('label', item['to'])}）")
    for item in diff.get("invitable", [])[:6]:
        lines.append(f"🎟️ {item['name']} 有可邀请名额了")
    for item in diff.get("changed", [])[:6]:
        lines.append(f"ℹ️ {item['name']} 邀请人数 {item['from'].get('invitees')} → {item['to'].get('invitees')}")
    if not lines:
        lines.append("与上次相比无变化")

    recent = history[-5:] if history else []
    times = [time_item.get("time") for time_item in recent if time_item.get("time")]
    foot = ("最近刷新：" + "、".join(
        time.strftime("%m-%d %H:%M", time.localtime(ts)) for ts in times)) if times else "暂无历史记录"
    return {
        "component": "VCard",
        "props": {"class": "mb-4", "variant": "flat"},
        "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-2 d-flex align-center"},
             "content": [{"component": "VIcon", "props": {"class": "mr-2"}, "text": "mdi-chart-timeline-variant"},
                         {"component": "span", "text": "最近变化"}]},
            {"component": "VCardText", "props": {"class": "pt-0"},
             "content": [{"component": "div", "props": {"class": "text-body-2"},
                          "content": [{"component": "div", "text": line} for line in lines]},
                         {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                          "text": foot}]},
        ],
    }


def build_grouped_table(sites: Dict[str, dict], keyword: str = "") -> dict:
    """按类别分组的站点明细（服务端分组=前端筛选），支持站点名关键词过滤。"""
    grouped: Dict[str, List[dict]] = {}
    keyword = (keyword or "").strip()
    for name, record in (sites or {}).items():
        if keyword and keyword not in name:
            continue
        meta = site_categories.classify(record)
        grouped.setdefault(meta["key"], []).append({"name": name, **meta})

    panels = []
    for key, meta in sorted(CATEGORY_META.items(), key=lambda item: item[1]["weight"]):
        items = grouped.get(key)
        if not items:
            continue
        items.sort(key=lambda item: (-(item["permanent_count"] + item["temporary_count"]), item["name"]))
        rows = [{
            "component": "tr",
            "content": [
                {"component": "td", "text": item["name"]},
                {"component": "td", "text": f"{item['permanent_count']} / {item['temporary_count']}"},
                {"component": "td", "text": str(item["invitee_count"])},
                {"component": "td", "text": item["reason"][:56]},
            ],
        } for item in items]
        panels.append({
            "component": "VExpansionPanel",
            "props": {"title": f"{meta['label']}（{len(items)}）"},
            "content": [{
                "component": "VExpansionPanelText",
                "content": [{
                    "component": "table",
                    "props": {"class": "site-invitees-table"},
                    "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "站点"},
                            {"component": "th", "text": "永久 / 临时名额"},
                            {"component": "th", "text": "后宫人数"},
                            {"component": "th", "text": "说明"},
                        ]}]},
                        {"component": "tbody", "content": rows},
                    ],
                }],
            }],
        })
    if not panels:
        return {"component": "VAlert",
                "props": {"type": "info", "variant": "tonal",
                          "text": "没有匹配的站点（可在插件配置中调整关键词过滤）"}}
    return {
        "component": "VCard",
        "props": {"class": "mb-4", "variant": "flat"},
        "content": [
            {"component": "VCardTitle", "props": {"class": "text-subtitle-2 d-flex align-center"},
             "content": [{"component": "VIcon", "props": {"class": "mr-2"}, "text": "mdi-table-sync"},
                         {"component": "span", "text": "站点明细（按状态分组）"}]},
            {"component": "VCardText", "props": {"class": "pt-0"},
             "content": [{"component": "VExpansionPanels",
                          "props": {"variant": "accordion", "multiple": True},
                          "content": panels}]},
        ],
    }


def build_export_rows(sites: Dict[str, dict]) -> List[Dict[str, Any]]:
    """把站点数据整理成可导出的行（CSV/JSON 共用）。"""
    rows = []
    for name, record in (sites or {}).items():
        meta = site_categories.classify(record)
        rows.append({
            "站点": name,
            "状态": meta["label"],
            "原因": meta["reason"],
            "永久名额": meta["permanent_count"],
            "临时名额": meta["temporary_count"],
            "后宫人数": meta["invitee_count"],
            "魔力值": meta["bonus"],
            "建议": meta["advice"],
        })
    return rows
