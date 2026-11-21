"""U2（u2.dmhy.org）：showup.php 四选一签到。

U2 的 showup 页面不是普通 attendance 表单，而是带一次性 ``req`` / ``hash`` /
``form`` / ``_csrf`` 的四选一提交。流程与 MoviePilot 官方 AutoSignIn 保持一致：

1. 打开 showup.php，明确显示当天完成且已登录时返回“今日已签到”；
2. 有可用视觉 AI 时识别题图；未配置时保留原有随机签到；
3. POST 到 showup.php?action=show；
4. 无论返回页结果如何，都重新读取 showup.php 确认状态，不能只凭 JS 跳转报成功。
"""
from __future__ import annotations

import re
import random
import time
from datetime import datetime
from html import unescape
from typing import Any, Dict, List, Mapping, Tuple
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo


BASE = "https://u2.dmhy.org"
SHOWUP_URL = f"{BASE}/showup.php"


def current_time():
    """按站点使用的北京时间判断，避免受宿主时区影响。"""
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def page_day(page: str) -> str:
    """默认签到页的日期选择器标识本页周期，公开列表中的他人日期不作证据。"""
    from ..quiz import clean_tree
    values = set(clean_tree(page).xpath('//form//input[@name="date" and @type="date"]/@value'))
    if len(values) == 1:
        value = next(iter(values))
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return value
    return ''


def signed_today(page: str) -> bool:
    """有当前周期日期和已登录的完成回执，才能计入今日签到。"""
    now = current_time()
    return now.hour >= 9 and page_day(page) == now.date().isoformat() and _is_signed(page)

_SIGNED_LINK_RE = re.compile(
    r"<a\b(?=[^>]*\bhref\s*=\s*[\"'](?:https?://u2\.dmhy\.org)?/?showup\.php"
    r"(?:\?[^\"']*)?[\"'])[^>]*>\s*(?:已签到|已簽到|Show Up|Показать)\s*</a>",
    re.IGNORECASE | re.DOTALL,
)
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_SIGNED_STATUS_RE = re.compile(
    r"(?:感谢|感謝)[，,]?\s*(?:今天|今日)\s*已(?:签到|簽到)|"
    r"(?:今天|今日)\s*已(?:签到|簽到)",
    re.IGNORECASE,
)
_LOGIN_FORM_RE = re.compile(
    r"<form\b(?=[^>]*\baction\s*=\s*[\"'][^\"']*(?:takelogin|login)\.php[^\"']*[\"'])"
    r"[^>]*>.*?<input\b[^>]*\btype\s*=\s*[\"']password[\"']",
    re.IGNORECASE | re.DOTALL,
)


def _attrs(tag: str) -> Dict[str, str]:
    return {name.lower(): value for name, _, value in _ATTR_RE.findall(tag or "")}


def _answer_form_inner(page: str) -> str:
    """只返回带 req 隐藏字段的真实答题表单，忽略历史查询等其他 showup 表单。"""
    from ..quiz import clean_tree
    from lxml import etree
    safe = etree.tostring(clean_tree(page), encoding="unicode", method="html")
    for _attrs_text, inner in _FORM_RE.findall(safe):
        if re.search(r"<input\b[^>]*\bname\s*=\s*[\"']req[\"']",
                     inner, re.IGNORECASE):
            return inner
    return ""


def _has_signin_form(page: str) -> bool:
    return bool(_answer_form_inner(page))


def _is_signed(page: str) -> bool:
    """已签到页必须明确写“今天/今日已签到”，且不能残留答题表单。"""
    from ..core import visible_markup
    if _has_signin_form(page):
        return False
    page = visible_markup(page or "")
    if "logout.php" not in page.casefold():
        return False
    visible = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page,
                     flags=re.IGNORECASE | re.DOTALL)
    visible = unescape(re.sub(r"<[^>]+>", " ", visible))
    visible = re.sub(r"\s+", "", visible)
    return bool(_SIGNED_STATUS_RE.search(visible))


def _is_login_page(page: str) -> bool:
    return bool(_LOGIN_FORM_RE.search(page or ""))


def _daily_reward(page: str, cookie: str = "", day: str = "") -> str:
    """只读取本人当天签到行；公开列表、账户余额及旧日期均不能作奖励。"""
    from ..core import cookie_uid, plain_text
    from ..quiz import clean_tree
    from lxml import etree

    def user_id(href: str) -> str:
        parsed = urlsplit(urljoin(BASE + '/', href))
        if parsed.hostname != 'u2.dmhy.org' or parsed.path != '/userdetails.php':
            return ''
        values = parse_qs(parsed.query).get('id', [])
        return values[0] if len(values) == 1 and values[0].isdigit() else ''

    root = clean_tree(page)
    ids = {user_id(href) for href in root.xpath('//*[@id="info_block"]//a/@href')}
    ids.discard('')
    cookie_id = str(cookie_uid(cookie) or '')
    if len(ids) > 1 or (ids and cookie_id and cookie_id not in ids):
        return ''
    uid = next(iter(ids), cookie_id)
    if not uid:
        return ''
    day = day or current_time().date().isoformat()
    rewards = set()
    for link in root.xpath('//a[@href]'):
        if user_id(link.get('href', '')) != uid:
            continue
        rows = link.xpath('ancestor::tr[1]')
        if not rows:
            continue
        row = rows[0]
        owners = {user_id(href) for href in row.xpath('.//a/@href')} - {''}
        stamps = row.xpath('.//time/@title | .//time/@datetime')
        if owners != {uid} or len(stamps) != 1 or stamps[0][:10] != day:
            continue
        receipt = plain_text(etree.tostring(row, encoding='unicode', method='html'))
        found = re.search(r'(?:奖励|獎勵)\s*UCoin\s*[:：]\s*(\d[\d,]*(?:\.\d+)?)', receipt, re.I)
        if found:
            rewards.add(found.group(1) + ' UCoin')
    return next(iter(rewards)) if len(rewards) == 1 else ''


