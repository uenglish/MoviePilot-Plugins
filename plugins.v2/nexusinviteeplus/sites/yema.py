"""YemaPT 新平台处理器。

站点已从 NexusPHP 迁移到新平台（web/www.yemapt.org），旧站的 usercp.php/invite.php
都不存在，因此无法用 NexusPHP 方式读取后宫数据。这里改为调用平台接口校验账号，
并明确提示邀请信息的查看入口，避免误报「无法获取用户ID」。
"""
from typing import Any, Dict

from app.log import logger

from ..site_access import COOKIE_EXPIRED_REASON, get, swap_www
from . import _ISiteHandler


class YemaHandler(_ISiteHandler):
    """YemaPT 新平台：校验账号，提示邀请入口。"""

    site_schema = "yema"
    _profile_api = "/api/user/profile"

    @classmethod
    def match(cls, url: str) -> bool:
        return "yemapt" in (url or "").lower()

    def parse_invite_page(self, site_info: Dict[str, Any], session) -> Dict[str, Any]:
        site_name = site_info.get("name", "")
        result = {
            "invite_status": {
                "can_invite": False,
                "reason": "站点已迁移到新平台，暂未提供邀请数据接口",
                "permanent_count": 0,
                "temporary_count": 0,
                "bonus": 0,
                "permanent_invite_price": 0,
                "temporary_invite_price": 0,
            },
            "invitees": [],
        }
        base_url = (site_info.get("url") or "").strip()
        # 新平台接口只在 www 主机上可用（裸域名会返回前端页面）
        alt_base = swap_www(base_url)
        try:
            # 站点支持 180 天有效期的 API Auth Key，优先带上（无效时仍可回退 Cookie）
            api_key = str(site_info.get("apikey") or "").strip()
            if api_key:
                session.headers.update({"api-auth-key": api_key})
            response = get(session, base_url, self._profile_api)
            if api_key and not (response.text or "").strip().startswith("{"):
                session.headers.pop("api-auth-key", None)
                response = get(session, base_url, self._profile_api)
            if not (response.text or "").strip().startswith("{"):
                if alt_base:
                    logger.debug(f"站点 {site_name} 裸域名未返回接口数据，改用 {alt_base}")
                    base_url = alt_base
                    response = get(session, base_url, self._profile_api)
            if response.status_code in (401, 403) or "login" in (response.url or "").lower():
                result["invite_status"]["reason"] = COOKIE_EXPIRED_REASON
                result["error"] = COOKIE_EXPIRED_REASON
                return result
            payload = response.json() if response.text and response.text.strip().startswith("{") else {}
            if not payload.get("success"):
                reason = "登录状态校验失败，请更新站点 Cookie"
                result["invite_status"]["reason"] = reason
                result["error"] = reason
                return result
            user = (payload.get("data") or {}).get("name") or ""
            result["invite_status"]["reason"] = (
                f"新平台账号已校验（{user}），邀请信息请在「用户成长 → Invite」页面查看"
            )
            logger.info(f"站点 {site_name} 新平台账号校验通过: {user}")
        except Exception as err:
            reason = f"无法访问新平台接口: {err}"
            logger.error(f"站点 {site_name} {reason}")
            result["invite_status"]["reason"] = reason
            result["error"] = reason
        return result
