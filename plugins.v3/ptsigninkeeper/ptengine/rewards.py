"""Normalize earned rewards without turning account balances into daily income."""
from __future__ import annotations
import re
from urllib.parse import urlsplit

UNITS = ('做种积分', '做種積分', '魔力值', '魔力', '茉莉', '爆米花', '积分', '積分',
         '金币', '金幣', '憨豆', '金贝', '金貝', '鲸币', '鯨幣', '猫粮', '貓糧',
         '铜板', '銅板', 'U币', 'UCoin', '电力值', '蝌蚪', '冰晶', '星焱', '元宝',
         '工分', '蟹币值', '幸运星', '魅力值', '魅力', 'G值', 'bonus', 'points', '奖励', '獎勵')
UNIT_PATTERN = '|'.join(map(re.escape, sorted(UNITS, key=len, reverse=True)))
AMOUNT = r'\d+(?:,\d{3})*(?:\.\d+)?'
COUNTER = r'(?:个|粒|枚|点|個|顆|點|朵|颗|克)?'
# Only currencies checked against the corresponding account header belong here.
# Unknown domains keep their original amount; no universal "magic" fallback.
SITE_UNITS = {
    'ubits.club': 'U币', 'zmpt.cc': '电力值', 'qingwapt.com': '蝌蚪',
    'agsvpt.com': '冰晶', 'xingyungept.org': '星焱', 'cspt.top': '元宝',
    'ptlgs.org': '工分', 'crabpt.vip': '蟹币值', 'luckpt.de': '幸运星',
    'pterclub.net': '猫粮', 'pterclub.com': '猫粮', 'si-qi.xyz': '魔力值', 'u2.dmhy.org': 'UCoin',
    'hddolby.com': '鲸币', 'uhddolby.com': '鲸币',
    'cyanbug.net': '魔力值',
    'gamegamept.com': 'G值',
    'hdcity.city': '魅力值', 'pttime.org': '魔力值',
    'hdarea.club': '魔力值', 'rousi.pro': '魔力值',
}
_NOTE = re.compile(r'Cookie|Browserless|Playwright|回读|回讀|表单|表單|提交|入口|\.php|最近动向', re.I)


def normalize_reward(value, url='', message=''):
    """Prefer an explicit receipt unit, then a verified domain-specific unit."""
    def parse(raw):
        if raw is None or isinstance(raw, bool):
            return ''
        text = re.sub(r'\s+', ' ', str(raw)).strip()
        if re.fullmatch(AMOUNT, text):
            return text
        quantity = rf'[+＋]?\s*({AMOUNT})\s*({COUNTER}\s*(?:{UNIT_PATTERN}))?'
        # An earned verb is evidence; a profile balance or an execution note is not.
        found = re.search(r'(?:获得|獲得|獲取|奖励|獎勵|赠送|增加|得到|签到已得|簽到已得)(?:了)?\s*[:：+]?\s*' + quantity, text, re.I)
        if found:
            return found.group(1) + (' ' + found.group(2).strip() if found.group(2) else '')
        found = re.search(rf'(?:获得|獲得|獲取|奖励|獎勵)(?:了)?\s*({UNIT_PATTERN})\s*[:：]?\s*({AMOUNT})', text, re.I)
        if found:
            return found.group(2) + ' ' + found.group(1)
        found = re.fullmatch(quantity, text, re.I)
        if found:
            return found.group(1) + (' ' + found.group(2).strip() if found.group(2) else '')
        found = re.fullmatch(rf'({UNIT_PATTERN})\s*[:：]?\s*\+?\s*({AMOUNT})', text, re.I)
        if found:
            return found.group(2) + ' ' + found.group(1)
        for part in reversed(re.findall(r'[（(]([^（()）]{1,100})[)）]', text)):
            if not _NOTE.search(part):
                result = parse(part)
                if result:
                    return result
        return ''
    reward = parse(value)
    receipt = parse(message)
    if not reward:
        reward = receipt
    elif re.fullmatch(AMOUNT, reward) and receipt:
        # A unit may be restored only if the recorded quantity agrees.
        amount = re.match(AMOUNT, receipt)
        if amount and amount.group() == reward and not re.fullmatch(AMOUNT, receipt):
            reward = receipt
    if reward and re.fullmatch(AMOUNT, reward):
        host = (urlsplit(url if '://' in str(url) else 'https://' + str(url)).hostname or '').lower().rstrip('.')
        unit = next((unit for domain, unit in SITE_UNITS.items() if host == domain or host.endswith('.' + domain)), '')
        if unit:
            reward += ' ' + unit
    # Display the amount and currency consistently, without optional counters.
    matched = re.fullmatch(rf'({AMOUNT})\s*{COUNTER}\s*({UNIT_PATTERN})', reward, re.I)
    if matched:
        reward = matched.group(1) + ' ' + matched.group(2)
    return reward


def daily_header_reward(text, url=''):
    """Read only the explicitly labelled daily amount, never the adjacent balance."""
    match = re.search(rf'(?:签到已得|簽到已得)\s*[:：]?\s*({AMOUNT})', text)
    if not match:
        return ''
    # Preserve a unit printed directly after the daily amount if present.
    after = re.match(rf'\s*({COUNTER}\s*(?:{UNIT_PATTERN}))', text[match.end():], re.I)
    if after:
        return normalize_reward(match.group(1) + ' ' + after.group(1).strip(), url)
    before = text[max(0, match.start()-180):match.start()]
    candidates = list(re.finditer(rf'({UNIT_PATTERN})\s*(?:\[[^\]]{{0,30}}\]\s*)*[:：]\s*{AMOUNT}', before, re.I))
    if candidates:
        return match.group(1) + ' ' + candidates[-1].group(1)
    return normalize_reward(match.group(1), url)


def pttime_daily_reward(page, day):
    """Read the signed-in account's dated receipt from the read-only list view."""
    from lxml import html
    try:
        root = html.fromstring(page)
    except (ValueError, TypeError):
        return ''
    # The record section identifies its owner; avoid matches in comments/news.
    for heading in root.xpath('//p[b[contains(text(),"7天签到记录")]]'):
        if '用户ID' not in heading.text_content() or not heading.xpath('.//a[contains(@href,"userdetails.php?id=")]'):
            continue
        for node in heading.itersiblings():
            if node.tag in ('h1', 'h2', 'p'):
                break
            if node.tag != 'span':
                continue
            text = ' '.join(node.text_content().split())
            if not re.fullmatch(r'时间：\s*' + re.escape(day) + r' \d{2}:\d{2}:\d{2}', text):
                continue
            reward = node.getnext()
            if reward is None or reward.tag != 'span':
                continue
            match = re.fullmatch(r'获得魔力值：\s*(' + AMOUNT + ')', reward.text_content().strip())
            if match:
                return normalize_reward(match.group(1), 'https://pttime.org')
    return ''


def rousi_daily_reward(payload, day):
    """Use the current account's actual receipt, never the advertised policy."""
    if not isinstance(payload, dict) or payload.get('claimed_today') is not True or payload.get('today') != day:
        return ''
    row = payload.get('today_record')
    if not isinstance(row, dict) or row.get('attendance_date') != day:
        return ''
    value = row.get('total_reward')
    if isinstance(value, bool) or not re.fullmatch(AMOUNT, str(value)):
        return ''
    return normalize_reward(str(value), 'https://rousi.pro')