def _reward_suffix(page: str, site: Mapping[str, Any]) -> str:
    reward = _daily_reward(page, str(site.get('cookie') or ''))
    return f'（获得 {reward}）' if reward else ''


def _showup_fields(page: str) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """返回 showup 表单的 hidden 字段和 submit 选项。"""
    inner = _answer_form_inner(page)
    if not inner:
        return {}, []
    hidden: Dict[str, str] = {}
    options: List[Tuple[str, str]] = []
    for tag in _INPUT_RE.findall(inner):
        attrs = _attrs(tag)
        name = str(attrs.get("name") or "").strip()
        if not name:
            continue
        kind = str(attrs.get("type") or "text").strip().lower()
        value = str(attrs.get("value") or "")
        if kind == "hidden":
            hidden[name] = value
        elif kind in {"submit", "button", "image"}:
            options.append((name, value))
    return hidden, options


class U2:
    site_url = "u2.dmhy.org"
    authoritative = True

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        # U2 凌晨仍可能显示上一周期“今天已签到”，保守地在 09:00 后核验。
        # 此前不读取或提交旧周期答题，保持未完成以便每日补跑。
        if current_time().hour < 9:
            return False, "待开放：U2 今日签到将在北京时间 09:00 后检查，凌晨回执不计入今日"
        page = ctx.page(SHOWUP_URL)
        if not page:
            return False, "签到失败：打不开 U2 的 showup 页面"
        if _is_login_page(page):
            return False, "签到失败：Cookie 已失效"
        if signed_today(page):
            return True, "今日已签到" + _reward_suffix(page, site)
        if _is_signed(page) or (page_day(page) and page_day(page) != current_time().date().isoformat()):
            return False, "待确认：U2 页面尚未确认今日签到周期，稍后重新检查"

        hidden, options = _showup_fields(page)
        required = ("req", "hash", "form", "_csrf")
        if any(not hidden.get(name) for name in required) or not options:
            return False, "签到失败：未找到 U2 签到参数"

        from ..quiz import clean_tree, choose, claim, read_question_image, solver_available
        rule = getattr(ctx, 'rule', {}) or {}
        ai_mode = solver_available(rule, vision=True)
        try:
            labels = {name: unescape(value).strip() for name, value in options}
            if len(labels) != len(options) or len(options) != 4:
                return False, '待答题：U2 选项结构发生变化，未提交'
            if ai_mode:
                root = clean_tree('<div>' + _answer_form_inner(page) + '</div>')
                images = root.xpath('.//img/@src')
                # Restrict to this answer form, never the public history table.
                if len(images) != 1:
                    return False, '待答题：未能唯一确定 U2 当前题图，未提交'
                session = ctx._session()
                try:
                    picture = read_question_image(session, SHOWUP_URL, images[0], ctx.timeout, ctx.proxies)
                finally:
                    if callable(getattr(session, 'close', None)):
                        session.close()
                selected = choose(rule, '根据题图选择对应的动漫作品。', labels, image=picture)
                answer_name = selected[0]
                answer_value = dict(options)[answer_name]
            else:
                # Explicitly retain original random sign-in when AI is not configured.
                answer_name, answer_value = random.choice(options)
            claim(rule)
        except ValueError as error:
            return False, str(error) if str(error).startswith('待答题：') else '待答题：' + str(error)
        data = {name: hidden[name] for name in required}
        data["message"] = "一切随缘~"
        data[answer_name] = answer_value
        response = ctx.post(
            f"{SHOWUP_URL}?action=show",
            data=data,
            headers={"Referer": SHOWUP_URL, "Origin": BASE},
        )
        if response is not None:
            from ..core import plain_text
            if re.search(r"(?:答案|回答|CAPTCHA).{0,16}(?:错误|錯誤|不正确|incorrect|wrong)", plain_text(response.text or ""), re.I):
                return False, "签到未确认：站点反馈答案未通过；本日不再自动提交"

        # 不依赖提交响应的跳转脚本是否可识别，始终回读 showup.php。
        # 若站点写库较慢，只重复读取，不重复提交答题。
        checked = ""
        for attempt in range(3):
            checked = ctx.page(SHOWUP_URL)
            if signed_today(checked):
                return True, "签到成功" + _reward_suffix(checked, site) + ("；未配置可用视觉 AI，沿用随机签到" if not ai_mode else "")
            if _is_login_page(checked):
                return False, "签到失败：Cookie 已失效"
            if attempt < 2:
                time.sleep(2)
        if _has_signin_form(checked):
            return False, "签到未确认：站点仍显示答题表单；本日不再自动提交"
        return False, "签到失败：已提交，但多次回读仍未确认当天签到状态"
